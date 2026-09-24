"""Snapshot-based, atomic updates of JSON-like data using JSONPath.

Whereas a naive "find then modify" loop invalidates array indices as items
are removed, functions in this module resolve all target locations against
the document as it was when the operation started, then publish all changes
in one go.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING
from typing import Callable
from typing import Dict
from typing import Iterable
from typing import List
from typing import MutableMapping
from typing import MutableSequence
from typing import NamedTuple
from typing import Optional
from typing import Tuple
from typing import Union

from ._data import load_data
from ._types import JSONData
from .exceptions import JSONPathUpdateError
from .match import JSONPathMatch
from .match import PathPart
from .patch import JSONPatch
from .pointer import JSONPointer

if TYPE_CHECKING:
    from .env import JSONPathEnvironment
    from .match import FilterContextVars


class ConflictStrategy(enum.Enum):
    """How to handle update targets that are ancestors of other targets.

    Attributes:
        ERROR: Raise a `JSONPathUpdateError`. This is the default, forcing
            the caller to choose an explicit strategy.
        SHALLOW: Keep the ancestor target and discard targets inside it.
        DEEP: Keep the descendant targets and discard the ancestor.
    """

    ERROR = "error"
    SHALLOW = "shallow"
    DEEP = "deep"


class _Remove:
    def __repr__(self) -> str:
        return "REMOVE"


REMOVE = _Remove()
"""Sentinel an update callback can return to remove the matched node."""


class UpdateResult(NamedTuple):
    """The result of a snapshot-based update.

    Attributes:
        data: The updated document. This is the input data, modified in
            place, unless the document root was replaced or the input was
            a string or file-like object.
        patch: A `JSONPatch` containing plain RFC 6902 operations that,
            when applied to the original document, produce `data`. This is
            `None` unless a patch was requested.
    """

    data: object
    patch: Optional[JSONPatch]


class _Target(NamedTuple):
    parts: Tuple[PathPart, ...]
    remove: bool
    value: object


def update(  # noqa: D103
    env: JSONPathEnvironment,
    path: Union[str, Iterable[str]],
    data: JSONData,
    func: Optional[Callable[[JSONPathMatch], object]] = None,
    *,
    remove: bool = False,
    conflict: ConflictStrategy = ConflictStrategy.ERROR,
    filter_context: Optional[FilterContextVars] = None,
    patch: bool = False,
) -> UpdateResult:
    """Modify _data_ at locations matched by one or more JSONPaths, atomically.

    All target locations are resolved against _data_ as it is when this
    function is called. Removing several items from the same array, for
    example, uses the array indices from before any modification took place.

    Every distinct node is modified at most once, no matter how many paths
    or selectors match it. If one matched node contains another matched
    node, _conflict_ decides how to proceed.

    All callback results are computed and all targets are validated before
    any modification is published. If a callback raises an exception, _data_
    is left unchanged.

    Arguments:
        env: The `JSONPathEnvironment` used to compile and evaluate paths.
        path: A JSONPath string, or an iterable of JSONPath strings.
        data: A JSON document or Python object implementing the `Sequence`
            or `Mapping` interfaces. Required containers must be mutable.
        func: A callable accepting a `JSONPathMatch` and returning a
            replacement value, or the `REMOVE` sentinel to delete the
            matched node. It is called exactly once per distinct matched
            node, before any modification takes place.
        remove: If `True`, remove all matched nodes. Mutually exclusive
            with _func_.
        conflict: The `ConflictStrategy` used when a matched node is an
            ancestor of another matched node. Defaults to
            `ConflictStrategy.ERROR`.
        filter_context: Arbitrary data made available to filters using
            the _filter context_ selector.
        patch: If `True`, include a plain RFC 6902 `JSONPatch` in the
            result. Applying it to the original document yields the same
            result as this update.

    Returns:
        An `UpdateResult` with the updated document and, optionally, a
        `JSONPatch`.

    Raises:
        JSONPathUpdateError: If _func_ and _remove_ are both given or both
            missing, if matched nodes conflict and _conflict_ is
            `ConflictStrategy.ERROR`, if the document root is removed, or
            if a target can't be modified in place.
    """
    if func is None and not remove:
        raise JSONPathUpdateError("expected a callback or remove=True")
    if func is not None and remove:
        raise JSONPathUpdateError("expected a callback or remove=True, not both")

    _data = load_data(data)
    paths = [path] if isinstance(path, str) else list(path)

    # Resolve all matches before any modification. Keys of this dict are
    # tuples of typed path parts, so array index 0 can never be confused
    # with the object key "0", and keys containing "/" or "~" need no
    # escaping. A node matched by multiple selectors is kept just once.
    matches: Dict[Tuple[PathPart, ...], JSONPathMatch] = {}
    for _path in paths:
        for match in env.compile(_path).finditer(
            _data, filter_context=filter_context
        ):
            matches.setdefault(match.parts, match)

    targets = _resolve_conflicts(matches, conflict)

    # Validate every target against the initial document before computing
    # callback results or publishing any changes.
    for parts in targets:
        _validate_target(_data, parts)

    targets_to_ops: List[_Target] = []
    for parts, match in targets.items():
        if remove:
            _check_not_root(parts)
            targets_to_ops.append(_Target(parts, remove=True, value=None))
        else:
            assert func is not None  # noqa: S101
            value = func(match)  # A failure here leaves _data_ unchanged.
            if value is REMOVE:
                _check_not_root(parts)
                targets_to_ops.append(_Target(parts, remove=True, value=None))
            else:
                targets_to_ops.append(_Target(parts, remove=False, value=value))

    # Publish. Descending path order keeps not-yet-applied target locations
    # valid: removing an array item only affects the locations of its
    # higher-indexed siblings, which have already been handled.
    ordered = sorted(targets_to_ops, key=lambda op: _sort_key(op.parts), reverse=True)

    new_data = _data
    for op in ordered:
        new_data = _apply(new_data, op)

    json_patch = _build_patch(ordered) if patch else None
    return UpdateResult(data=new_data, patch=json_patch)


def _sort_key(parts: Tuple[PathPart, ...]) -> Tuple[Tuple[int, object], ...]:
    # A total order over typed path parts. Object keys sort before array
    # indices; indices compare numerically. A container can only hold one
    # of the two kinds, so the relative order of keys vs. indices is
    # arbitrary but must be consistent.
    return tuple((0, p) if isinstance(p, str) else (1, p) for p in parts)


def _resolve_conflicts(
    matches: Dict[Tuple[PathPart, ...], JSONPathMatch],
    strategy: ConflictStrategy,
) -> Dict[Tuple[PathPart, ...], JSONPathMatch]:
    parts_set = set(matches)

    def ancestors(parts: Tuple[PathPart, ...]) -> Iterable[Tuple[PathPart, ...]]:
        return (parts[:i] for i in range(1, len(parts)))

    def has_target_ancestor(parts: Tuple[PathPart, ...]) -> bool:
        return any(prefix in parts_set for prefix in ancestors(parts))

    if not any(has_target_ancestor(parts) for parts in parts_set):
        return matches

    if strategy is ConflictStrategy.ERROR:
        for parts in sorted(parts_set, key=_sort_key):
            for prefix in ancestors(parts):
                if prefix in parts_set:
                    raise JSONPathUpdateError(
                        f"conflicting update targets: {matches[prefix].path!r} "
                        f"is an ancestor of {matches[parts].path!r}; pass an "
                        "explicit conflict strategy"
                    )

    if strategy is ConflictStrategy.SHALLOW:
        return {
            parts: match
            for parts, match in matches.items()
            if not has_target_ancestor(parts)
        }

    # ConflictStrategy.DEEP
    return {
        parts: match
        for parts, match in matches.items()
        if not any(
            len(other) > len(parts) and other[: len(parts)] == parts
            for other in parts_set
        )
    }


def _check_not_root(parts: Tuple[PathPart, ...]) -> None:
    if not parts:
        raise JSONPathUpdateError("can't remove the document root")


def _validate_target(data: object, parts: Tuple[PathPart, ...]) -> None:
    node = data
    for part in parts:
        if not isinstance(node, (MutableSequence, MutableMapping)):
            raise JSONPathUpdateError(
                f"can't modify {node.__class__.__name__!r} at "
                f"{JSONPointer._encode(parts)!r}: "  # noqa: SLF001
                "expected a mutable sequence or mapping"
            )
        node = node[part]  # type: ignore


def _apply(data: object, op: _Target) -> object:
    if not op.parts:
        # Replace the document root.
        return op.value

    node = data
    for part in op.parts[:-1]:
        node = node[part]  # type: ignore

    key = op.parts[-1]
    if op.remove:
        del node[key]  # type: ignore
    else:
        node[key] = op.value  # type: ignore
    return data


def _build_patch(ordered: List[_Target]) -> JSONPatch:
    json_patch = JSONPatch()
    for op in ordered:
        pointer = JSONPointer.from_parts(
            op.parts, unicode_escape=False, uri_decode=False
        )
        if op.remove:
            json_patch.remove(pointer)
        else:
            json_patch.replace(pointer, op.value)
    return json_patch
