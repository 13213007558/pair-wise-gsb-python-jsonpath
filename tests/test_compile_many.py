"""Tests for `jsonpath.compile_many` and `QueryPlan`."""

import asyncio
from typing import Any
from typing import cast
from typing import Dict
from typing import Iterator
from typing import List
from typing import Mapping
from typing import overload
from typing import Sequence
from typing import Tuple

import pytest

import jsonpath
from jsonpath import BatchResult
from jsonpath import JSONPathBatchError
from jsonpath import JSONPathEnvironment
from jsonpath.exceptions import JSONPathSyntaxError
from jsonpath.function_extensions import ExpressionType
from jsonpath.function_extensions import FilterFunction

DATA: Mapping[str, object] = {
    "orders": [
        {
            "id": 1,
            "lines": [
                {"sku": "a", "qty": 1, "price": 10},
                {"sku": "b", "qty": 2, "price": 20},
            ],
        },
        {
            "id": 2,
            "lines": [
                {"sku": "c", "qty": 3, "price": 30},
            ],
        },
        {"id": 3, "lines": []},
    ],
    "meta": {"count": 3},
}


def twenty_queries() -> Dict[str, str]:
    """Twenty queries, most of which start with `$.orders[*].lines[*]`."""
    queries = {
        "skus": "$.orders[*].lines[*].sku",
        "qtys": "$.orders[*].lines[*].qty",
        "prices": "$.orders[*].lines[*].price",
        "lines": "$.orders[*].lines[*]",
        "order_ids": "$.orders[*].id",
        "all_orders": "$.orders[*]",
        "meta_count": "$.meta.count",
        "all_prices": "$..price",
        "first_line_sku": "$.orders[*].lines[0].sku",
        "root": "$",
    }
    for i in range(6):
        queries[f"qty_gt_{i}"] = f"$.orders[*].lines[?@.qty > {i}].sku"
    for i in range(4):
        queries[f"price_gt_{i}"] = f"$.orders[*].lines[?@.price > {i * 10}]"
    assert len(queries) == 20
    return queries


def test_compile_many_matches_individual_execution() -> None:
    queries = twenty_queries()
    plan = jsonpath.compile_many(queries)

    assert plan.names == tuple(queries)

    all_values = plan.findall(DATA)
    all_matches = plan.finditer(DATA)

    for name, path in queries.items():
        # Values, in order, including duplicates.
        assert all_values[name] == jsonpath.findall(path, DATA), name
        # Normalized paths, in order, including duplicates.
        assert all_matches[name].paths() == [
            match.path for match in jsonpath.finditer(path, DATA)
        ], name


def test_compile_many_strict() -> None:
    queries = {"skus": "$.orders[*].lines[*].sku", "count": "$.meta.count"}
    plan = jsonpath.compile_many(queries, strict=True)
    assert plan.findall(DATA)["count"] == [3]


def test_duplicate_selectors_are_not_optimized_away() -> None:
    queries = {
        "ids": "$.orders[*].id",
        "ids_twice": "$.orders[*]['id', 'id']",
        "sku_qty_sku": "$.orders[*].lines[*]['sku', 'qty', 'sku']",
        "same_a": "$.orders[*].lines[*].sku",
        "same_b": "$.orders[*].lines[*].sku",
    }
    plan = jsonpath.compile_many(queries)
    results = plan.finditer(DATA)

    for name, path in queries.items():
        assert results[name].paths() == [
            match.path for match in jsonpath.finditer(path, DATA)
        ], name

    # Repeated selectors within a query produce repeated matches.
    assert results["ids_twice"].values() == [1, 1, 2, 2, 3, 3]
    assert results["sku_qty_sku"].values()[:4] == ["a", 1, "a", "b"]


class CountingMapping(Mapping[str, object]):
    """A mapping that counts item and iteration access."""

    def __init__(self, data: Dict[str, object]) -> None:
        self._data = data
        self.getitem_count = 0
        self.items_count = 0

    def __getitem__(self, key: str) -> object:
        value = self._data[key]
        self.getitem_count += 1
        return value

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def items(self) -> object:  # type: ignore[override]
        self.items_count += 1
        return self._data.items()


class CountingSequence(Sequence[object]):
    """A sequence that counts item access."""

    def __init__(self, data: List[object]) -> None:
        self._data = data
        self.getitem_count = 0

    @overload
    def __getitem__(self, index: int) -> object: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[object]: ...

    def __getitem__(self, index: object) -> object:
        assert isinstance(index, int)
        value = self._data[index]
        self.getitem_count += 1
        return value

    def __len__(self) -> int:
        return len(self._data)


def counting_data() -> (
    Tuple[
    CountingMapping, CountingSequence, List[CountingMapping], List[CountingSequence]
]
):
    """Wrap DATA in access-counting mappings and sequences."""
    orders = []
    for order in cast(List[Dict[str, Any]], DATA["orders"]):
        lines = CountingSequence(list(order["lines"]))
        orders.append((CountingMapping({**order, "lines": lines}), lines))
    orders_sequence: CountingSequence = CountingSequence(
        [order for order, _ in orders]
    )
    root = CountingMapping(
        {
            "orders": orders_sequence,
            "meta": CountingMapping(dict(cast(Dict[str, object], DATA["meta"]))),
        }
    )
    return (
        root,
        orders_sequence,
        [order for order, _ in orders],
        [lines for _, lines in orders],
    )


