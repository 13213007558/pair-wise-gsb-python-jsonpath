import copy

import pytest

import jsonpath
from jsonpath import ConflictStrategy
from jsonpath import JSONPathTransformConflictError
from jsonpath import JSONPathTransformError
from jsonpath import TransformResult


def test_delete_consecutive_array_items() -> None:
    """Consecutive removals from the same array use initial indices."""
    data = {
        "items": [
            {"id": 1, "expired": True},
            {"id": 2},
            {"id": 3, "expired": True},
            {"id": 4, "expired": True},
            {"id": 5},
        ]
    }
    result = jsonpath.transform("$..items[?@.expired]", data, delete=True)
    assert result.data == {"items": [{"id": 2}, {"id": 5}]}
    assert result.matched == 3
    assert result.modified == 3
    # The input document is modified in place.
    assert data == {"items": [{"id": 2}, {"id": 5}]}


def test_delete_all_array_items() -> None:
    data = {"items": [1, 2, 3]}
    result = jsonpath.transform("$.items[*]", data, delete=True)
    assert result.data == {"items": []}


def test_delete_is_order_independent() -> None:
    """The result does not depend on the order matches were found in."""
    data = {"a": [0, 1, 2, 3, 4], "b": [0, 1, 2, 3, 4]}
    result = jsonpath.transform("$.*[?@ > 0 && @ < 4]", data, delete=True)
    assert result.data == {"a": [0, 4], "b": [0, 4]}


def test_replace_with_callback() -> None:
    data = {"items": [1, 2, 3]}
    result = jsonpath.transform(
        "$.items[*]", data, lambda match: match.obj * 2  # type: ignore
    )
    assert result.data == {"items": [2, 4, 6]}


def test_callback_sees_original_document() -> None:
    """All callbacks run before any modification is committed."""
    data = {"items": [1, 2, 3]}
    seen = []

    def callback(match: jsonpath.JSONPathMatch) -> object:
        seen.append(match.obj)
        # The root document is unmodified at callback time.
        assert match.root == {"items": [1, 2, 3]}
        return match.obj

    jsonpath.transform("$.items[*]", data, callback)
    assert seen == [1, 2, 3]


def test_duplicate_matches_are_modified_once() -> None:
    """A node matched by multiple selectors is modified once only."""
    data = {"a": [1]}
    calls = []

    def callback(match: jsonpath.JSONPathMatch) -> object:
        calls.append(match.parts)
        return 99

    result = jsonpath.transform("$.a[0] | $.a[0]", data, callback)
    assert result.data == {"a": [99]}
    assert calls == [("a", 0)]
    assert result.matched == 1
    assert result.modified == 1


def test_parent_and_child_match_raises_by_default() -> None:
    data = {"a": {"b": 1}}
    with pytest.raises(JSONPathTransformConflictError):
        jsonpath.transform("$.a | $.a.b", data, lambda match: None)

    with pytest.raises(JSONPathTransformConflictError):
        jsonpath.transform("$.a | $.a.b", data, delete=True)

    # The failed transform leaves the document unchanged.
    assert data == {"a": {"b": 1}}


def test_parent_and_child_match_parent_strategy() -> None:
    data = {"a": {"b": 1}, "c": 2}
    result = jsonpath.transform(
        "$.a | $.a.b",
        data,
        lambda match: "replaced",
        conflict=ConflictStrategy.PARENT,
    )
    assert result.data == {"a": "replaced", "c": 2}
    assert result.matched == 2
    assert result.modified == 1


def test_parent_and_child_match_child_strategy() -> None:
    data = {"a": {"b": 1}, "c": 2}
    result = jsonpath.transform(
        "$.a | $.a.b",
        data,
        lambda match: "replaced",
        conflict=ConflictStrategy.CHILD,
    )
    assert result.data == {"a": {"b": "replaced"}, "c": 2}
    assert result.matched == 2
    assert result.modified == 1


def test_parent_and_child_delete_parent_strategy() -> None:
    """Deleting a parent drops the descendant target, so no error occurs."""
    data = {"a": {"b": 1}, "c": 2}
    result = jsonpath.transform(
        "$.a | $.a.b", data, delete=True, conflict=ConflictStrategy.PARENT
    )
    assert result.data == {"c": 2}


