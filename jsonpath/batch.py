"""Compile many JSONPaths into a single plan that shares common selection steps."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import AsyncIterable
from typing import Dict
from typing import Iterable
from typing import List
from typing import Mapping
from typing import Optional
from typing import Tuple
from typing import Union

from ._data import load_data
from .exceptions import JSONPathBatchError
from .exceptions import JSONPathError
from .match import JSONPathMatch
from .path import CompoundJSONPath
from .path import JSONPath
from .selectors import Filter
from .selectors import KeysFilter
from .selectors import SingularQuerySelector

if TYPE_CHECKING:
    from ._types import JSONData
    from .env import JSONPathEnvironment
    from .filter import BaseExpression
    from .match import FilterContextVars
    from .segments import JSONPathSegment
    from .selectors import JSONPathSelector

PathLike = Union[str, JSONPath, CompoundJSONPath]
"""A JSONPath given as a string or an already compiled path."""

NamedPaths = Union[
    Mapping[str, PathLike],
    Iterable[Tuple[str, PathLike]],
]
"""Query names mapped to JSONPaths, or an iterable of (name, path) pairs."""

_PRIVATE = "__jsonpath_private__"


async def _aiter(matches: List[JSONPathMatch]) -> AsyncIterable[JSONPathMatch]:
    for match in matches:
        yield match


class _PlanNode:
    """A node in the shared segment tree."""

    __slots__ = ("branches", "components", "subtree_components")

    def __init__(self) -> None:
        self.branches: Dict[Tuple[object, ...], Tuple[JSONPathSegment, _PlanNode]] = {}
        self.components: List[int] = []
        self.subtree_components: Tuple[int, ...] = ()


class CompiledBatch:
    """Multiple named JSONPaths compiled into a single, reusable execution plan.

    Queries that share a common prefix of segments have that prefix resolved
    just once per execution, with each query still producing exactly the same
    results - order, duplicates and normalized paths included - as it would
    when compiled and executed on its own.

    A segment is only shared between queries when it is safe to do so. In
    particular, filters that call function extensions are not shared unless
    every function they call is known to be free of side effects. Functions
    are considered pure if they are instances of `FilterFunction` with a
    `pure` attribute that is true (as all built-in functions are), or if they
    are any other callable with a `pure` attribute that is true.

    A plan holds no per-execution state, so it can be applied to any number of
    documents, sequentially or concurrently, without leaking matches from one
    execution to the next.

    Arguments:
        env: The `JSONPathEnvironment` to compile and resolve queries with.
        paths: A mapping of query names to JSONPaths, or an iterable of
            `(name, path)` pairs. Names must be unique. Paths can be strings,
            which are compiled against `env`, or already compiled `JSONPath`
            or `CompoundJSONPath` instances.
    """

    __slots__ = (
        "env",
        "names",
        "_component_query",
        "_query_components",
        "_roots",
    )

    def __init__(self, *, env: JSONPathEnvironment, paths: NamedPaths) -> None:
        self.env = env
        items = list(paths.items()) if isinstance(paths, Mapping) else list(paths)
        names = tuple(name for name, _ in items)
        if len(set(names)) != len(names):
            raise ValueError("query names must be unique")
        self.names: Tuple[str, ...] = names

        # Every query is decomposed into one or more components. A plain
        # JSONPath has a single component. A CompoundJSONPath has one
        # component per union/intersection operand.
        self._component_query: List[int] = []
        self._query_components: List[List[Tuple[Optional[str], int]]] = [
            [] for _ in items
        ]
        self._roots: Dict[bool, _PlanNode] = {False: _PlanNode(), True: _PlanNode()}

        for query_index, (name, path) in enumerate(items):
            compiled = self._compile_named(name, path)
            for op, component_path in self._decompose(compiled):
                component_id = len(self._component_query)
                self._component_query.append(query_index)
                self._query_components[query_index].append((op, component_id))
                self._insert(component_id, component_path)

        for root in self._roots.values():
            self._freeze(root)

    def _decompose(
        self, compiled: Union[JSONPath, CompoundJSONPath]
    ) -> List[Tuple[Optional[str], JSONPath]]:
        if isinstance(compiled, JSONPath):
            return [(None, compiled)]
        head = (
            self._decompose(compiled.path)
            if isinstance(compiled.path, CompoundJSONPath)
            else [(None, compiled.path)]
        )
        return [*head, *compiled.paths]

    def _compile_named(
        self, name: str, path: PathLike
    ) -> Union[JSONPath, CompoundJSONPath]:
        if isinstance(path, (JSONPath, CompoundJSONPath)):
            return path
        try:
            return self.env.compile(path)
        except JSONPathError as err:
            raise JSONPathBatchError(
                f"error compiling query {name!r}",
                name=name,
                errors={name: err},
            ) from err

    def _insert(self, component_id: int, path: JSONPath) -> None:
        node = self._roots[path.pseudo_root]
        for position, segment in enumerate(path.segments):
            key = self._segment_key(segment, component_id, position)
            branch = node.branches.get(key)
            if branch is None:
                child = _PlanNode()
                node.branches[key] = (segment, child)
                node = child
            else:
                node = branch[1]
        node.components.append(component_id)

    def _freeze(self, node: _PlanNode) -> Tuple[int, ...]:
        component_ids = list(node.components)
        for _segment, child in node.branches.values():
            component_ids.extend(self._freeze(child))
        node.subtree_components = tuple(component_ids)
        return node.subtree_components

    def _segment_key(
        self, segment: JSONPathSegment, component_id: int, position: int
    ) -> Tuple[object, ...]:
        if all(self._selector_shareable(selector) for selector in segment.selectors):
            return (
                segment.__class__,
                tuple(
                    (selector.__class__, str(selector))
                    for selector in segment.selectors
                ),
            )
        # Segments that might have observable side effects are never shared
        # between queries. Giving them a component-specific key also prevents
        # any of the segments that follow from being shared.
        return (_PRIVATE, component_id, position)

    def _selector_shareable(self, selector: JSONPathSelector) -> bool:
        if isinstance(selector, (Filter, KeysFilter)):
            return self._expression_pure(selector.expression)
        if isinstance(selector, SingularQuerySelector):
            # An embedded query could contain anything. Be conservative.
            return False
        return True

    def _expression_pure(self, expression: BaseExpression) -> bool:
        # Deferred imports to avoid a circular import at package load time.
        from .filter import FunctionExtension  # noqa: PLC0415
        from .filter import walk  # noqa: PLC0415

        for node in walk(expression):
            if isinstance(node, FunctionExtension):
                func = self.env.function_extensions.get(node.name)
                if not getattr(func, "pure", False):
                    return False
        return True

    def finditer(
        self, data: JSONData, *, filter_context: Optional[FilterContextVars] = None
    ) -> Dict[str, List[JSONPathMatch]]:
        """Apply every query to _data_ and return matches by query name.

        Arguments:
            data: A JSON document or Python object implementing the `Sequence`
                or `Mapping` interfaces.
            filter_context: Arbitrary data made available to filters using
                the _filter context_ selector.

        Returns:
            A dictionary mapping each query name to a list of `JSONPathMatch`
            objects, in the same order and with the same duplicates and
            normalized paths as if each query had been executed individually.

        Raises:
            JSONPathBatchError: If any query fails. The exception's `name`,
                `errors` and `results` attributes identify the failing
                queries and the results that did complete.
        """
        _data = load_data(data)
        component_matches: Dict[int, List[JSONPathMatch]] = {}
        failed: Dict[int, Exception] = {}
        for pseudo_root, root_node in self._roots.items():
            if not root_node.subtree_components:
                continue
            root_match = JSONPathMatch(
                filter_context=filter_context or {},
                obj=[_data] if pseudo_root else _data,
                parent=None,
                path=self.env.pseudo_root_token if pseudo_root else self.env.root_token,
                parts=(),
                root=_data,
            )
            self._resolve_node(root_node, [root_match], component_matches, failed)
        return self._collect(component_matches, failed)

    def findall(
        self, data: JSONData, *, filter_context: Optional[FilterContextVars] = None
    ) -> Dict[str, List[object]]:
        """Apply every query to _data_ and return matched objects by query name.

        This is a convenience wrapper around `finditer()` that returns the
        matched object for each match instead of `JSONPathMatch` objects.
        """
        return {
            name: [match.obj for match in matches]
            for name, matches in self.finditer(
                data, filter_context=filter_context
            ).items()
        }

    async def finditer_async(
        self, data: JSONData, *, filter_context: Optional[FilterContextVars] = None
    ) -> Dict[str, List[JSONPathMatch]]:
        """An async version of `finditer()`."""
        _data = load_data(data)
        component_matches: Dict[int, List[JSONPathMatch]] = {}
        failed: Dict[int, Exception] = {}
        for pseudo_root, root_node in self._roots.items():
            if not root_node.subtree_components:
                continue
            root_match = JSONPathMatch(
                filter_context=filter_context or {},
                obj=[_data] if pseudo_root else _data,
                parent=None,
                path=self.env.pseudo_root_token if pseudo_root else self.env.root_token,
                parts=(),
                root=_data,
            )
            await self._resolve_node_async(
                root_node, [root_match], component_matches, failed
            )
        return self._collect(component_matches, failed)

    async def findall_async(
        self, data: JSONData, *, filter_context: Optional[FilterContextVars] = None
    ) -> Dict[str, List[object]]:
        """An async version of `findall()`."""
        return {
            name: [match.obj for match in matches]
            for name, matches in (
                await self.finditer_async(data, filter_context=filter_context)
            ).items()
        }

    def _resolve_node(
        self,
        node: _PlanNode,
        matches: List[JSONPathMatch],
        out: Dict[int, List[JSONPathMatch]],
        failed: Dict[int, Exception],
    ) -> None:
        for component_id in node.components:
            out[component_id] = list(matches)
        for segment, child in node.branches.values():
            try:
                child_matches = list(segment.resolve(matches))
            except Exception as err:  # noqa: BLE001
                for component_id in child.subtree_components:
                    failed.setdefault(component_id, err)
                continue
            self._resolve_node(child, child_matches, out, failed)

    async def _resolve_node_async(
        self,
        node: _PlanNode,
        matches: List[JSONPathMatch],
        out: Dict[int, List[JSONPathMatch]],
        failed: Dict[int, Exception],
    ) -> None:
        for component_id in node.components:
            out[component_id] = list(matches)
        for segment, child in node.branches.values():
            try:
                child_matches = [
                    match async for match in segment.resolve_async(_aiter(matches))
                ]
            except Exception as err:  # noqa: BLE001
                for component_id in child.subtree_components:
                    failed.setdefault(component_id, err)
                continue
            await self._resolve_node_async(child, child_matches, out, failed)

    def _collect(
        self,
        component_matches: Dict[int, List[JSONPathMatch]],
        failed: Dict[int, Exception],
    ) -> Dict[str, List[JSONPathMatch]]:
        results: Dict[str, List[JSONPathMatch]] = {}
        errors: Dict[str, Exception] = {}
        for query_index, name in enumerate(self.names):
            components = self._query_components[query_index]
            error = next(
                (
                    failed[component_id]
                    for _, component_id in components
                    if component_id in failed
                ),
                None,
            )
            if error is not None:
                errors[name] = error
                continue
            matches = list(component_matches[components[0][1]])
            for op, component_id in components[1:]:
                other = component_matches[component_id]
                if op == self.env.union_token:
                    matches.extend(other)
                else:
                    objs = [match.obj for match in other]
                    matches = [match for match in matches if match.obj in objs]
            results[name] = matches
        if errors:
            first_name = next(iter(errors))
            if len(errors) == 1:
                message = f"query {first_name!r} failed"
            else:
                message = (
                    f"queries {', '.join(repr(name) for name in errors)} failed"
                )
            raise JSONPathBatchError(
                message,
                name=first_name,
                errors=errors,
                results=results,
            ) from errors[first_name]
        return results
