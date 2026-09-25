import asyncio
from typing import Dict
from typing import Iterator
from typing import List
from typing import Mapping
from typing import Sequence
from typing import Union
from typing import overload

import pytest

import jsonpath
from jsonpath import JSONPathBatchError
from jsonpath import JSONPathEnvironment
from jsonpath import JSONPathError


class CountingMap(Mapping[str, object]):
    def __init__(self, data: Dict[str, object]) -> None:
        self._data = data
        self.get_count = 0

    def __getitem__(self, key: str) -> object:
        self.get_count += 1
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


class CountingList(Sequence[object]):
    def __init__(self, data: List[object]) -> None:
        self._data = data
        self.get_count = 0

    @overload
    def __getitem__(self, index: int) -> object: ...

    @overload
    def __getitem__(self, index: slice) -> List[object]: ...

    def __getitem__(self, index: Union[int, slice]) -> object:
        self.get_count += 1
        if isinstance(index, slice):
            return self._data[index]
        return self._data[index]

    def __len__(self) -> int:
        return len(self._data)


class AsyncCountingMap(Mapping[str, object]):
    def __init__(self, data: Dict[str, object]) -> None:
        self._data = data
        self.get_count = 0
        self.async_get_count = 0

    def __getitem__(self, key: str) -> object:
        self.get_count += 1
        return self._data[key]

    async def __getitem_async__(self, key: str) -> object:
        self.async_get_count += 1
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def make_orders_doc() -> Dict[str, object]:
    return {
        "orders": [
            {
                "id": 1,
                "lines": [
                    {"price": 10, "sku": "a", "qty": 1},
                    {"price": 20, "sku": "b", "qty": 2},
                ],
            },
            {"id": 2, "lines": [{"price": 10, "sku": "c", "qty": 3}]},
            {"id": 3, "lines": []},
        ],
        "currency": "USD",
    }


def make_twenty_queries() -> Dict[str, str]:
    queries = {
        "prices": "$.orders[*].lines[*].price",
        "skus": "$.orders[*].lines[*].sku",
        "qtys": "$.orders[*].lines[*].qty",
        "order_ids": "$.orders[*].id",
        "first_price": "$.orders[0].lines[0].price",
        "expensive_skus": "$.orders[*].lines[?@.price > 15].sku",
        "cheap_prices": "$.orders[*].lines[?@.price < 15].price",
        "priced_skus": "$.orders[*].lines[?length(@.sku) > 0].sku",
        "priced_skus_again": "$.orders[*].lines[?length(@.sku) > 0].sku",
        "dup_in_segment": "$.orders[*].lines[*]['price', 'price']",
        "currency": "$.currency",
        "everything": "$..price",
    }
    for i in range(8):
        queries[f"prices_again_{i}"] = "$.orders[*].lines[*].price"
    assert len(queries) == 20
    return queries


def test_batch_results_match_individual_execution() -> None:
    data = make_orders_doc()
    queries = make_twenty_queries()
    plan = jsonpath.compile_many(queries)
    batch_matches = plan.finditer(data)
    batch_values = plan.findall(data)

    assert set(batch_matches) == set(queries)
    for name, query in queries.items():
        expected = list(jsonpath.compile(query).finditer(data))
        # Same order, same duplicates and same normalized paths.
        assert [m.obj for m in batch_matches[name]] == [m.obj for m in expected]
        assert [m.path for m in batch_matches[name]] == [m.path for m in expected]
        assert batch_values[name] == [m.obj for m in expected]


def test_shared_prefix_is_resolved_once() -> None:
    def build() -> List[CountingMap]:
        lines1 = CountingList(
            [
                CountingMap({"price": 10}),
                CountingMap({"price": 20}),
            ]
        )
        lines2 = CountingList([CountingMap({"price": 30})])
        order1 = CountingMap({"id": 1, "lines": lines1})
        order2 = CountingMap({"id": 2, "lines": lines2})
        root = CountingMap({"orders": CountingList([order1, order2])})
        return [root, order1, order2]

    queries = {f"q{i}": "$.orders[*].lines[*].price" for i in range(20)}

    batch_maps = build()
    plan = jsonpath.compile_many(queries)
    results = plan.findall(batch_maps[0])

    # The shared prefix is resolved once, not once per query.
    assert batch_maps[0].get_count == 1
    assert batch_maps[1].get_count == 1
    assert batch_maps[2].get_count == 1

    solo_maps = build()
    expected = {}
    for name, query in queries.items():
        expected[name] = jsonpath.findall(query, solo_maps[0])
    assert solo_maps[0].get_count == 20  # noqa: PLR2004
    assert solo_maps[1].get_count == 20  # noqa: PLR2004
    assert solo_maps[2].get_count == 20  # noqa: PLR2004

    assert results == expected


def test_repeated_selectors_within_a_query_are_not_optimized_away() -> None:
    data: Dict[str, object] = {"a": {"a": {"b": 1}}}
    queries = {
        "single": "$.a",
        "double": "$.a.a",
        "double_b": "$.a.a.b",
        "double_b_again": "$.a.a.b",
    }
    plan = jsonpath.compile_many(queries)
    results = plan.findall(data)
    for name, query in queries.items():
        assert results[name] == jsonpath.findall(query, data)
    # If the repeated `.a` selector had been collapsed, `double` would
    # return the inner {"a": {"b": 1}} mapping instead of {"b": 1}.
    assert results["double"] == [{"b": 1}]
    assert results["double_b"] == [1]
    assert results["double_b_again"] == [1]


