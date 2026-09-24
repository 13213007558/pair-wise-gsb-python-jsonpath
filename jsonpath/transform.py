"""Modify JSON-like data based on an initial set of JSONPath matches.

Unlike a find-and-modify loop, `transform()` computes the full set of
matching nodes up front, then commits all modifications at once. Every
target location - array indices and object keys - is resolved against the
document as it was when the operation started, so removing several items
from the same array does not suffer from index shifting, and results never
depend on the order in which matches were found.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from typing import Any
from typing import Callable
from typing import Iterable
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
from typing import Union

from ._data import load_data
from ._types import JSONData
from .exceptions import JSONPathTransformConflictError
from .exceptions import JSONPathTransformError
from .exceptions import JSONPointerResolutionError
from .match import JSONPathMatch
from .patch import JSONPatch
from .pointer import JSONPointer

PathParts = Tuple[Union[int, str], ...]


class ConflictStrategy(Enum):
    """How to handle ancestor/descendant pairs in a transform target set.

    Attributes:
        ERROR: Raise a `JSONPathTransformConflictError` if the initial match
            set contains a node and one of its ancestors. This is the default.
        PARENT: Keep ancestor matches and discard their descendants.
        CHILD: Keep descendant matches and discard their ancestors.
    """

    ERROR = "error"
    PARENT = "parent"
    CHILD = "child"


@dataclass
class TransformResult:
    """The result of a `transform()` operation.

    Attributes:
        data: The modified document. This is the same object as the input
            data, modified in place, unless the document root was replaced
            with an object of a different type.
        matched: The number of distinct nodes in the initial match set,
            after de-duplicating nodes matched by more than one selector.
        modified: The number of nodes actually modified, after conflict
            resolution.
        patch: A `JSONPatch` containing plain RFC 6902 operations that,
            when applied to the original document, produce the same result.
            `None` unless `return_patch=True` was given.
    """

    data: object
    matched: int
    modified: int
    patch: Optional[JSONPatch]


def transform_matches(
    matches: Iterable[JSONPathMatch],
    data: JSONData,
    callback: Optional[Callable[[JSONPathMatch], object]] = None,
    *,
    delete: bool = False,
    conflict: ConflictStrategy = ConflictStrategy.ERROR,
    return_patch: bool = False,
) -> TransformResult:
    """Modify _data_ at each location in _matches_, atomically.

    All target locations are fixed against the state of _data_ at the time
    of this call. Every callback is invoked - with a match referring to the
    original document - and every target is validated before any
    modification is committed. If a callback raises, _data_ is left
    unchanged.

    Arguments:
        matches: The initial set of matches, usually from
            `JSONPath.finditer()`. Nodes matched more than once are
            modified once only.
        data: A JSON document or Python object implementing the `Sequence`
            or `Mapping` interfaces.
        callback: A function accepting a `JSONPathMatch` and returning a
            replacement value for the matched node. Mutually exclusive with
            _delete_.
        delete: When `True`, remove each matched node instead of replacing
            it. Mutually exclusive with _callback_.
        conflict: The `ConflictStrategy` to apply when the match set
            contains a node and one of its ancestors.
        return_patch: When `True`, include a `JSONPatch` of plain
            RFC 6902 operations in the result.

    Returns:
        A `TransformResult`.

    Raises:
        JSONPathTransformError: If neither _callback_ nor _delete_ is given,
            if both are given, if a removal targets the document root, or if
            a target can not be validated against the original document.
        JSONPathTransformConflictError: If the match set contains a node and
            one of its ancestors and _conflict_ is `ConflictStrategy.ERROR`.
    """
    if callback is None and not delete:
        raise JSONPathTransformError("expected a callback or delete=True")
    if callback is not None and delete:
        raise JSONPathTransformError(
            "callback and delete are mutually exclusive"
        )

    _data = load_data(data)

    # De-duplicate nodes matched by more than one selector. The first match
    # wins, preserving query order. Identity is based on path parts, where
    # array indices are ints and object keys are strings, so a numeric
    # object key can never be confused with an array index.
    unique: List[JSONPathMatch] = []
    seen: Set[PathParts] = set()
    for match in matches:
        if match.parts not in seen:
            seen.add(match.parts)
            unique.append(match)

    targets = _resolve_conflicts(unique, conflict)

    # Compute all callback results and validate all targets before
    # committing any modification. If a callback raises, the original
    # document is left unchanged.
    removals: List[PathParts] = []
    replacements: List[Tuple[PathParts, object]] = []
    if delete:
        for match in targets:
            if not match.parts:
                raise JSONPathTransformError("can't remove the document root")
            removals.append(match.parts)
    else:
        assert callback is not None  # noqa: S101
        for match in targets:
            replacements.append((match.parts, callback(match)))

    for parts in removals:
        _validate(parts, _data)
    for parts, _ in replacements:
        _validate(parts, _data)

    # A single transform is either all removals or all replacements, so
    # replacements can not invalidate each other's paths. Removals are
    # ordered deepest first and, within the same array, highest index
    # first, so that no removal can shift the target location of another.
    patch = JSONPatch()
    for parts in sorted(removals, key=_sort_key, reverse=True):
        patch.remove(_pointer(parts))
    for parts, value in replacements:
        patch.replace(_pointer(parts), value)

    # Commit. We apply the same patch we optionally return, guaranteeing
    # that it produces an identical result when applied to the original
    # document. Working on a deep copy keeps the original intact if
    # anything fails here.
    result = patch.apply(copy.deepcopy(_data))

    if isinstance(_data, dict) and isinstance(result, dict):
        _data.clear()
        _data.update(result)  # type: ignore
        result = _data
    elif isinstance(_data, list) and isinstance(result, list):
        _data.clear()
        _data.extend(result)  # type: ignore
        result = _data

    return TransformResult(
        data=result,
        matched=len(unique),
        modified=len(targets),
        patch=patch if return_patch else None,
    )


def _resolve_conflicts(
    matches: List[JSONPathMatch], strategy: ConflictStrategy
) -> List[JSONPathMatch]:
    """Apply _strategy_ to ancestor/descendant pairs in _matches_."""
    if len(matches) < 2:
        return list(matches)

    by_parts = {match.parts: match for match in matches}
    ancestor_of: Set[PathParts] = set()
    descendant_of: Set[PathParts] = set()

    for match in matches:
        for i in range(1, len(match.parts)):
            prefix = match.parts[:i]
            if prefix in by_parts:
                ancestor_of.add(prefix)
                descendant_of.add(match.parts)

    if not ancestor_of:
        return list(matches)

    if strategy is ConflictStrategy.ERROR:
        for match in matches:
            if match.parts in descendant_of:
                ancestor = next(
                    by_parts[match.parts[:i]]
                    for i in range(1, len(match.parts))
                    if match.parts[:i] in by_parts
                )
                raise JSONPathTransformConflictError(
                    f"{ancestor.path!r} and {match.path!r} target a node and "
                    "its descendant; pass an explicit conflict strategy, "
                    "such as ConflictStrategy.PARENT or ConflictStrategy.CHILD"
                )

    if strategy is ConflictStrategy.PARENT:
        return [m for m in matches if m.parts not in descendant_of]

    assert strategy is ConflictStrategy.CHILD  # noqa: S101
    return [m for m in matches if m.parts not in ancestor_of]


def _sort_key(parts: PathParts) -> Tuple[Tuple[int, Union[int, str]], ...]:
    """Return a sort key for path parts, without comparing ints to strings."""
    return tuple((0, part) if isinstance(part, int) else (1, part) for part in parts)


def _pointer(parts: PathParts) -> JSONPointer:
    """Return a JSON Pointer for _parts_, preserving raw key strings."""
    return JSONPointer.from_parts(parts, unicode_escape=False, uri_decode=False)


def _validate(parts: PathParts, data: Any) -> None:
    """Raise if _parts_ does not identify an existing node in _data_."""
    pointer = _pointer(parts)
    try:
        pointer.resolve(data)
    except JSONPointerResolutionError as err:
        raise JSONPathTransformError(
            f"transform target does not exist: {pointer}"
        ) from err