def prefix_queries() -> Dict[str, str]:
    """Twenty queries that all start with `$.orders[*].lines`."""
    queries = {}
    suffixes = ("[*].sku", "[*].qty", "[*].price", "[*]")
    for i in range(10):
        queries[f"field_{i}"] = f"$.orders[*].lines{suffixes[i % 4]}"
    for i in range(10):
        queries[f"filter_{i}"] = f"$.orders[*].lines[?@.qty > {i}]"
    assert len(queries) == 20
    return queries


def test_shared_prefix_is_evaluated_once() -> None:
    queries = prefix_queries()
    root, orders_sequence, orders, _lines = counting_data()

    plan = jsonpath.compile_many(queries)
    plan.findall(root)

    # "$.orders" is resolved once for all twenty queries.
    assert root.getitem_count == 1
    # The wildcard over the orders array iterates it once.
    assert orders_sequence.getitem_count == 3
    # ".lines" is resolved once per order, not once per query per order.
    for order in orders:
        assert order.getitem_count == 1


def test_individual_execution_counts_for_comparison() -> None:
    queries = prefix_queries()
    root, orders_sequence, orders, _lines = counting_data()

    for path in queries.values():
        jsonpath.findall(path, root)

    # Without a shared plan, every query walks the prefix itself.
    assert root.getitem_count == len(queries)
    assert orders_sequence.getitem_count == 3 * len(queries)
    for order in orders:
        assert order.getitem_count == len(queries)


