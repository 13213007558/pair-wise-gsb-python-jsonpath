"""Snapshot-based update test cases."""

import copy
import re
from typing import cast

import pytest

import jsonpath
from jsonpath import REMOVE
from jsonpath import ConflictStrategy
from jsonpath import JSONPathEnvironment
from jsonpath import JSONPathMatch
from jsonpath import JSONPathUpdateError
from jsonpath import update


def test_remove_consecutive_array_items() -> None:
    data = {
        "items": [
            {"id": 0, "expired": True},
            {"id": 1, "expired": True},
            {"id": 2, "expired": True},
            {"id": 3, "expired": False},
            {"id": 4, "expired": False},
            {"id": 5, "expired": False},
        ]
    }
    result = update("$..items[?@.expired == true]", data, remove=True)
    assert result.data == {
        "items": [
            {"id": 3, "expired": False},
            {"id": 4, "expired": False},
            {"id": 5, "expired": False},
        ]
    }
    assert result.patch is None


def test_remove_scattered_array_items() -> None:
    data = [0, 1, 2, 3, 4, 5]
    result = update("$[1, 2, 4]", data, remove=True)
    assert result.data == [0, 3, 5]


def test_remove_all_array_items() -> None:
    data = {"a": [1, 2, 3], "b": [4, 5]}
    result = update("$[*][*]", data, remove=True)
    assert result.data == {"a": [], "b": []}


def test_callback_replace() -> None:
    data = {"items": [{"price": 1.5}, {"price": 2.0}]}
    result = update(
        "$.items[*].price", data, lambda match: cast(float, match.value) * 2
    )
    assert result.data == {"items": [{"price": 3.0}, {"price": 4.0}]}


def test_callback_receives_match() -> None:
    data = {"a": [{"b": 1}]}
    seen = []

    def callback(match: JSONPathMatch) -> object:
        seen.append((match.path, match.parts, match.value))
        return match.value

    update("$.a[0].b", data, callback)
    assert seen == [("$['a'][0]['b']", ("a", 0, "b"), 1)]


def test_callback_returning_remove_sentinel() -> None:
    data = [0, 1, 2, 3, 4]
    result = update(
        "$[*]",
        data,
        lambda match: REMOVE
        if cast(int, match.value) % 2
        else cast(int, match.value) * 10,
    )
    assert result.data == [0, 20, 40]


def test_duplicate_hits_modify_once() -> None:
    data = {"a": 1}
    calls = []

    def callback(match: JSONPathMatch) -> object:
        calls.append(match.parts)
        return cast(int, match.value) + 1

    result = update(["$.a", "$.a", "$['a']"], data, callback)
    assert result.data == {"a": 2}
    assert calls == [("a",)]


def test_union_duplicate_hits_modify_once() -> None:
    data = {"a": 1}
    calls = []

    def callback(match: JSONPathMatch) -> object:
        calls.append(match.parts)
        return cast(int, match.value) + 1

    result = update("$['a','a']", data, callback)
    assert result.data == {"a": 2}
    assert calls == [("a",)]


def test_ancestor_conflict_raises_by_default() -> None:
    data = {"a": {"b": 1}}
    with pytest.raises(JSONPathUpdateError, match="conflicting update targets"):
        update(["$.a", "$.a.b"], data, remove=True)
    assert data == {"a": {"b": 1}}


def test_ancestor_conflict_shallow() -> None:
    data = {"a": {"b": 1, "c": 2}, "d": 3}
    result = update(
        ["$.a", "$.a.b"], data, remove=True, conflict=ConflictStrategy.SHALLOW
    )
    assert result.data == {"d": 3}


def test_ancestor_conflict_deep() -> None:
    data = {"a": {"b": 1, "c": 2}, "d": 3}
    result = update(
        ["$.a", "$.a.b"], data, remove=True, conflict=ConflictStrategy.DEEP
    )
    assert result.data == {"a": {"c": 2}, "d": 3}


def test_ancestor_conflict_deep_with_callback() -> None:
    data = {"a": {"b": 1}}
    result = update(
        ["$.a", "$.a.b"],
        data,
        lambda match: match.value + 1
        if isinstance(match.value, int)
        else match.value,
        conflict=ConflictStrategy.DEEP,
    )
    assert result.data == {"a": {"b": 2}}


def test_callback_failure_leaves_data_unchanged() -> None:
    data = {"items": [1, 2, 3]}
    snapshot = copy.deepcopy(data)

    def callback(match: JSONPathMatch) -> object:
        if match.value == snapshot["items"][1]:
            raise ValueError("boom")
        return cast(int, match.value) * 10

    with pytest.raises(ValueError, match="boom"):
        update("$.items[*]", data, callback)
    assert data == snapshot


def test_conflicting_callback_failure_leaves_data_unchanged() -> None:
    data = {"a": {"b": 1}}

    def callback(match: JSONPathMatch) -> object:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        update(
            ["$.a", "$.a.b"],
            data,
            callback,
            conflict=ConflictStrategy.SHALLOW,
        )
    assert data == {"a": {"b": 1}}


def test_numeric_string_keys_are_not_confused_with_indices() -> None:
    data = {"0": "zero", "arr": ["a", "b"]}
    result = update("$['0']", data, lambda match: "ZERO")
    assert result.data == {"0": "ZERO", "arr": ["a", "b"]}

    result = update("$.arr[0]", data, lambda match: "A")
    assert result.data == {"0": "ZERO", "arr": ["A", "b"]}


