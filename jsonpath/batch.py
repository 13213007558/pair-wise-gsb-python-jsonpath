"""Compile many JSONPaths into a single, shared query plan."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import AsyncIterable
from typing import Dict
from typing import FrozenSet
from typing import Iterable
from typing import Iterator
from typing import List
from typing import Mapping
from typing import Optional
from typing import Tuple

from ._data import load_data
from .exceptions import JSONPathBatchError
from .exceptions import JSONPathError
from .match import NodeList
from .path import CompoundJSONPath
from .path import JSONPath
from .selectors import Filter
from .selectors import IndexSelector
from .selectors import KeySelector
from .selectors import KeysFilter
from .selectors import KeysSelector
from .selectors import NameSelector
from .selectors import SingularQuerySelector
from .selectors import SliceSelector
from .selectors import WildcardSelector

if TYPE_CHECKING:
    from ._types import JSONData
    from .env import JSONPathEnvironment
    from .filter import BaseExpression
    from .match import FilterContextVars
    from .match import JSONPathMatch
    from .segments import JSONPathSegment
    from .selectors import JSONPathSelector


class BatchResult(Mapping[str, NodeList]):
    """The outcome of applying a `QueryPlan` to some data.

    Attributes:
        results: A mapping of query name to the `NodeList` matched by that
            query. Every query named in `QueryPlan.names` is present, even if
            it matched nothing or raised an exception.
        errors: A mapping of query name to the exception raised by that query.
            Queries that share a failing selection step will report the same
            exception instance.
    """

    __slots__ = ("errors", "results")

    def __init__(
        self,
        results: Dict[str, NodeList],
        errors: Dict[str, Exception],
    ) -> None:
        self.results = results
        self.errors = errors

    def __getitem__(self, name: str) -> NodeList:
        return self.results[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    @property
    def ok(self) -> bool:
        """Return `True` if no query raised an exception."""
        return not self.errors

    def raise_for_errors(self) -> None:
        """Raise a `JSONPathBatchError` if any query raised an exception."""
        if self.errors:
            raise JSONPathBatchError(self.errors, self.results)


class _Unshareable:
    """A trie key that never merges with any other key."""

    __slots__ = ()


class _TrieNode:
    """A node in the shared segment trie."""

    __slots__ = ("all_names", "children", "names", "segment")

    def __init__(self, segment: Optional[JSONPathSegment] = None) -> None:
        self.segment = segment
        self.children: Dict[Any, _TrieNode] = {}
        self.names: List[str] = []
        self.all_names: FrozenSet[str] = frozenset()


class QueryPlan:
    """Multiple named JSONPaths compiled into a single execution plan.

    Selection steps that are common to two or more queries, and that are safe
    to reuse, are resolved once and shared between those queries. A selection
    step is safe to reuse unless it contains a filter expression calling a
    function extension that has not been declared _pure_ (see
    `FilterFunction.pure`). Repeated selectors within a single query are
    never optimized away.

    For every query, the order of results, the number of duplicate matches and
    the normalized path of each match are identical to compiling and executing
    each query individually.

    A plan is stateless with respect to the data it is applied to. It can be
    reused against any number of JSON documents, synchronously or
    asynchronously, without leaking matches from one application to the next.

    Arguments:
        env: The `JSONPathEnvironment` this plan is bound to.
        queries: A mapping of query name to JSONPath string.

    Attributes:
        env: The `JSONPathEnvironment` this plan is bound to.
    """

    __slots__ = ("_direct", "_names", "_roots", "env")

    def __init__(
        self,
        *,
        env: JSONPathEnvironment,
        queries: Mapping[str, str],
    ) -> None:
        self.env = env
        self._names: Tuple[str, ...] = tuple(queries)
        self._roots: Dict[bool, _TrieNode] = {False: _TrieNode(), True: _TrieNode()}
        self._direct: List[Tuple[str, CompoundJSONPath]] = []

        compiled: Dict[str, Any] = {}
        errors: Dict[str, Exception] = {}
        for name, path in queries.items():
            try:
                compiled[name] = env.compile(path)
            except JSONPathError as err:
                errors[name] = err

        if errors:
            raise JSONPathBatchError(errors)

        for name, path in compiled.items():
            if isinstance(path, CompoundJSONPath):
                # Union/intersection queries are executed as-is. Their
                # component paths are not shared with other queries.
                self._direct.append((name, path))
                continue

            node = self._roots[path.pseudo_root]
            for segment in path.segments:
                key: Any = _segment_key(env, segment)
                if key is None:
                    key = _Unshareable()
                child = node.children.get(key)
                if child is None:
                    child = _TrieNode(segment)
                    node.children[key] = child
                node = child
            node.names.append(name)

        for root in self._roots.values():
            _set_all_names(root)

    @property
    def names(self) -> Tuple[str, ...]:
        """The names of the queries in this plan."""
        return self._names

    def apply(
        self, data: JSONData, *, filter_context: Optional[FilterContextVars] = None
    ) -> BatchResult:
        """Apply all queries in this plan to _data_.

        Unlike `finditer()` and `findall()`, this method does not raise if
        a query fails. Inspect `BatchResult.errors` for per-query failures.

        Arguments:
            data: A JSON document or Python object implementing the `Sequence`
                or `Mapping` interfaces.
            filter_context: Arbitrary data made available to filters using
                the _filter context_ selector.

        Returns:
            A `BatchResult` mapping query names to `NodeList` instances.
        """
        _data = load_data(data)
        results: Dict[str, NodeList] = {name: NodeList() for name in self._names}
        errors: Dict[str, Exception] = {}

        for pseudo_root, root_node in self._roots.items():
            if not root_node.names and not root_node.children:
                continue
            root_match = self.env.match_class(
                filter_context=filter_context or {},
                obj=[_data] if pseudo_root else _data,
                parent=None,
                path=self.env.pseudo_root_token if pseudo_root else self.env.root_token,
                parts=(),
                root=_data,
            )
            stack: List[Tuple[_TrieNode, List[JSONPathMatch]]] = [
                (root_node, [root_match])
            ]
            while stack:
                node, matches = stack.pop()
                for name in node.names:
                    results[name].extend(matches)
                for child in node.children.values():
                    try:
                        resolved = list(child.segment.resolve(matches))  # type: ignore[union-attr]
                    except Exception as err:  # noqa: BLE001
                        for name in child.all_names:
                            errors.setdefault(name, err)
                        continue
                    stack.append((child, resolved))

        for name, path in self._direct:
            try:
                results[name].extend(
                    path.finditer(_data, filter_context=filter_context)
                )
            except Exception as err:  # noqa: BLE001
                errors.setdefault(name, err)

        return BatchResult(results, errors)

    async def apply_async(
        self, data: JSONData, *, filter_context: Optional[FilterContextVars] = None
    ) -> BatchResult:
        """An async version of `apply()`."""
        _data = load_data(data)
        results: Dict[str, NodeList] = {name: NodeList() for name in self._names}
        errors: Dict[str, Exception] = {}

        for pseudo_root, root_node in self._roots.items():
            if not root_node.names and not root_node.children:
                continue
            root_match = self.env.match_class(
                filter_context=filter_context or {},
                obj=[_data] if pseudo_root else _data,
                parent=None,
                path=self.env.pseudo_root_token if pseudo_root else self.env.root_token,
                parts=(),
                root=_data,
            )
            stack: List[Tuple[_TrieNode, List[JSONPathMatch]]] = [
                (root_node, [root_match])
            ]
            while stack:
                node, matches = stack.pop()
                for name in node.names:
                    results[name].extend(matches)
                for child in node.children.values():
                    try:
                        resolved = [
                            match
                            async for match in child.segment.resolve_async(  # type: ignore[union-attr]
                                _aiter(matches)
                            )
                        ]
                    except Exception as err:  # noqa: BLE001
                        for name in child.all_names:
                            errors.setdefault(name, err)
                        continue
                    stack.append((child, resolved))

        for name, path in self._direct:
            try:
                async for match in await path.finditer_async(
                    _data, filter_context=filter_context
                ):
                    results[name].append(match)
            except Exception as err:  # noqa: BLE001
                errors.setdefault(name, err)

        return BatchResult(results, errors)

    def finditer(
        self, data: JSONData, *, filter_context: Optional[FilterContextVars] = None
    ) -> Dict[str, NodeList]:
        """Apply all queries in this plan to _data_ and return matches by name.

        Arguments:
            data: A JSON document or Python object implementing the `Sequence`
                or `Mapping` interfaces.
            filter_context: Arbitrary data made available to filters using
                the _filter context_ selector.

        Returns:
            A dict mapping query names to `NodeList` instances.

        Raises:
            JSONPathBatchError: If one or more queries raised an exception.
                The exception's `errors` attribute maps query names to the
                exception each query raised.
        """
        result = self.apply(data, filter_context=filter_context)
        result.raise_for_errors()
        return result.results

    def findall(
        self, data: JSONData, *, filter_context: Optional[FilterContextVars] = None
    ) -> Dict[str, List[object]]:
        """Apply all queries in this plan to _data_ and return values by name.

        Arguments:
            data: A JSON document or Python object implementing the `Sequence`
                or `Mapping` interfaces.
            filter_context: Arbitrary data made available to filters using
                the _filter context_ selector.

        Returns:
            A dict mapping query names to lists of matched objects.

        Raises:
            JSONPathBatchError: If one or more queries raised an exception.
        """
        result = self.apply(data, filter_context=filter_context)
        result.raise_for_errors()
        return {name: matches.values() for name, matches in result.results.items()}

    async def finditer_async(
        self, data: JSONData, *, filter_context: Optional[FilterContextVars] = None
    ) -> Dict[str, NodeList]:
        """An async version of `finditer()`."""
        result = await self.apply_async(data, filter_context=filter_context)
        result.raise_for_errors()
        return result.results

    async def findall_async(
        self, data: JSONData, *, filter_context: Optional[FilterContextVars] = None
    ) -> Dict[str, List[object]]:
        """An async version of `findall()`."""
        result = await self.apply_async(data, filter_context=filter_context)
        result.raise_for_errors()
        return {name: matches.values() for name, matches in result.results.items()}


def _set_all_names(node: _TrieNode) -> FrozenSet[str]:
    names = set(node.names)
    for child in node.children.values():
        names.update(_set_all_names(child))
    node.all_names = frozenset(names)
    return node.all_names


async def _aiter(items: Iterable[JSONPathMatch]) -> AsyncIterable[JSONPathMatch]:
    for item in items:
        yield item


def _segment_key(env: JSONPathEnvironment, segment: JSONPathSegment) -> Any:
    """Return a structural key for _segment_, or `None` if it is unshareable."""
    selector_keys = []
    for selector in segment.selectors:
        key = _selector_key(env, selector)
        if key is None:
            return None
        selector_keys.append(key)
    return (segment.__class__, tuple(selector_keys))


def _selector_key(env: JSONPathEnvironment, selector: JSONPathSelector) -> Any:
    """Return a structural key for _selector_, or `None` if it is unshareable."""
    if isinstance(selector, NameSelector):
        return ("name", selector.name)
    if isinstance(selector, IndexSelector):
        return ("index", selector.index)
    if isinstance(selector, SliceSelector):
        return (
            "slice",
            selector.slice.start,
            selector.slice.stop,
            selector.slice.step,
        )
    if isinstance(selector, WildcardSelector):
        return ("wildcard",)
    if isinstance(selector, KeysSelector):
        return ("keys",)
    if isinstance(selector, KeySelector):
        return ("key", selector.key)
    if isinstance(selector, Filter):
        if not _expression_pure(env, selector.expression):
            return None
        return ("filter", str(selector.expression))
    if isinstance(selector, KeysFilter):
        if not _expression_pure(env, selector.expression):
            return None
        return ("keys_filter", str(selector.expression))
    if isinstance(selector, SingularQuerySelector):
        key = _path_key(env, selector.query)
        if key is None:
            return None
        return ("singular_query", key)
    # Unknown or custom selectors are never shared.
    return None


def _path_key(env: JSONPathEnvironment, path: JSONPath) -> Any:
    """Return a structural key for _path_, or `None` if it is unshareable."""
    keys = []
    for segment in path.segments:
        key = _segment_key(env, segment)
        if key is None:
            return None
        keys.append(key)
    return tuple(keys)


def _expression_pure(env: JSONPathEnvironment, expression: BaseExpression) -> bool:
    """Return `False` if _expression_ calls a function not declared pure."""
    # Deferred to avoid a circular import at package initialization time.
    from .filter import FilterQuery  # noqa: PLC0415
    from .filter import FunctionExtension  # noqa: PLC0415
    from .filter import walk  # noqa: PLC0415

    for node in walk(expression):
        if isinstance(node, FunctionExtension):
            func = env.function_extensions.get(node.name)
            if not getattr(func, "pure", False):
                return False
        elif isinstance(node, FilterQuery) and not _path_pure(env, node.path):
            return False
    return True


def _path_pure(env: JSONPathEnvironment, path: JSONPath) -> bool:
    """Return `False` if any selector in _path_ contains an impure function."""
    for segment in path.segments:
        for selector in segment.selectors:
            expression = getattr(selector, "expression", None)
            if expression is not None and not _expression_pure(env, expression):
                return False
            query = getattr(selector, "query", None)
            if query is not None and not _path_pure(env, query):
                return False
    return True