def test_parent_and_child_delete_child_strategy() -> None:
    data = {"a": {"b": 1}, "c": 2}
    result = jsonpath.transform(
        "$.a | $.a.b", data, delete=True, conflict=ConflictStrategy.CHILD
    )
    assert result.data == {"a": {}, "c": 2}


def test_failing_callback_leaves_document_unchanged() -> None:
    data = {"items": [1, 2, 3]}
    original = copy.deepcopy(data)

    def callback(match: jsonpath.JSONPathMatch) -> object:
        if match.obj == 2:
            raise ValueError("boom")
        return 0

    with pytest.raises(ValueError, match="boom"):
        jsonpath.transform("$.items[*]", data, callback)

    assert data == original


def test_numeric_string_keys_are_not_array_indices() -> None:
    """An object key \"0\" is not confused with array index 0."""
    data = {"0": "zero", "arr": ["zero"]}
    result = jsonpath.transform("$['0']", data, lambda match: "ZERO")
    assert result.data == {"0": "ZERO", "arr": ["zero"]}

    result = jsonpath.transform("$.arr[0]", data, lambda match: "ZERO")
    assert result.data == {"0": "ZERO", "arr": ["ZERO"]}


def test_keys_containing_slashes_and_tildes() -> None:
    data = {"a/b": 1, "c~d": 2, "a~1b": 3, "other": 4}
    result = jsonpath.transform(
        "$['a/b', 'c~d']", data, lambda match: 99, return_patch=True
    )
    assert result.data == {"a/b": 99, "c~d": 99, "a~1b": 3, "other": 4}

    assert result.patch is not None
    ops = result.patch.asdicts()
    assert [op["path"] for op in ops] == ["/a~1b", "/c~0d"]


def test_returned_patch_reproduces_result() -> None:
    """The optional RFC 6902 patch transforms the original document."""
    data = {
        "items": [
            {"id": 1, "expired": True},
            {"id": 2},
            {"id": 3, "expired": True},
            {"id": 4, "expired": True},
            {"id": 5},
        ]
    }
    original = copy.deepcopy(data)
    result = jsonpath.transform(
        "$..items[?@.expired]", data, delete=True, return_patch=True
    )

    assert result.patch is not None
    ops = result.patch.asdicts()
    # Plain RFC 6902 operations only.
    assert {op["op"] for op in ops} == {"remove"}
    # Removals from the same array are ordered highest index first.
    assert [op["path"] for op in ops] == ["/items/3", "/items/2", "/items/0"]

    patched = jsonpath.JSONPatch(ops).apply(copy.deepcopy(original))
    assert patched == result.data


def test_returned_patch_for_replacements() -> None:
    data = {"items": [1, 2, 3]}
    original = copy.deepcopy(data)
    result = jsonpath.transform(
        "$.items[*]",
        data,
        lambda match: match.obj * 10,  # type: ignore
        return_patch=True,
    )
    assert result.patch is not None
    ops = result.patch.asdicts()
    assert {op["op"] for op in ops} == {"replace"}
    patched = jsonpath.JSONPatch(ops).apply(copy.deepcopy(original))
    assert patched == result.data == {"items": [10, 20, 30]}


def test_no_patch_by_default() -> None:
    data = {"items": [1]}
    result = jsonpath.transform("$.items[*]", data, delete=True)
    assert result.patch is None


def test_replace_root() -> None:
    data = {"a": 1}
    result = jsonpath.transform("$", data, lambda match: [1, 2, 3])
    assert result.data == [1, 2, 3]


def test_remove_root_is_an_error() -> None:
    data = {"a": 1}
    with pytest.raises(JSONPathTransformError):
        jsonpath.transform("$", data, delete=True)
    assert data == {"a": 1}


def test_missing_callback_and_delete_is_an_error() -> None:
    with pytest.raises(JSONPathTransformError):
        jsonpath.transform("$.a", {"a": 1})


def test_callback_and_delete_are_mutually_exclusive() -> None:
    with pytest.raises(JSONPathTransformError):
        jsonpath.transform("$.a", {"a": 1}, lambda match: None, delete=True)


