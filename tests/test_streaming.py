import json
from typing import Iterator
from typing import List

import pytest

import jsonpath
from jsonpath import JSONPathStreamError
from jsonpath import JSONStreamBufferError
from jsonpath import JSONStreamDecodeError


def chunked(data: bytes, size: int = 1) -> List[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


def test_filter_query_matches_finditer() -> None:
    data = [
        {"name": "a", "score": 1},
        {"name": "b", "score": 5},
        {"name": "c", "score": 3},
    ]
    doc = json.dumps(data).encode()
    query = jsonpath.compile_stream("$[?@.score > 2].name")
    matches = list(query.finditer([doc]))
    assert [m.obj for m in matches] == ["b", "c"]
    assert [m.path for m in matches] == [
        "$[1]['name']",
        "$[2]['name']",
    ]
    assert [m.parts for m in matches] == [(1, "name"), (2, "name")]


@pytest.mark.parametrize(
    "path",
    [
        "$[*]",
        "$[0]",
        "$[2]",
        "$[1:4]",
        "$[1:5:2]",
        "$[::2]",
        "$[:3]",
        "$[2:]",
        "$[*].x",
        "$[?@ > 2]",
        "$[?@.x == 2]",
    ],
)
def test_array_queries_match_finditer(path: str) -> None:
    data = [{"x": 1}, {"x": 2}, {"x": 3}, {"x": 2}, {"x": 5}]
    doc = json.dumps(data).encode()
    expected = [(m.obj, m.path) for m in jsonpath.finditer(path, data)]
    for size in (1, 7, len(doc)):
        query = jsonpath.compile_stream(path)
        matches = list(query.finditer(chunked(doc, size)))
        assert [(m.obj, m.path) for m in matches] == expected


@pytest.mark.parametrize(
    "path",
    [
        "$.a.b.c",
        "$.a.b[*]",
        "$.a.b[1].c",
        "$.a.b[0:2].c",
        "$.a[?@.c > 1].d",
        "$['a']['b']['c']",
    ],
)
def test_object_queries_match_finditer(path: str) -> None:
    data = {
        "a": {
            "b": [
                {"c": 1, "d": "one"},
                {"c": 2, "d": "two"},
                {"c": 3, "d": "three"},
            ]
        }
    }
    doc = json.dumps(data).encode()
    expected = [(m.obj, m.path) for m in jsonpath.finditer(path, data)]
    for size in (1, 5, len(doc)):
        query = jsonpath.compile_stream(path)
        matches = list(query.finditer(chunked(doc, size)))
        assert [(m.obj, m.path) for m in matches] == expected


def test_scalar_candidates() -> None:
    data = [1, 2, 3, 4]
    doc = json.dumps(data).encode()
    query = jsonpath.compile_stream("$[?@ > 2]")
    matches = list(query.finditer(chunked(doc, 3)))
    assert [m.obj for m in matches] == [3, 4]
    assert [m.path for m in matches] == ["$[2]", "$[3]"]


def test_filter_field_can_appear_after_selected_field() -> None:
    doc = json.dumps([{"a": 1, "z": 99}, {"a": 2, "z": 1}]).encode()
    query = jsonpath.compile_stream("$[?@.z > 50].a")
    matches = list(query.finditer(chunked(doc, 4)))
    assert [m.obj for m in matches] == [1]
    assert [m.path for m in matches] == ["$[0]['a']"]


def test_first_match_before_input_is_consumed() -> None:
    data = [{"id": i, "pad": "x" * 100} for i in range(100)]
    doc = json.dumps(data).encode()
    consumed = 0

    def gen() -> Iterator[bytes]:
        nonlocal consumed
        for i in range(0, len(doc), 16):
            consumed += 1
            yield doc[i : i + 16]

    query = jsonpath.compile_stream("$[0]")
    it = query.finditer(gen())
    match = next(it)
    assert match.obj == data[0]
    assert match.path == "$[0]"
    assert consumed < len(doc) // 16


def test_does_not_yield_partially_parsed_match() -> None:
    doc = b'[{"ok": true}, {"ok": tru'
    query = jsonpath.compile_stream("$[?@.ok]")
    matches = []
    with pytest.raises(JSONStreamDecodeError):
        matches.extend(query.finditer([doc]))
    assert len(matches) == 1
    assert matches[0].obj == {"ok": True}
    assert matches[0].path == "$[0]"


def test_decode_error_reports_byte_position() -> None:
    doc = b'[{"a": 1}, {"a": 2}, {"a": ]}'
    query = jsonpath.compile_stream("$[*]")
    matches = []
    with pytest.raises(JSONStreamDecodeError) as exc_info:
        matches.extend(query.finditer(chunked(doc, 3)))
    assert len(matches) == 2  # noqa: PLR2004
    assert exc_info.value.position == doc.index(b"]}")
    assert str(exc_info.value.position) in str(exc_info.value)


def test_utf8_and_escapes_across_chunks() -> None:
    data = [{"s": "héllo 😀", "t": "\t\né"}]
    doc = json.dumps(data, ensure_ascii=False).encode()
    query = jsonpath.compile_stream("$[*].s")
    matches = list(query.finditer(chunked(doc, 1)))
    assert [m.obj for m in matches] == ["héllo 😀"]
    query = jsonpath.compile_stream("$[*].t")
    matches = list(query.finditer(chunked(doc, 1)))
    assert [m.obj for m in matches] == ["\t\né"]


def test_unicode_escape_split_across_chunks() -> None:
    doc = b'["\\u00e9", "\\ud83d\\ude00"]'
    query = jsonpath.compile_stream("$[*]")
    matches = list(query.finditer(chunked(doc, 1)))
    assert [m.obj for m in matches] == ["é", "\U0001f600"]


def test_buffer_limit() -> None:
    doc = json.dumps([{"big": "x" * 1000}, {"big": "y"}]).encode()
    query = jsonpath.compile_stream("$[?@.big]")
    with pytest.raises(JSONStreamBufferError) as exc_info:
        list(query.finditer([doc], max_buffer_size=100))
    assert exc_info.value.limit == 100  # noqa: PLR2004
    # The same query succeeds with a sufficiently large buffer.
    matches = list(query.finditer([doc], max_buffer_size=2048))
    assert len(matches) == 2  # noqa: PLR2004


def test_buffer_limit_applies_to_single_string() -> None:
    doc = json.dumps([{"big": "x" * 1000}]).encode()
    query = jsonpath.compile_stream("$[*]")
    with pytest.raises(JSONStreamBufferError):
        list(query.finditer(chunked(doc, 8), max_buffer_size=100))


@pytest.mark.parametrize(
    "path",
    [
        "$..a",
        "$[-1]",
        "$[::-1]",
        "$[5:1:-1]",
        "$[-2:]",
        "$[:-1]",
        "$[?@.x > $.max]",
        "$[?@.x][0, 1]",
        "$['a', 'b']",
        "$.a | $.b",
    ],
)
def test_unsupported_queries_are_rejected_at_compile_time(path: str) -> None:
    with pytest.raises(JSONPathStreamError):
        jsonpath.compile_stream(path)


def test_unsupported_query_does_not_consume_input() -> None:
    consumed = False

    def gen() -> Iterator[bytes]:
        nonlocal consumed
        consumed = True
        yield b"[1, 2, 3]"

    with pytest.raises(JSONPathStreamError):
        jsonpath.compile_stream("$[?@.x > $.max]")
    assert not consumed


def test_single_bytes_input_and_empty_chunks() -> None:
    doc = json.dumps([{"a": 1}, {"a": 2}]).encode()
    query = jsonpath.compile_stream("$[*].a")
    matches = list(query.finditer(doc))
    assert [m.obj for m in matches] == [1, 2]
    chunks = [b"", doc[:5], b"", doc[5:], b""]
    matches = list(query.finditer(chunks))
    assert [m.obj for m in matches] == [1, 2]


def test_root_scalar_and_whole_document() -> None:
    query = jsonpath.compile_stream("$")
    assert [m.obj for m in query.finditer([b"42"])] == [42]
    assert [m.obj for m in query.finditer([b"true"])] == [True]
    assert [m.obj for m in query.finditer([b"null"])] == [None]
    assert [m.obj for m in query.finditer([b"[1, 2]"])] == [[1, 2]]
    assert [m.obj for m in query.finditer([b' {"a": 1} '])] == [{"a": 1}]


def test_findall() -> None:
    doc = json.dumps([{"a": 1}, {"a": 2}]).encode()
    query = jsonpath.compile_stream("$[*].a")
    assert query.findall(chunked(doc, 2)) == [1, 2]


def test_strict_mode() -> None:
    doc = json.dumps([{"a": 1}, {"a": 2}]).encode()
    query = jsonpath.compile_stream("$[*].a", strict=True)
    assert [m.obj for m in query.finditer(chunked(doc, 3))] == [1, 2]


def test_trailing_data_is_an_error() -> None:
    query = jsonpath.compile_stream("$[*]")
    with pytest.raises(JSONStreamDecodeError) as exc_info:
        list(query.finditer([b"[1, 2] x"]))
    assert exc_info.value.position == 7  # noqa: PLR2004


def test_empty_input_is_an_error() -> None:
    query = jsonpath.compile_stream("$[*]")
    with pytest.raises(JSONStreamDecodeError):
        list(query.finditer([b"  "]))


def test_filter_on_object_values() -> None:
    data = {"a": {"n": 1}, "b": {"n": 5}}
    doc = json.dumps(data).encode()
    query = jsonpath.compile_stream("$[?@.n > 2]")
    matches = list(query.finditer(chunked(doc, 4)))
    assert [m.obj for m in matches] == [{"n": 5}]
    assert [m.path for m in matches] == ["$['b']"]


def test_match_parent_and_root() -> None:
    doc = json.dumps([{"a": {"b": 1}}]).encode()
    query = jsonpath.compile_stream("$[?@.a].a.b")
    matches = list(query.finditer([doc]))
    assert len(matches) == 1
    assert matches[0].obj == 1
    assert matches[0].path == "$[0]['a']['b']"
    assert matches[0].parent is not None
    assert matches[0].parent.path == "$[0]['a']"
    assert matches[0].parent.parent is not None
    assert matches[0].parent.parent.path == "$[0]"


def test_slice_applies_to_strings() -> None:
    data = ["héllo", 2.5, []]
    doc = json.dumps(data, ensure_ascii=False).encode()
    path = "$[0][1:]"
    expected = [(m.obj, m.path) for m in jsonpath.finditer(path, data)]
    matches = list(jsonpath.compile_stream(path).finditer(chunked(doc, 2)))
    assert [(m.obj, m.path) for m in matches] == expected
    assert matches[0].obj == "é"
    assert matches[0].path == "$[0][1]"


def test_multiple_filter_segments() -> None:
    data = [
        {"enabled": True, "items": [{"ok": 1}, {"ok": 0}]},
        {"enabled": False, "items": [{"ok": 2}]},
    ]
    doc = json.dumps(data).encode()
    path = "$[?@.enabled == true].items[?@.ok > 0]"
    expected = [(m.obj, m.path) for m in jsonpath.finditer(path, data)]
    matches = list(jsonpath.compile_stream(path).finditer(chunked(doc, 5)))
    assert [(m.obj, m.path) for m in matches] == expected
    assert [m.path for m in matches] == ["$[0]['items'][0]"]


def test_deeply_nested_document() -> None:
    depth = 5000
    doc = b"[" * depth + b"1" + b"]" * depth
    query = jsonpath.compile_stream("$")
    matches = list(query.finditer([doc]))
    assert len(matches) == 1


def test_match_root_is_the_buffered_item() -> None:
    doc = json.dumps([{"a": 1}]).encode()
    query = jsonpath.compile_stream("$[?@.a]")
    matches = list(query.finditer([doc]))
    assert matches[0].root == {"a": 1}