def test_keys_with_slashes_and_tildes() -> None:
    data = {"a/b": 1, "c~d": 2, "e~0f": 3, "g~1h": 4}
    result = update(
        ["$['a/b']", "$['c~d']"], data, lambda match: cast(int, match.value) * 10
    )
    assert result.data == {"a/b": 10, "c~d": 20, "e~0f": 3, "g~1h": 4}


def test_keys_with_slashes_and_tildes_in_patch() -> None:
    data = {"a/b": 1, "c~d": 2}
    original = copy.deepcopy(data)
    result = update("$['a/b']", data, lambda match: 10, patch=True)
    assert result.data == {"a/b": 10, "c~d": 2}
    assert result.patch is not None
    assert result.patch.asdicts() == [
        {"op": "replace", "path": "/a~1b", "value": 10}
    ]
    assert result.patch.apply(original) == result.data


def test_patch_round_trip_consecutive_removes() -> None:
    data = {"items": [0, 1, 2, 3, 4, 5]}
    original = copy.deepcopy(data)
    result = update("$..items[?@ < 3]", data, remove=True, patch=True)
    assert result.data == {"items": [3, 4, 5]}
    assert result.patch is not None
    assert result.patch.asdicts() == [
        {"op": "remove", "path": "/items/2"},
        {"op": "remove", "path": "/items/1"},
        {"op": "remove", "path": "/items/0"},
    ]
    assert result.patch.apply(original) == result.data


def test_patch_round_trip_mixed_remove_and_replace() -> None:
    data = [0, 1, 2, 3, 4]
    original = copy.deepcopy(data)
    result = update(
        "$[*]",
        data,
        lambda match: REMOVE
        if cast(int, match.value) % 2
        else cast(int, match.value) * 10,
        patch=True,
    )
    assert result.data == [0, 20, 40]
    assert result.patch is not None
    assert result.patch.apply(original) == result.data


def test_patch_round_trip_remove_shifts_nested_target() -> None:
    data = {"a": [{"x": 1}, {"x": 2}, {"x": 3}]}
    original = copy.deepcopy(data)

    def callback(match: JSONPathMatch) -> object:
        if match.parts == ("a", 0):
            return REMOVE
        return {"x": 30}

    result = update(["$.a[0]", "$.a[2]"], data, callback, patch=True)
    assert result.data == {"a": [{"x": 2}, {"x": 30}]}
    assert result.patch is not None
    assert result.patch.apply(original) == result.data


def test_replace_root() -> None:
    data = {"a": 1}
    result = update("$", data, lambda match: {"b": 2}, patch=True)
    assert result.data == {"b": 2}
    assert result.patch is not None
    assert result.patch.asdicts() == [{"op": "replace", "path": "", "value": {"b": 2}}]
    assert result.patch.apply({"a": 1}) == {"b": 2}


def test_remove_root_raises() -> None:
    data = {"a": 1}
    with pytest.raises(JSONPathUpdateError, match="can't remove the document root"):
        update("$", data, remove=True)
    assert data == {"a": 1}


def test_remove_root_via_callback_raises() -> None:
    data = {"a": 1}
    with pytest.raises(JSONPathUpdateError, match="can't remove the document root"):
        update("$", data, lambda match: REMOVE)
    assert data == {"a": 1}


def test_missing_callback_and_remove() -> None:
    with pytest.raises(
        JSONPathUpdateError, match=re.escape("expected a callback or remove=True")
    ):
        update("$.a", {"a": 1})


def test_callback_and_remove_are_mutually_exclusive() -> None:
    with pytest.raises(JSONPathUpdateError, match="not both"):
        update("$.a", {"a": 1}, lambda match: 2, remove=True)


def test_immutable_container_raises_and_leaves_data_unchanged() -> None:
    data = {"a": (1, 2, 3)}
    with pytest.raises(JSONPathUpdateError, match="expected a mutable"):
        update("$.a[*]", data, remove=True)
    assert data == {"a": (1, 2, 3)}


def test_no_matches() -> None:
    data = {"a": 1}
    result = update("$.b", data, remove=True, patch=True)
    assert result.data == {"a": 1}
    assert result.patch is not None
    assert result.patch.asdicts() == []


def test_update_from_string_data() -> None:
    result = update("$.a", '{"a": 1}', lambda match: 2)
    assert result.data == {"a": 2}


def test_environment_update_method() -> None:
    env = JSONPathEnvironment()
    data = {"items": [1, 2, 3]}
    result = env.update("$.items[*]", data, lambda match: cast(int, match.value) + 1)
    assert result.data == {"items": [2, 3, 4]}


def test_module_level_strict() -> None:
    data = {"a": [1, 2]}
    result = jsonpath.update("$.a[0]", data, lambda match: 10, strict=True)
    assert result.data == {"a": [10, 2]}


def test_filter_context() -> None:
    data = {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}
    result = update(
        "$.items[?@.id > _.limit]",
        data,
        remove=True,
        filter_context={"limit": 1},
    )
    assert result.data == {"items": [{"id": 1}]}


def test_updates_are_independent_of_query_order() -> None:
    # The same initial target set, queried in different orders, must yield
    # the same result.
    paths = ["$.a[0]", "$.a[1]", "$.a[2]"]
    first = update(paths, {"a": [1, 2, 3]}, remove=True)
    second = update(list(reversed(paths)), {"a": [1, 2, 3]}, remove=True)
    assert first.data == second.data == {"a": []}