def test_no_matches_is_a_no_op() -> None:
    data = {"a": 1}
    result = jsonpath.transform("$.missing", data, delete=True, return_patch=True)
    assert result.data == {"a": 1}
    assert result.matched == 0
    assert result.modified == 0
    assert result.patch is not None
    assert result.patch.asdicts() == []


def test_transform_on_compiled_path() -> None:
    path = jsonpath.compile("$.items[?@.expired]")
    data = {"items": [{"expired": True}, {}]}
    result = path.transform(data, delete=True)
    assert isinstance(result, TransformResult)
    assert result.data == {"items": [{}]}


def test_transform_on_environment() -> None:
    env = jsonpath.JSONPathEnvironment()
    data = {"items": [1, 2, 3]}
    result = env.transform("$.items[0]", data, lambda match: 99)
    assert result.data == {"items": [99, 2, 3]}


def test_transform_strict() -> None:
    data = {"items": [1, 2, 3]}
    result = jsonpath.transform("$.items[0]", data, lambda match: 99, strict=True)
    assert result.data == {"items": [99, 2, 3]}


def test_transform_accepts_a_json_string() -> None:
    result = jsonpath.transform("$.items[*]", '{"items": [1, 2]}', delete=True)
    assert result.data == {"items": []}


def test_delete_at_different_depths_in_shared_array() -> None:
    """A removal can't shift the target of a deeper removal."""
    data = {"a": [0, {"del": True}, {"x": {"del": True}, "y": 1}]}
    original = copy.deepcopy(data)
    result = jsonpath.transform("$..[?@.del]", data, delete=True, return_patch=True)
    assert result.data == {"a": [0, {"y": 1}]}

    assert result.patch is not None
    # The deeper removal is applied first, so removing "a[1]" can't shift
    # the target of the removal at "a[2].x".
    assert [op["path"] for op in result.patch.asdicts()] == ["/a/2/x", "/a/1"]
    patched = jsonpath.JSONPatch(result.patch.asdicts()).apply(
        copy.deepcopy(original)
    )
    assert patched == result.data


def test_delete_from_root_array() -> None:
    data = [1, 2, 3, 4, 5]
    result = jsonpath.transform("$[?@ > 1 && @ < 5]", data, delete=True)
    assert result.data == [1, 5]
    assert data == [1, 5]


def test_delete_from_nested_arrays() -> None:
    data = {
        "items": [{"expired": True}, {"ok": True}],
        "sub": {"items": [{"expired": True}, {"ok": True}]},
    }
    result = jsonpath.transform("$..items[?@.expired]", data, delete=True)
    assert result.data == {
        "items": [{"ok": True}],
        "sub": {"items": [{"ok": True}]},
    }


def test_delete_with_union_and_duplicate_matches() -> None:
    data = {"a": [1, 2]}
    result = jsonpath.transform("$.a[0] | $.a[*]", data, delete=True, return_patch=True)
    assert result.data == {"a": []}
    assert result.patch is not None
    assert [op["path"] for op in result.patch.asdicts()] == ["/a/1", "/a/0"]


def test_key_looking_like_an_escape_sequence() -> None:
    data = {"a\\u0041b": 1}
    original = copy.deepcopy(data)
    result = jsonpath.transform(
        "$['a\\\\u0041b']", data, lambda match: 2, return_patch=True
    )
    assert result.data == {"a\\u0041b": 2}
    assert result.patch is not None
    # The patch is plain RFC 6902. We disable this library's non-standard
    # unicode escape decoding when re-parsing pointer strings.
    patched = jsonpath.JSONPatch(
        result.patch.asdicts(), unicode_escape=False
    ).apply(copy.deepcopy(original))
    assert patched == result.data


def test_empty_and_tilde_only_keys() -> None:
    data = {"": 1, "~": 2, "~0": 3}
    original = copy.deepcopy(data)
    result = jsonpath.transform("$['', '~']", data, lambda match: 9, return_patch=True)
    assert result.data == {"": 9, "~": 9, "~0": 3}
    assert result.patch is not None
    patched = jsonpath.JSONPatch(result.patch.asdicts()).apply(
        copy.deepcopy(original)
    )
    assert patched == result.data
