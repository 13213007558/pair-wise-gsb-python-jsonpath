"""Streaming JSONPath evaluation over chunked byte input.

This module implements a JSONPath query entry point that consumes a JSON
document as an iterable of byte chunks, yielding matches as soon as they are
fully parsed, without loading the whole document into memory.

Only queries that can be evaluated with bounded buffering are supported. At
compile time, before any input is consumed, a query is rejected with a
`JSONPathStreamError` if it uses:

- the descendant segment (`..`)
- a union of selectors in a single segment
- negative array indices or slice indices
- zero or negative slice steps
- filter expressions that reference the query root (`$`)

Name selectors, the wildcard selector, non-negative array indices, slices
with a positive step, and filter expressions that only depend on the current
array item or object value (`@`) are supported. When a query contains a
filter, the item being tested is buffered in full - fields referenced by the
filter may appear after the fields being returned - but the top-level array
is never loaded in its entirety.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING
from typing import Any
from typing import Callable
from typing import Deque
from typing import Dict
from typing import Iterable
from typing import Iterator
from typing import List
from typing import NoReturn
from typing import Optional
from typing import Tuple
from typing import Union

from .exceptions import JSONPathStreamError
from .exceptions import JSONPathTypeError
from .exceptions import JSONStreamBufferError
from .exceptions import JSONStreamDecodeError
from .filter import BaseExpression
from .filter import RootFilterQuery
from .match import JSONPathMatch
from .path import CompoundJSONPath
from .path import JSONPath
from .segments import JSONPathRecursiveDescentSegment
from .selectors import Filter
from .selectors import FilterContext
from .selectors import IndexSelector
from .selectors import JSONPathSelector
from .selectors import NameSelector
from .selectors import SliceSelector
from .selectors import WildcardSelector
from .serialize import canonical_string

if TYPE_CHECKING:
    from .env import JSONPathEnvironment
    from .match import FilterContextVars
    from .match import PathPart

DEFAULT_MAX_BUFFER_SIZE = 8 * 1024 * 1024  # 8 MiB

# A prefix segment matcher. Given the kind of the parent container ("object"
# or "array") and the part (key or index) selecting a child of that container,
# return the normalized path part to use for the child, or None if the child
# does not match the segment.
PrefixMatcher = Callable[[str, "PathPart"], "Optional[PathPart]"]

BytesLike = Union[bytes, bytearray, memoryview]

_SPACE = 0x20
_TAB = 0x09
_LF = 0x0A
_CR = 0x0D
_QUOTE = 0x22
_BACKSLASH = 0x5C
_LBRACE = 0x7B
_RBRACE = 0x7D
_LBRACKET = 0x5B
_RBRACKET = 0x5D
_COLON = 0x3A
_COMMA = 0x2C
_MINUS = 0x2D
_PLUS = 0x2B
_DOT = 0x2E
_ZERO = 0x30
_NINE = 0x39

_SIMPLE_ESCAPES = {
    0x22: '"',
    0x5C: "\\",
    0x2F: "/",
    0x62: "\b",
    0x66: "\f",
    0x6E: "\n",
    0x72: "\r",
    0x74: "\t",
}


def _is_digit(byte: int) -> bool:
    return _ZERO <= byte <= _NINE


def _is_hex(byte: int) -> bool:
    return (
        _is_digit(byte)
        or 0x41 <= byte <= 0x46  # A-F
        or 0x61 <= byte <= 0x66  # a-f
    )


def _part_to_str(part: Union[int, str]) -> str:
    if isinstance(part, str):
        return f"[{canonical_string(part)}]"
    return f"[{part}]"


class _SliceMatcher:
    """Matches array indices against a slice with a positive step.

    Also able to enumerate the indices selected from a sequence of known
    length, used when the regular engine's (non-standard) slicing of strings
    is replicated.
    """

    __slots__ = ("start", "stop", "step")

    def __init__(self, start: int, stop: Optional[int], step: int) -> None:
        self.start = start
        self.stop = stop
        self.step = step

    def __call__(self, kind: str, part: "PathPart") -> "Optional[PathPart]":
        if kind != "array" or not isinstance(part, int):
            return None
        if part < self.start:
            return None
        if self.stop is not None and part >= self.stop:
            return None
        if (part - self.start) % self.step:
            return None
        return part

    def indices(self, length: int) -> range:
        """Return the indices selected from a sequence of _length_ items."""
        stop = length if self.stop is None else min(self.stop, length)
        return range(self.start, stop, self.step)


class _ChunkReader:
    """A pull-based byte reader over an iterable of byte chunks.

    Bytes are only consumed from the upstream iterable as the parser demands
    them, so a caller can observe partial consumption while iterating matches.
    """

    def __init__(self, chunks: Union[Iterable[BytesLike], BytesLike]) -> None:
        if isinstance(chunks, (bytes, bytearray, memoryview)):
            chunks = (chunks,)
        self._chunks = iter(chunks)
        self._buf = bytearray()
        self._start = 0  # absolute offset of self._buf[0]
        self.pos = 0  # absolute offset of the next byte to read
        self.eof = False

    def _fill(self) -> bool:
        if self.eof:
            return False
        if self.pos > self._start:
            del self._buf[: self.pos - self._start]
            self._start = self.pos
        for chunk in self._chunks:
            if chunk:
                self._buf += chunk
                return True
        self.eof = True
        return False

    def peek(self, offset: int = 0) -> Optional[int]:
        """Return the byte at _offset_ from the read position, or None at EOF."""
        while self.pos + offset - self._start >= len(self._buf):
            if not self._fill():
                return None
        return self._buf[self.pos + offset - self._start]

    def advance(self, count: int = 1) -> None:
        """Move the read position forward _count_ bytes."""
        self.pos += count


class StreamQuery:
    """A compiled JSONPath, ready for streaming evaluation over byte chunks.

    Instances are created with `JSONPathEnvironment.compile_stream()` or the
    top-level `jsonpath.compile_stream()` function. A `JSONPathStreamError`
    is raised at compile time if the query can not be evaluated with bounded
    buffering.

    Attributes:
        env: The `JSONPathEnvironment` this query is bound to.
        path: The compiled `JSONPath` this query was built from.
        prefix: Matchers for the leading segments that are applied
            incrementally, without buffering any values.
        boundary: The first `Filter` selector in the query, if any. Items
            it applies to are buffered one at a time.
        suffix: A `JSONPath` made of the segments following _boundary_, if
            any, evaluated in memory against each buffered item.
    """

    __slots__ = ("env", "path", "prefix", "boundary", "suffix")

    def __init__(
        self, *, env: JSONPathEnvironment, path: Union[JSONPath, CompoundJSONPath]
    ) -> None:
        if isinstance(path, CompoundJSONPath):
            raise JSONPathStreamError(
                "union and intersection of paths is not supported for streaming"
            )
        if path.pseudo_root:
            raise JSONPathStreamError(
                "pseudo-root queries are not supported for streaming"
            )

        self.env = env
        self.path = path
        self.prefix: List[PrefixMatcher] = []
        self.boundary: Optional[Filter] = None
        boundary_index: Optional[int] = None

        for index, segment in enumerate(path.segments):
            if isinstance(segment, JSONPathRecursiveDescentSegment):
                raise JSONPathStreamError(
                    "the descendant segment (..) is not supported for streaming",
                    token=segment.token,
                )
            if len(segment.selectors) != 1:
                raise JSONPathStreamError(
                    "a union of selectors is not supported for streaming",
                    token=segment.token,
                )
            selector = segment.selectors[0]
            if isinstance(selector, Filter):
                self._check_filter_expression(selector)
                if boundary_index is None:
                    boundary_index = index
                    self.boundary = selector
            elif boundary_index is None:
                self.prefix.append(self._compile_prefix_matcher(selector))
            else:
                self._validate_selector(selector)

        if boundary_index is not None:
            # Segments following the first filter segment are evaluated in
            # memory against each buffered item.
            segments = path.segments[boundary_index + 1 :]
            self.suffix: Optional[JSONPath] = (
                JSONPath(env=env, segments=segments) if segments else None
            )
        else:
            self.suffix = None

    def __str__(self) -> str:
        return str(self.path)

    def finditer(
        self,
        chunks: Union[Iterable[BytesLike], BytesLike],
        *,
        max_buffer_size: int = DEFAULT_MAX_BUFFER_SIZE,
        filter_context: Optional[FilterContextVars] = None,
    ) -> Iterator[JSONPathMatch]:
        """Generate `JSONPathMatch` objects from chunked JSON byte input.

        Matches are produced as soon as each matched value has been fully
        parsed, in the same order as `JSONPath.finditer()`. Input is only
        pulled from _chunks_ as needed, so iteration can stop early without
        consuming the whole document.

        Arguments:
            chunks: An iterable of `bytes` (or a single `bytes`-like
                object) making up one JSON document. String escapes and UTF-8
                sequences may span chunk boundaries.
            max_buffer_size: The maximum number of input bytes to buffer for
                a single item. A `JSONStreamBufferError` is raised if an
                item that must be buffered exceeds this limit.
            filter_context: Arbitrary data made available to filters using
                the _filter context_ selector.

        Returns:
            An iterator yielding `JSONPathMatch` objects for each match.

        Raises:
            JSONStreamDecodeError: If the input is not valid JSON. The
                exception's `position` attribute is the absolute byte offset
                of the error.
            JSONStreamBufferError: If a single buffered item exceeds
                _max_buffer_size_ bytes.
            JSONPathTypeError: If a filter expression attempts to use types in
                an incompatible way.
        """
        return _StreamEngine(
            env=self.env,
            query=self,
            chunks=chunks,
            max_buffer_size=max_buffer_size,
            filter_context=filter_context,
        ).run()

    def findall(
        self,
        chunks: Union[Iterable[BytesLike], BytesLike],
        *,
        max_buffer_size: int = DEFAULT_MAX_BUFFER_SIZE,
        filter_context: Optional[FilterContextVars] = None,
    ) -> List[object]:
        """Return a list of objects matching this query from chunked input.

        See `StreamQuery.finditer()` for details.
        """
        return [
            match.obj
            for match in self.finditer(
                chunks,
                max_buffer_size=max_buffer_size,
                filter_context=filter_context,
            )
        ]

    def _validate_selector(self, selector: JSONPathSelector) -> None:
        """Raise `JSONPathStreamError` if _selector_ is not stream-safe."""
        if isinstance(selector, (NameSelector, WildcardSelector)):
            return
        if isinstance(selector, IndexSelector):
            if selector.index < 0:
                raise JSONPathStreamError(
                    "negative array indices are not supported for streaming",
                    token=selector.token,
                )
            return
        if isinstance(selector, SliceSelector):
            self._check_slice(selector)
            return
        if isinstance(selector, Filter):
            self._check_filter_expression(selector)
            return
        raise JSONPathStreamError(
            f"{type(selector).__name__} is not supported for streaming",
            token=selector.token,
        )

    def _check_slice(self, selector: SliceSelector) -> None:
        _slice = selector.slice
        step = _slice.step if _slice.step is not None else 1
        if step < 1:
            raise JSONPathStreamError(
                "slice steps less than 1 are not supported for streaming",
                token=selector.token,
            )
        if (_slice.start is not None and _slice.start < 0) or (
            _slice.stop is not None and _slice.stop < 0
        ):
            raise JSONPathStreamError(
                "negative slice indices are not supported for streaming",
                token=selector.token,
            )

    def _check_filter_expression(self, selector: Filter) -> None:
        stack: List[BaseExpression] = [selector.expression]
        while stack:
            node = stack.pop()
            if isinstance(node, RootFilterQuery):
                raise JSONPathStreamError(
                    "filter expressions that reference the root of the document "
                    "are not supported for streaming",
                    token=selector.token,
                )
            stack.extend(node.children())

    def _compile_prefix_matcher(self, selector: JSONPathSelector) -> PrefixMatcher:
        self._validate_selector(selector)

        if isinstance(selector, NameSelector):
            name = selector.name

            def match_name(kind: str, part: PathPart) -> Optional[PathPart]:
                if kind == "object" and part == name:
                    return part
                return None

            return match_name

        if isinstance(selector, WildcardSelector):

            def match_wildcard(_kind: str, part: PathPart) -> Optional[PathPart]:
                return part

            return match_wildcard

        if isinstance(selector, IndexSelector):
            index = selector.index
            as_key = str(index)
            strict = self.env.strict

            def match_index(kind: str, part: PathPart) -> Optional[PathPart]:
                if kind == "array" and part == index:
                    return index
                if not strict and kind == "object" and part == as_key:
                    return index
                return None

            return match_index

        assert isinstance(selector, SliceSelector)
        _slice = selector.slice
        start = _slice.start if _slice.start is not None else 0
        stop = _slice.stop
        step = _slice.step if _slice.step is not None else 1

        return _SliceMatcher(start, stop, step)


class _StreamEngine:
    """Incrementally parse chunked JSON input, emitting matches as they complete."""

    def __init__(
        self,
        *,
        env: JSONPathEnvironment,
        query: StreamQuery,
        chunks: Union[Iterable[BytesLike], BytesLike],
        max_buffer_size: int,
        filter_context: Optional[FilterContextVars],
    ) -> None:
        self._env = env
        self._query = query
        self._reader = _ChunkReader(chunks)
        self._max_buffer_size = max_buffer_size
        self._filter_context = filter_context or {}
        # Containers on the path to the current position. The root container
        # has a `part` of `None`.
        self._containers: List[Dict[str, Any]] = []
        self._parts: List[PathPart] = []
        self._path = env.root_token
        self._path_stack: List[str] = []
        self._skipped = 0  # nesting depth of a skipped subtree
        self._capture: Optional[Dict[str, Any]] = None
        self._out: Deque[JSONPathMatch] = deque()

    def run(self) -> Iterator[JSONPathMatch]:
        """Parse the whole document, yielding matches as they complete."""
        self._skip_ws()
        pos = self._reader.pos
        if self._reader.peek() is None:
            self._error("expected a JSON value", pos)
        yield from self._parse_any()
        self._skip_ws()
        pos = self._reader.pos
        if self._reader.peek() is not None:
            self._error("unexpected data after top-level value", pos)

    def _parse_any(self) -> Iterator[JSONPathMatch]:  # noqa: PLR0912, PLR0915
        stack: List[List[Any]] = []
        pending_value = True
        while True:
            while self._out:
                yield self._out.popleft()
            self._check_buffer()
            if pending_value:
                self._skip_ws()
                pos = self._reader.pos
                byte = self._reader.peek()
                if byte is None:
                    self._error("unexpected end of input", pos)
                if byte == _LBRACE:
                    self._reader.advance()
                    self._begin_container("object", pos)
                    stack.append(["object", "first"])
                    pending_value = False
                    continue
                if byte == _LBRACKET:
                    self._reader.advance()
                    self._begin_container("array", pos)
                    stack.append(["array", "first"])
                    pending_value = False
                    continue
                self._parse_scalar(byte, pos)
                _value_done(stack)
                pending_value = False
                if not stack:
                    break
                continue
            frame = stack[-1]
            self._skip_ws()
            pos = self._reader.pos
            byte = self._reader.peek()
            if byte is None:
                self._error("unexpected end of input", pos)
            if frame[0] == "object":
                state = frame[1]
                if state in ("first", "expect_key"):
                    if state == "first" and byte == _RBRACE:
                        self._reader.advance()
                        stack.pop()
                        self._end_container()
                        _value_done(stack)
                        if not stack:
                            break
                        continue
                    key = self._parse_string(pos)
                    self._skip_ws()
                    if self._reader.peek() != _COLON:
                        self._error("expected ':'", self._reader.pos)
                    self._reader.advance()
                    self._on_key(key)
                    frame[1] = "expect_value"
                    pending_value = True
                    continue
                if byte == _COMMA:
                    self._reader.advance()
                    frame[1] = "expect_key"
                    continue
                if byte == _RBRACE:
                    self._reader.advance()
                    stack.pop()
                    self._end_container()
                    _value_done(stack)
                    if not stack:
                        break
                    continue
                self._error("expected ',' or '}'", pos)
            else:
                state = frame[1]
                if state == "first":
                    if byte == _RBRACKET:
                        self._reader.advance()
                        stack.pop()
                        self._end_container()
                        _value_done(stack)
                        if not stack:
                            break
                        continue
                    frame[1] = "expect_comma"
                    pending_value = True
                    continue
                if byte == _COMMA:
                    self._reader.advance()
                    pending_value = True
                    continue
                if byte == _RBRACKET:
                    self._reader.advance()
                    stack.pop()
                    self._end_container()
                    _value_done(stack)
                    if not stack:
                        break
                    continue
                self._error("expected ',' or ']'", pos)
        while self._out:
            yield self._out.popleft()

    def _parse_scalar(self, byte: int, pos: int) -> None:
        if byte == _QUOTE:
            self._on_scalar(self._parse_string(pos), pos)
        elif byte == 0x74:  # t
            self._expect_literal(b"true")
            self._on_scalar(value=True, pos=pos)
        elif byte == 0x66:  # f
            self._expect_literal(b"false")
            self._on_scalar(value=False, pos=pos)
        elif byte == 0x6E:  # n
            self._expect_literal(b"null")
            self._on_scalar(None, pos)
        elif byte == _MINUS or _is_digit(byte):
            self._on_scalar(self._parse_number(), pos)
        else:
            self._error(f"unexpected character {chr(byte)!r}", pos)

    def _expect_literal(self, literal: bytes) -> None:
        for expected in literal:
            byte = self._reader.peek()
            if byte is None or byte != expected:
                self._error("invalid literal", self._reader.pos)
            self._reader.advance()

    def _parse_string(self, pos: int) -> str:
        # Assumes the current byte is the opening quote.
        self._reader.advance()
        parts: List[str] = []
        raw = bytearray()
        raw_start = self._reader.pos
        while True:
            byte = self._reader.peek()
            if byte is None:
                self._error("unterminated string", pos)
            if byte == _QUOTE:
                if raw:
                    parts.append(self._decode(raw, raw_start))
                self._reader.advance()
                return "".join(parts)
            if byte == _BACKSLASH:
                if raw:
                    parts.append(self._decode(raw, raw_start))
                    raw = bytearray()
                self._reader.advance()
                parts.append(self._parse_escape(pos))
                raw_start = self._reader.pos
                continue
            if byte < _SPACE:
                self._error("invalid control character in string", self._reader.pos)
            raw.append(byte)
            self._reader.advance()
            self._check_buffer()

    def _parse_escape(self, pos: int) -> str:
        byte = self._reader.peek()
        if byte is None:
            self._error("unterminated string", pos)
        if byte == 0x75:  # u
            self._reader.advance()
            code = self._parse_hex4()
            if (
                0xD800 <= code <= 0xDBFF
                and self._reader.peek() == _BACKSLASH
                and self._reader.peek(1) == 0x75
            ):
                self._reader.advance(2)
                low = self._parse_hex4()
                if 0xDC00 <= low <= 0xDFFF:
                    return chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00))
                return chr(code) + chr(low)
            return chr(code)
        escape = _SIMPLE_ESCAPES.get(byte)
        if escape is None:
            self._error("invalid escape sequence in string", self._reader.pos)
        self._reader.advance()
        return escape

    def _parse_hex4(self) -> int:
        code = 0
        for _ in range(4):
            byte = self._reader.peek()
            if byte is None or not _is_hex(byte):
                self._error("invalid \\u escape sequence in string", self._reader.pos)
            code = code * 16 + int(chr(byte), 16)
            self._reader.advance()
        return code

    def _parse_number(self) -> Union[int, float]:
        buf = bytearray()
        byte = self._reader.peek()
        if byte == _MINUS:
            buf.append(byte)
            self._reader.advance()
        byte = self._reader.peek()
        if byte is None or not _is_digit(byte):
            self._error("invalid number", self._reader.pos)
        if byte == _ZERO:
            buf.append(byte)
            self._reader.advance()
        else:
            while True:
                byte = self._reader.peek()
                if byte is None or not _is_digit(byte):
                    break
                buf.append(byte)
                self._reader.advance()
        is_float = False
        if self._reader.peek() == _DOT:
            is_float = True
            buf.append(_DOT)
            self._reader.advance()
            self._parse_digits(buf)
        byte = self._reader.peek()
        if byte in (0x65, 0x45):  # e, E
            is_float = True
            buf.append(byte)
            self._reader.advance()
            byte = self._reader.peek()
            if byte in (_PLUS, _MINUS):
                buf.append(byte)
                self._reader.advance()
            self._parse_digits(buf)
        text = buf.decode("ascii")
        return float(text) if is_float else int(text)

    def _parse_digits(self, buf: bytearray) -> None:
        byte = self._reader.peek()
        if byte is None or not _is_digit(byte):
            self._error("invalid number", self._reader.pos)
        while byte is not None and _is_digit(byte):
            buf.append(byte)
            self._reader.advance()
            self._check_buffer()
            byte = self._reader.peek()

    def _decode(self, raw: bytearray, start: int) -> str:
        try:
            return bytes(raw).decode("utf-8")
        except UnicodeDecodeError as err:
            raise JSONStreamDecodeError(
                "invalid UTF-8 in string", position=start + err.start
            ) from err

    def _skip_ws(self) -> None:
        while True:
            byte = self._reader.peek()
            if byte not in (_SPACE, _TAB, _LF, _CR):
                return
            self._reader.advance()

    def _error(self, message: str, pos: int) -> NoReturn:
        raise JSONStreamDecodeError(message, position=pos)

    def _check_buffer(self) -> None:
        capture = self._capture
        if (
            capture is not None
            and self._reader.pos - capture["start"] > self._max_buffer_size
        ):
            raise JSONStreamBufferError(
                "buffered item exceeds the maximum buffer size",
                position=self._reader.pos,
                limit=self._max_buffer_size,
            )

    def _begin_container(self, kind: str, pos: int) -> None:  # noqa: PLR0911
        capture = self._capture
        if capture is not None:
            capture["builders"].append([kind, {} if kind == "object" else [], None])
            return
        if self._skipped:
            self._skipped += 1
            return
        query = self._query
        if not self._containers:
            # The root value.
            if query.prefix or query.boundary is not None:
                self._push_tracked(kind, None)
            else:
                self._start_capture(kind, pos, None, "match")
            return
        part = self._next_part()
        seg_index = len(self._parts)
        if seg_index < len(query.prefix):
            norm = query.prefix[seg_index](self._containers[-1]["kind"], part)
            if norm is None:
                self._skipped = 1
                return
            if seg_index + 1 == len(query.prefix) and query.boundary is None:
                self._start_capture(kind, pos, norm, "match")
                return
            self._push_tracked(kind, norm)
            return
        if query.boundary is not None and seg_index == len(query.prefix):
            self._start_capture(kind, pos, part, "filter")
            return
        self._skipped = 1

    def _end_container(self) -> None:
        capture = self._capture
        if capture is not None:
            builders = capture["builders"]
            _kind, value, _key = builders.pop()
            if builders:
                self._builder_add(builders, value)
            else:
                self._capture = None
                self._finish_capture(capture, value)
            return
        if self._skipped:
            self._skipped -= 1
            return
        container = self._containers.pop()
        if container["part"] is not None:
            self._parts.pop()
            self._path = self._path_stack.pop()

    def _on_key(self, key: str) -> None:
        capture = self._capture
        if capture is not None:
            capture["builders"][-1][2] = key
            return
        if self._skipped:
            return
        self._containers[-1]["key"] = key

    def _on_scalar(self, value: Any, pos: int) -> None:  # noqa: ARG002
        capture = self._capture
        if capture is not None:
            self._builder_add(capture["builders"], value)
            return
        if self._skipped:
            return
        query = self._query
        if not self._containers:
            # The root value is a scalar.
            if not query.prefix and query.boundary is None:
                self._emit_match(value, (), self._env.root_token)
            return
        part = self._next_part()
        seg_index = len(self._parts)
        if seg_index < len(query.prefix):
            norm = query.prefix[seg_index](self._containers[-1]["kind"], part)
            if norm is None:
                return
            if seg_index + 1 == len(query.prefix) and query.boundary is None:
                self._emit_match(
                    value,
                    tuple(self._parts) + (norm,),
                    self._path + _part_to_str(norm),
                )
            elif isinstance(value, str):
                # The regular engine applies slice selectors to strings.
                self._expand_string(
                    value,
                    tuple(self._parts) + (norm,),
                    self._path + _part_to_str(norm),
                    seg_index + 1,
                )
            return
        if query.boundary is not None and seg_index == len(query.prefix):
            self._select_candidate(value, part)

    def _expand_string(
        self, value: str, parts: Tuple[PathPart, ...], path: str, seg_index: int
    ) -> None:
        query = self._query
        while seg_index < len(query.prefix):
            matcher = query.prefix[seg_index]
            if not isinstance(matcher, _SliceMatcher):
                return
            if seg_index + 1 == len(query.prefix):
                if query.boundary is None:
                    for index in matcher.indices(len(value)):
                        self._emit_match(
                            value[index],
                            parts + (index,),
                            path + _part_to_str(index),
                        )
                else:
                    for index in matcher.indices(len(value)):
                        self._select_candidate(
                            value[index],
                            index,
                            base_parts=parts,
                            base_path=path,
                        )
                return
            for index in matcher.indices(len(value)):
                self._expand_string(
                    value[index],
                    parts + (index,),
                    path + _part_to_str(index),
                    seg_index + 1,
                )
            return

    def _next_part(self) -> PathPart:
        container = self._containers[-1]
        if container["kind"] == "array":
            part: PathPart = container["index"]
            container["index"] += 1
            return part
        part = container["key"]
        container["key"] = None
        return part

    def _push_tracked(self, kind: str, part: Optional[PathPart]) -> None:
        self._containers.append({"kind": kind, "index": 0, "key": None, "part": part})
        if part is not None:
            self._path_stack.append(self._path)
            self._path += _part_to_str(part)
            self._parts.append(part)

    def _start_capture(
        self, kind: str, pos: int, part: Optional[PathPart], purpose: str
    ) -> None:
        self._capture = {
            "start": pos,
            "part": part,
            "purpose": purpose,
            "builders": [[kind, {} if kind == "object" else [], None]],
        }

    def _finish_capture(self, capture: Dict[str, Any], value: Any) -> None:
        part = capture["part"]
        if capture["purpose"] == "match":
            if part is None:
                self._emit_match(value, (), self._env.root_token)
            else:
                self._emit_match(
                    value,
                    tuple(self._parts) + (part,),
                    self._path + _part_to_str(part),
                )
            return
        self._select_candidate(value, part)

    def _select_candidate(
        self,
        value: Any,
        part: PathPart,
        base_parts: Optional[Tuple[PathPart, ...]] = None,
        base_path: Optional[str] = None,
    ) -> None:
        query = self._query
        selector = query.boundary
        assert selector is not None  # noqa: S101
        if selector.cacheable_nodes and self._env.filter_caching:
            expr = selector.expression.cache_tree()
        else:
            expr = selector.expression
        context = FilterContext(
            env=self._env,
            current=value,
            root=value,
            extra_context=self._filter_context,
            current_key=part,
        )
        try:
            selected = expr.evaluate(context)
        except JSONPathTypeError as err:
            if not err.token:
                err.token = selector.token
            raise
        if not selected:
            return
        if base_parts is None:
            base_parts = tuple(self._parts)
        if base_path is None:
            base_path = self._path
        base_parts = base_parts + (part,)
        base_path = base_path + _part_to_str(part)
        if query.suffix is None:
            self._emit_match(value, base_parts, base_path)
            return
        base = JSONPathMatch(
            filter_context=self._filter_context,
            obj=value,
            parent=None,
            parts=base_parts,
            path=base_path,
            root=value,
        )
        memo: Dict[int, JSONPathMatch] = {}
        for match in query.suffix.finditer(value, filter_context=self._filter_context):
            self._out.append(self._rebase(match, base, memo))

    def _emit_match(self, value: Any, parts: Tuple[PathPart, ...], path: str) -> None:
        self._out.append(
            JSONPathMatch(
                filter_context=self._filter_context,
                obj=value,
                parent=None,
                path=path,
                parts=parts,
                root=value,
            )
        )

    def _rebase(
        self,
        match: JSONPathMatch,
        base: JSONPathMatch,
        memo: Dict[int, JSONPathMatch],
    ) -> JSONPathMatch:
        if match.parent is None:
            return base
        parent = memo.get(id(match.parent))
        if parent is None:
            parent = self._rebase(match.parent, base, memo)
            memo[id(match.parent)] = parent
        return JSONPathMatch(
            filter_context=self._filter_context,
            obj=match.obj,
            parent=parent,
            parts=base.parts + match.parts,
            path=base.path + match.path[len(self._env.root_token) :],
            root=base.root,
        )

    @staticmethod
    def _builder_add(builders: List[List[Any]], value: Any) -> None:
        top = builders[-1]
        if top[0] == "array":
            top[1].append(value)
        else:
            top[1][top[2]] = value
            top[2] = None


def _value_done(stack: List[List[Any]]) -> None:
    if stack and stack[-1][0] == "object" and stack[-1][1] == "expect_value":
        stack[-1][1] = "expect_comma"
