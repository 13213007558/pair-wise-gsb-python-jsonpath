"""Bounded, chunked byte input support for JSONPath queries."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Any
from typing import Iterable
from typing import Iterator
from typing import List
from typing import Optional
from typing import Tuple
from typing import cast

from .exceptions import JSONPathBufferError
from .exceptions import JSONPathStreamingError
from .exceptions import JSONPathTypeError
from .filter import CURRENT_KEY
from .filter import FilterContextPath
from .filter import FilterExpression
from .filter import FilterQuery
from .filter import FunctionExtension
from .filter import RootFilterQuery
from .match import JSONPathMatch
from .path import JSONPath
from .segments import JSONPathChildSegment
from .segments import JSONPathRecursiveDescentSegment
from .selectors import Filter
from .selectors import FilterContext
from .selectors import IndexSelector
from .selectors import KeySelector
from .selectors import KeysFilter
from .selectors import KeysSelector
from .selectors import NameSelector
from .selectors import SingularQuerySelector
from .selectors import SliceSelector
from .selectors import WildcardSelector

if TYPE_CHECKING:
    from .match import FilterContextVars

BytesChunk = bytes
BytesChunkStream = Iterable[BytesChunk]

DEFAULT_MAX_ITEM_SIZE = 10 * 1024 * 1024


def prepare_streaming_query(path: JSONPath) -> StreamingJSONPathQuery:
    """Return a bounded streaming execution plan for a compiled JSONPath."""
    if path.pseudo_root:
        raise JSONPathStreamingError("streaming queries must use the document root")
    if not path.segments:
        raise JSONPathStreamingError(
            "streaming queries must select items from the top-level array"
        )

    first_segment = path.segments[0]
    if not isinstance(first_segment, JSONPathChildSegment) or len(
        first_segment.selectors
    ) != 1:
        raise JSONPathStreamingError(
            "the first streaming segment must select from the top-level array"
        )

    first_selector = first_segment.selectors[0]
    if isinstance(first_selector, NameSelector):
        raise JSONPathStreamingError("streaming input must have an array root")
    if not isinstance(
        first_selector, (Filter, IndexSelector, SliceSelector, WildcardSelector)
    ):
        raise JSONPathStreamingError(
            "first streaming selector must be a wildcard, index, slice, or filter"
        )
    if isinstance(first_selector, IndexSelector) and first_selector.index < 0:
        raise JSONPathStreamingError("negative indexes require the complete array")
    if isinstance(first_selector, SliceSelector) and not _forward_slice(
        first_selector
    ):
        raise JSONPathStreamingError("only positive-step slices are supported")
    if isinstance(first_selector, Filter):
        _ensure_local_expression(first_selector.expression)

    for segment in path.segments[1:]:
        if isinstance(segment, JSONPathRecursiveDescentSegment):
            raise JSONPathStreamingError("recursive descent requires unbounded input")
        if not isinstance(segment, JSONPathChildSegment):
            raise JSONPathStreamingError("unsupported streaming segment")
        for selector in segment.selectors:
            _validate_local_selector(selector)

    return StreamingJSONPathQuery(
        path=path,
        first_selector=first_selector,
        remaining_segments=path.segments[1:],
    )


class StreamingJSONPathQuery:
    """A compiled query executable against chunks of a top-level JSON array."""

    __slots__ = ("first_selector", "path", "remaining_segments")

    def __init__(
        self,
        *,
        path: JSONPath,
        first_selector: Any,
        remaining_segments: Tuple[Any, ...],
    ) -> None:
        self.path = path
        self.first_selector = first_selector
        self.remaining_segments = remaining_segments

    def finditer(
        self,
        chunks: BytesChunkStream,
        *,
        filter_context: Optional[FilterContextVars] = None,
        max_item_size: int = DEFAULT_MAX_ITEM_SIZE,
    ) -> Iterator[JSONPathMatch]:
        """Yield complete matches from a chunked JSON document."""
        if max_item_size < 1:
            raise ValueError("max_item_size must be greater than zero")

        context_vars = filter_context or {}
        scanner = _ChunkedArrayScanner(max_item_size=max_item_size)

        for raw_item, start, index in scanner.items(chunks):
            item = _decode_item(raw_item, start)
            item_match = self.path.env.match_class(
                filter_context=context_vars,
                obj=item,
                parent=None,
                path=self.path.env.root_token,
                parts=(),
                root=cast(Any, None),
            )

            if isinstance(self.first_selector, Filter):
                context = FilterContext(
                    env=self.path.env,
                    current=item,
                    root=cast(Any, None),
                    extra_context=context_vars,
                    current_key=index,
                )
                try:
                    accepted = self.first_selector.expression.evaluate(context)
                except JSONPathTypeError as err:
                    if not err.token:
                        err.token = self.first_selector.token
                    raise
                if not self.path.env.is_truthy(accepted):
                    continue
                matches: Iterable[JSONPathMatch] = [item_match.new_child(item, index)]
            else:
                matches = self.first_selector.resolve(item_match)

            for segment in self.remaining_segments:
                matches = segment.resolve(matches)

            yield from matches


class _ChunkedArrayScanner:
    """Extract complete top-level array items without retaining old items."""

    __slots__ = (
        "_array_closed",
        "_buffer",
        "_depth",
        "_escaped",
        "_item_index",
        "_item_start",
        "_max_item_size",
    )

    def __init__(self, *, max_item_size: int) -> None:
        self._max_item_size = max_item_size
        self._array_closed = False
        self._buffer = bytearray()
        self._depth = 0
        self._escaped = False
        self._item_index = 0
        self._item_start = 0

    def items(
        self, chunks: BytesChunkStream
    ) -> Iterator[Tuple[bytes, int, int]]:
        """Yield raw item bytes, their absolute start, and array index."""
        stream = iter(chunks)
        chunk, position = self._skip_whitespace(stream, b"", 0)
        if not chunk or chunk[0] != 0x5B:
            raise _json_error("Expecting value", position)

        offset = 1
        in_item = False
        in_string = False
        self._depth = 0
        self._escaped = False

        while True:
            while offset < len(chunk):
                byte = chunk[offset]

                if not in_item:
                    if byte in b" \t\r\n":
                        offset += 1
                        continue
                    if byte == 0x5D:
                        self._array_closed = True
                        offset += 1
                        self._check_trailing(stream, chunk, position, offset)
                        return
                    if byte == 0x2C:
                        offset += 1
                        continue
                    in_item = True
                    in_string = False
                    self._depth = 0
                    self._escaped = False
                    self._item_start = position + offset
                    self._buffer.clear()

                if len(self._buffer) >= self._max_item_size:
                    raise _buffer_error(self._max_item_size)
                self._buffer.append(byte)
                offset += 1

                if in_string:
                    if self._escaped:
                        self._escaped = False
                    elif byte == 0x5C:
                        self._escaped = True
                    elif byte == 0x22:
                        in_string = False
                    continue

                if byte == 0x22:
                    in_string = True
                elif byte in (0x7B, 0x5B):
                    self._depth += 1
                elif byte in (0x7D, 0x5D):
                    self._depth -= 1
                    if self._depth < 0:
                        raise _json_error(
                            "Unexpected closing delimiter", position + offset - 1
                        )
                    if self._depth == 0:
                        yield self._complete(position, offset)
                        in_item = False
                elif self._depth == 0 and byte in b" \t\r\n,]":
                    del self._buffer[-1]
                    offset -= 1
                    yield self._complete(position, offset)
                    in_item = False

            try:
                next_chunk = next(stream)
            except StopIteration:
                break
            position += len(chunk)
            chunk = next_chunk
            offset = 0

        end_position = position + offset
        if in_string:
            raise _json_error("Unterminated string starting at", self._item_start)
        if in_item or self._depth:
            raise _json_error("Expecting value", end_position)
        raise _json_error("Expecting value", end_position)

    def _complete(self, position: int, offset: int) -> Tuple[bytes, int, int]:
        raw_item = bytes(self._buffer)
        result = raw_item, self._item_start, self._item_index
        self._item_index += 1
        self._buffer.clear()
        return result

    def _check_trailing(
        self,
        stream: Iterator[bytes],
        chunk: bytes,
        position: int,
        offset: int,
    ) -> None:
        while True:
            while offset < len(chunk):
                if chunk[offset] not in b" \t\r\n":
                    raise _json_error("Extra data", position + offset)
                offset += 1
            try:
                next_chunk = next(stream)
            except StopIteration:
                return
            position += len(chunk)
            chunk = next_chunk
            offset = 0

    def _skip_whitespace(
        self, stream: Iterator[bytes], chunk: bytes, position: int
    ) -> Tuple[bytes, int]:
        offset = 0
        while True:
            while offset < len(chunk) and chunk[offset] in b" \t\r\n":
                offset += 1
            if offset < len(chunk):
                return chunk[offset:], position + offset
            position += len(chunk)
            try:
                chunk = next(stream)
            except StopIteration:
                return b"", position
            offset = 0


def _decode_item(raw_item: bytes, start: int) -> object:
    try:
        text = raw_item.decode("utf-8")
    except UnicodeDecodeError as err:
        raise _json_error(err.reason.capitalize(), start + err.start) from err
    try:
        return json.loads(text)
    except json.JSONDecodeError as err:
        absolute_pos = start + min(err.pos, len(raw_item))
        error = json.JSONDecodeError(err.msg, "", absolute_pos)
        error.lineno = 1
        error.colno = absolute_pos + 1
        raise error from err


def _forward_slice(selector: SliceSelector) -> bool:
    selection = selector.slice
    step = 1 if selection.step is None else selection.step
    return (
        step > 0
        and (selection.start is None or selection.start >= 0)
        and (selection.stop is None or selection.stop >= 0)
    )


def _validate_local_selector(selector: Any) -> None:
    if isinstance(selector, Filter):
        _ensure_local_expression(selector.expression)
        return
    if isinstance(selector, (NameSelector, WildcardSelector)):
        return
    if isinstance(selector, IndexSelector):
        if selector.index < 0:
            raise JSONPathStreamingError("negative indexes require the complete array")
        return
    if isinstance(selector, SliceSelector):
        if not _forward_slice(selector):
            raise JSONPathStreamingError("only positive-step slices are supported")
        return
    if isinstance(selector, (KeySelector, KeysFilter, KeysSelector, SingularQuerySelector)):
        raise JSONPathStreamingError("unsupported streaming selector")
    raise JSONPathStreamingError("unsupported streaming selector")


def _ensure_local_expression(expression: FilterExpression) -> None:
    for node in _expression_nodes(expression):
        if isinstance(node, RootFilterQuery):
            raise JSONPathStreamingError("root queries require the complete document")
        if isinstance(node, FilterContextPath):
            raise JSONPathStreamingError("filter context queries are not stream-local")
        if node is CURRENT_KEY:
            raise JSONPathStreamingError("current key is not available before selection")
        if isinstance(node, FunctionExtension):
            raise JSONPathStreamingError("filter functions are not supported yet")
        if isinstance(node, FilterQuery):
            for segment in node.path.segments:
                if isinstance(segment, JSONPathRecursiveDescentSegment):
                    raise JSONPathStreamingError(
                        "recursive filter queries require unbounded input"
                    )
                if not isinstance(segment, JSONPathChildSegment):
                    raise JSONPathStreamingError("unsupported streaming filter query")
                for selector in segment.selectors:
                    _validate_local_selector(selector)


def _expression_nodes(expression: Any) -> Iterator[Any]:
    yield expression
    for child in expression.children():
        yield from _expression_nodes(child)


def _json_error(message: str, position: int) -> json.JSONDecodeError:
    return json.JSONDecodeError(message, "", position)


def _buffer_error(max_item_size: int) -> JSONPathBufferError:
    return JSONPathBufferError(
        f"top-level item exceeds maximum size of {max_item_size} bytes",
        token=None,
    )