def test_impure_function_is_evaluated_once_per_query() -> None:
    env = JSONPathEnvironment()
    calls: List[object] = []

    def tracked(value: object) -> bool:
        calls.append(value)
        return True

    env.function_extensions["tracked"] = tracked
    data = {"items": [{"x": 1}, {"x": 2}, {"x": 3}]}
    queries = {
        "first": "$.items[?tracked(@.x)].x",
        "second": "$.items[?tracked(@.x)].x",
    }

    plan = env.compile_many(queries)
    results = plan.findall(data)
    assert results == {"first": [1, 2, 3], "second": [1, 2, 3]}
    # Not declared pure, so the filter is evaluated once per query, exactly
    # as if each query had been executed individually.
    assert len(calls) == 6  # noqa: PLR2004


def test_pure_function_evaluation_is_shared() -> None:
    env = JSONPathEnvironment()
    calls: List[object] = []

    def tracked(value: object) -> bool:
        calls.append(value)
        return True

    tracked.pure = True  # type: ignore[attr-defined]
    env.function_extensions["tracked"] = tracked
    data = {"items": [{"x": 1}, {"x": 2}, {"x": 3}]}
    queries = {
        "first": "$.items[?tracked(@.x)].x",
        "second": "$.items[?tracked(@.x)].x",
    }

    plan = env.compile_many(queries)
    results = plan.findall(data)
    assert results == {"first": [1, 2, 3], "second": [1, 2, 3]}
    # Declared pure, so the shared filter segment is evaluated just once.
    assert len(calls) == 3  # noqa: PLR2004


def test_runtime_error_is_attributed_to_query_name() -> None:
    env = JSONPathEnvironment()

    def boom(_value: object) -> bool:
        raise ValueError("boom")

    env.function_extensions["boom"] = boom
    plan = env.compile_many(
        {
            "ok": "$.items[*].x",
            "bad": "$.items[?boom(@.x)].x",
        }
    )
    with pytest.raises(JSONPathBatchError) as exc_info:
        plan.findall({"items": [{"x": 1}, {"x": 2}]})

    err = exc_info.value
    assert err.name == "bad"
    assert "bad" in str(err)
    assert isinstance(err.errors["bad"], ValueError)
    assert isinstance(err.__cause__, ValueError)
    # Queries that did not fail still have their results available.
    assert [m.obj for m in err.results["ok"]] == [1, 2]


def test_compile_error_is_attributed_to_query_name() -> None:
    with pytest.raises(JSONPathBatchError) as exc_info:
        jsonpath.compile_many({"good": "$.a", "bad": "$["})
    err = exc_info.value
    assert err.name == "bad"
    assert isinstance(err.errors["bad"], JSONPathError)


def test_duplicate_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        jsonpath.compile_many([("a", "$.x"), ("a", "$.y")])


def test_compound_queries_match_individual_execution() -> None:
    data = {"a": [1, 2, 3], "b": [2, 3, 4], "c": {"x": 9}}
    queries = {
        "union": "$.a[*] | $.b[*]",
        "intersection": "$.a[*] & $.b[*]",
        "mixed": "$.a[*] | $.c.x",
    }
    plan = jsonpath.compile_many(queries)
    results = plan.findall(data)
    for name, query in queries.items():
        assert results[name] == jsonpath.findall(query, data)


def test_plan_is_reusable_across_documents() -> None:
    plan = jsonpath.compile_many({"prices": "$.orders[*].lines[*].price"})
    doc1 = {"orders": [{"lines": [{"price": 1}]}]}
    doc2 = {"orders": [{"lines": [{"price": 2}, {"price": 3}]}]}

    assert plan.findall(doc1) == {"prices": [1]}
    assert plan.findall(doc2) == {"prices": [2, 3]}
    # No residual matches from previous executions.
    assert plan.findall(doc1) == {"prices": [1]}

    run1 = plan.finditer(doc1)
    run2 = plan.finditer(doc1)
    assert run1["prices"][0] is not run2["prices"][0]
    assert run1["prices"][0].path == run2["prices"][0].path


def test_async_shared_prefix() -> None:
    order1 = AsyncCountingMap({"lines": [{"price": 10}, {"price": 20}]})
    order2 = AsyncCountingMap({"lines": [{"price": 30}]})
    root = AsyncCountingMap({"orders": [order1, order2]})
    queries = {f"q{i}": "$.orders[*].lines[*].price" for i in range(5)}

    async def coro() -> Dict[str, List[object]]:
        plan = jsonpath.compile_many(queries)
        return await plan.findall_async(root)

    results = asyncio.run(coro())

    assert root.async_get_count == 1
    assert order1.async_get_count == 1
    assert order2.async_get_count == 1
    # The sync accessor is never used by the async API.
    assert root.get_count == 0
    for name in queries:
        assert results[name] == [10, 20, 30]


def test_async_results_match_individual_execution() -> None:
    data = make_orders_doc()
    queries = make_twenty_queries()

    async def coro() -> Dict[str, List[object]]:
        plan = jsonpath.compile_many(queries)
        results = await plan.findall_async(data)
        for name, query in queries.items():
            assert results[name] == await jsonpath.findall_async(query, data)
        return results

    asyncio.run(coro())