class SideEffectFunction:
    """A filter function with an observable side effect (a call counter)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _: object) -> bool:
        self.calls += 1
        return True


def test_impure_function_is_not_shared() -> None:
    env = JSONPathEnvironment()
    func = SideEffectFunction()
    env.function_extensions["side_effect"] = func

    queries = {
        "a": "$.orders[*].lines[?side_effect(@)].sku",
        "b": "$.orders[*].lines[?side_effect(@)].qty",
    }
    plan = env.compile_many(queries)
    results = plan.findall(DATA)

    # Three line items in total, evaluated once per query.
    assert func.calls == 6  # noqa: PLR2004

    # The same number of calls as executing each query individually.
    individual_calls = 0
    for path in queries.values():
        counter = SideEffectFunction()
        env.function_extensions["side_effect"] = counter
        assert env.compile(path).findall(DATA) == results[
            "a" if path.endswith("sku") else "b"
        ]
        individual_calls += counter.calls
    assert func.calls == individual_calls


def test_pure_function_is_shared() -> None:
    env = JSONPathEnvironment()
    func = SideEffectFunction()
    func.pure = True  # type: ignore[attr-defined]
    env.function_extensions["side_effect"] = func

    queries = {
        "a": "$.orders[*].lines[?side_effect(@)].sku",
        "b": "$.orders[*].lines[?side_effect(@)].qty",
    }
    plan = env.compile_many(queries)
    results = plan.findall(DATA)

    # Declared pure, so the filter is evaluated once for both queries.
    assert func.calls == 3  # noqa: PLR2004
    assert results["a"] == ["a", "b", "c"]
    assert results["b"] == [1, 2, 3]


class TypedSideEffectFunction(FilterFunction):
    """A FilterFunction subclass; impure by default."""

    arg_types = [ExpressionType.VALUE]
    return_type = ExpressionType.LOGICAL

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _: object) -> bool:
        self.calls += 1
        return True


class TypedPureFunction(TypedSideEffectFunction):
    """A FilterFunction subclass explicitly declared pure."""

    pure = True


def test_filter_function_subclasses_default_to_impure() -> None:
    env = JSONPathEnvironment()
    impure = TypedSideEffectFunction()
    env.function_extensions["typed"] = impure
    queries = {
        "a": "$.orders[*].lines[?typed(@)].sku",
        "b": "$.orders[*].lines[?typed(@)].qty",
    }
    env.compile_many(queries).findall(DATA)
    assert impure.calls == 6  # noqa: PLR2004

    pure = TypedPureFunction()
    env.function_extensions["typed"] = pure
    env.compile_many(queries).findall(DATA)
    assert pure.calls == 3  # noqa: PLR2004


def test_builtin_functions_are_shared() -> None:
    root, _orders_sequence, _orders, lines = counting_data()
    queries = {
        "a": "$.orders[*].lines[?length(@.sku) > 0].sku",
        "b": "$.orders[*].lines[?length(@.sku) > 0].qty",
    }
    plan = jsonpath.compile_many(queries)
    results = plan.findall(root)

    assert results["a"] == ["a", "b", "c"]
    assert results["b"] == [1, 2, 3]
    # The shared filter segment iterates each lines array once, not twice.
    for line_sequence in lines:
        assert line_sequence.getitem_count == len(line_sequence)


class BoomFunction:
    """A filter function that raises at evaluation time."""

    def __call__(self, _: object) -> bool:
        raise RuntimeError("boom")


def test_resolution_errors_are_attributed_to_query_names() -> None:
    env = JSONPathEnvironment()
    env.function_extensions["boom"] = BoomFunction()

    queries = {
        "ok_ids": "$.orders[*].id",
        "bad": "$.orders[?boom(@)].id",
        "ok_meta": "$.meta.count",
    }
    plan = env.compile_many(queries)

    result = plan.apply(DATA)
    assert not result.ok
    assert list(result.errors) == ["bad"]
    assert isinstance(result.errors["bad"], RuntimeError)
    # Sibling queries are unaffected.
    assert result["ok_ids"].values() == [1, 2, 3]
    assert result["ok_meta"].values() == [3]
    assert result["bad"].values() == []

    # findall() raises, but the error still names the failing query.
    with pytest.raises(JSONPathBatchError) as exc_info:
        plan.findall(DATA)
    assert list(exc_info.value.errors) == ["bad"]
    assert "bad" in str(exc_info.value)


def test_compile_errors_are_attributed_to_query_names() -> None:
    with pytest.raises(JSONPathBatchError) as exc_info:
        jsonpath.compile_many({"good": "$.orders", "bad": "$.."})

    assert list(exc_info.value.errors) == ["bad"]
    assert isinstance(exc_info.value.errors["bad"], JSONPathSyntaxError)


def test_plan_is_reusable_across_documents() -> None:
    queries = twenty_queries()
    plan = jsonpath.compile_many(queries)

    first = plan.findall(DATA)
    other_data = {"orders": [], "meta": {"count": 0}}
    second = plan.findall(other_data)

    assert second["skus"] == []
    assert second["order_ids"] == []
    assert second["meta_count"] == [0]

    # No state from previous applications leaks into later ones.
    assert plan.findall(DATA) == first
    again = plan.finditer(DATA)
    for name, path in queries.items():
        assert again[name].paths() == [
            match.path for match in jsonpath.finditer(path, DATA)
        ], name


def test_union_and_intersection_queries() -> None:
    queries = {
        "union": "$.meta.count | $.orders[*].id",
        "shared": "$.orders[*].id",
    }
    plan = jsonpath.compile_many(queries)
    results = plan.finditer(DATA)
    for name, path in queries.items():
        assert results[name].paths() == [
            match.path for match in jsonpath.finditer(path, DATA)
        ], name


def test_pseudo_root_queries() -> None:
    queries = {
        "pseudo": "^[0].meta.count",
        "plain": "$.meta.count",
    }
    plan = jsonpath.compile_many(queries)
    results = plan.finditer(DATA)
    for name, path in queries.items():
        assert results[name].paths() == [
            match.path for match in jsonpath.finditer(path, DATA)
        ], name


class AsyncCountingMapping(Mapping[str, object]):
    """A mapping with an async item getter and access counters."""

    def __init__(self, data: Dict[str, object]) -> None:
        self._data = data
        self.getitem_count = 0
        self.await_count = 0

    def __getitem__(self, key: str) -> object:
        self.getitem_count += 1
        return self._data[key]

    async def __getitem_async__(self, key: str) -> object:
        self.await_count += 1
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def test_compile_many_async() -> None:
    queries = {
        "skus": "$.orders[*].lines[*].sku",
        "qtys": "$.orders[*].lines[*].qty",
        "order_ids": "$.orders[*].id",
    }
    root = AsyncCountingMapping(dict(DATA))
    plan = jsonpath.compile_many(queries)

    async def coro() -> Dict[str, List[object]]:
        return await plan.findall_async(root)

    results = asyncio.run(coro())

    for name, path in queries.items():
        assert results[name] == jsonpath.findall(path, DATA), name

    # The shared "$.orders" step awaited the async getter once.
    assert root.await_count == 1
    assert root.getitem_count == 0


def test_compile_many_async_matches_individual_async() -> None:
    queries = twenty_queries()
    plan = jsonpath.compile_many(queries)

    async def coro() -> Dict[str, List[str]]:
        matches = await plan.finditer_async(DATA)
        return {name: node_list.paths() for name, node_list in matches.items()}

    results = asyncio.run(coro())

    async def individual(path: str) -> List[str]:
        return [
            match.path
            async for match in await jsonpath.compile(path).finditer_async(DATA)
        ]

    for name, path in queries.items():
        assert results[name] == asyncio.run(individual(path)), name


def test_async_errors_are_attributed_to_query_names() -> None:
    env = JSONPathEnvironment()
    env.function_extensions["boom"] = BoomFunction()
    plan = env.compile_many(
        {"ok": "$.orders[*].id", "bad": "$.orders[?boom(@)].id"}
    )

    async def coro() -> BatchResult:
        return await plan.apply_async(DATA)

    result = asyncio.run(coro())
    assert list(result.errors) == ["bad"]
    assert result["ok"].values() == [1, 2, 3]
