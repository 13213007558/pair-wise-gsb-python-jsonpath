"""Tests for precise number mode."""

import asyncio
import decimal
from decimal import Decimal

import pytest

from jsonpath import JSONPathEnvironment
from jsonpath.exceptions import JSONPathTypeError
from jsonpath.filter import DecimalLiteral
from jsonpath.filter import IntegerLiteral
from jsonpath.function_extensions import ExpressionType
from jsonpath.function_extensions import FilterFunction


@pytest.fixture()
def env() -> JSONPathEnvironment:
    return JSONPathEnvironment(number_mode="precise")


def test_unknown_number_mode() -> None:
    with pytest.raises(ValueError, match="unknown number mode"):
        JSONPathEnvironment(number_mode="double")


def test_default_number_mode_is_float() -> None:
    assert JSONPathEnvironment().number_mode == "float"


def test_integer_literal_is_exact(env: JSONPathEnvironment) -> None:
    path = env.compile("$[?@ == 9007199254740993]")
    assert path.findall([9007199254740993]) == [9007199254740993]
    assert path.findall([9007199254740992]) == []


def test_adjacent_integers_beyond_2_to_the_53(env: JSONPathEnvironment) -> None:
    data = [2**53 - 1, 2**53, 2**53 + 1, 2**53 + 2]
    assert env.findall("$[?@ == 9007199254740991]", data) == [2**53 - 1]
    assert env.findall("$[?@ == 9007199254740992]", data) == [2**53]
    assert env.findall("$[?@ == 9007199254740993]", data) == [2**53 + 1]
    assert env.findall("$[?@ == 9007199254740994]", data) == [2**53 + 2]
    assert env.findall("$[?@ > 9007199254740992]", data) == [2**53 + 1, 2**53 + 2]


def test_adjacent_integers_beyond_2_to_the_53_from_json_text(
    env: JSONPathEnvironment,
) -> None:
    data = "[9007199254740991, 9007199254740992, 9007199254740993]"
    assert env.findall("$[?@ == 9007199254740993]", data) == [9007199254740993]
    # JSON text integers are loaded as arbitrary precision ints.
    assert env.findall("$[*]", data) == [2**53 - 1, 2**53, 2**53 + 1]


def test_float_mode_integer_literal_is_unchanged() -> None:
    # The default float mode keeps its historical float-roundtrip behavior.
    env = JSONPathEnvironment()
    assert env.findall("$[?@ == 9007199254740993]", [9007199254740992]) == [
        9007199254740992
    ]
    assert env.findall("$[?@ == 9007199254740993]", [9007199254740993]) == []


def test_high_precision_decimal_literal(env: JSONPathEnvironment) -> None:
    data = '{"a": 0.30000000000000000001, "b": 0.3}'
    assert env.findall("$[?@ == 0.30000000000000000001]", data) == [
        Decimal("0.30000000000000000001")
    ]
    # 0.3 and 0.30000000000000000001 are distinct.
    assert env.findall("$[?@ == 0.3]", data) == [Decimal("0.3")]
    assert env.findall("$.b", data) == [Decimal("0.3")]


def test_decimal_literal_does_not_round_trip_through_float(
    env: JSONPathEnvironment,
) -> None:
    # 0.1 as a binary float is not equal to the decimal 0.1.
    assert env.findall("$[?@ == 0.1]", "[0.1000000000000000055511151231257827]") == []
    assert env.findall("$[?@ == 0.1]", "[0.1]") == [Decimal("0.1")]


def test_scientific_notation_is_exact(env: JSONPathEnvironment) -> None:
    assert env.findall("$[?@ == 1.5e3]", "[1500]") == [1500]
    assert env.findall("$[?@ == 15e-1]", "[1.5]") == [Decimal("1.5")]
    # Beyond float range. These would be inf in float mode.
    assert env.findall("$[?@ == 1e400]", "[1e400]") == [Decimal("1e400")]
    assert env.findall("$[?@ == 1e400]", "[1e401]") == []


def test_huge_exponent(env: JSONPathEnvironment) -> None:
    data = "[1e999999999, 1e999999998]"
    assert env.findall("$[?@ == 1e999999999]", data) == [Decimal("1e999999999")]
    assert env.findall("$[?@ > 1e999999998]", data) == [Decimal("1e999999999")]
    assert env.findall("$[?@ == 1.5e-999999999]", "[1.5e-999999999]") == [
        Decimal("1.5e-999999999")
    ]


def test_exponent_integer_literal_is_decimal(env: JSONPathEnvironment) -> None:
    # Exponent-form integer literals stay exact without materializing
    # potentially huge ints.
    expr = env.compile("$[?@ == 1e3]")
    assert expr.findall([1000]) == [1000]
    filter_expr = expr.segments[0].selectors[0].expression  # type: ignore[attr-defined]
    assert isinstance(filter_expr.expression.right, DecimalLiteral)


def test_plain_integer_literal_is_int(env: JSONPathEnvironment) -> None:
    expr = env.compile("$[?@ == 42]")
    filter_expr = expr.segments[0].selectors[0].expression  # type: ignore[attr-defined]
    assert isinstance(filter_expr.expression.right, IntegerLiteral)


def test_booleans_are_not_numbers(env: JSONPathEnvironment) -> None:
    data = [True, False, 1, 0]
    assert env.findall("$[?@ == 1]", data) == [1]
    assert env.findall("$[?@ == 0]", data) == [0]
    assert env.findall("$[?@ == true]", data) == [True]
    assert env.findall("$[?@ < 2]", data) == [1, 0]
    assert env.findall("$[?@ < 1]", data) == [0]


def test_float_mode_boolean_comparison_is_unchanged() -> None:
    # Historical float mode behavior compares booleans as ints.
    env = JSONPathEnvironment()
    assert env.findall("$[?@ < 2]", [True]) == [True]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_json_numbers_in_json_text_are_rejected(
    env: JSONPathEnvironment, constant: str
) -> None:
    with pytest.raises(JSONPathTypeError, match="not valid JSON"):
        env.findall("$.a", f'{{"a": {constant}}}')


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_rejected(env: JSONPathEnvironment, number: float) -> None:
    # Caller-passed floats are kept as-is, but non-finite floats are not
    # valid JSON numbers and are rejected when compared.
    with pytest.raises(JSONPathTypeError, match="not a valid JSON number"):
        env.findall("$[?@ > 1]", [number])
    with pytest.raises(JSONPathTypeError, match="not a valid JSON number"):
        env.findall("$[?@ == 1]", [number])


@pytest.mark.parametrize("number", [Decimal("NaN"), Decimal("Infinity")])
def test_non_finite_decimals_are_rejected(
    env: JSONPathEnvironment, number: Decimal
) -> None:
    with pytest.raises(JSONPathTypeError, match="not a valid JSON number"):
        env.findall("$[?@ > 1]", [number])


def test_caller_supplied_floats_are_kept_as_floats(env: JSONPathEnvironment) -> None:
    # We do not try to recover the original decimal text from a binary
    # float. The float 0.1 is not equal to the decimal 0.1.
    assert env.findall("$[?@ == 0.1]", [0.1]) == []
    # Exactly representable floats still compare equal.
    assert env.findall("$[?@ == 1.5]", [1.5]) == [1.5]
    assert env.findall("$[?@ == 0.5]", [0.5]) == [0.5]


def test_mixed_int_and_decimal_comparison(env: JSONPathEnvironment) -> None:
    assert env.findall("$[?@ == 1.0]", "[1]") == [1]
    assert env.findall("$[?@ == 1]", "[1.0]") == [1]
    assert env.findall("$[?@ < 1.5]", "[1]") == [1]


class Double(FilterFunction):
    """An arithmetic function extension used for testing."""

    arg_types = [ExpressionType.VALUE]
    return_type = ExpressionType.VALUE

    def __call__(self, value: object) -> object:
        return value * 2  # type: ignore[operator]


def test_function_extensions_receive_precise_numbers(
    env: JSONPathEnvironment,
) -> None:
    env.function_extensions["double"] = Double()
    data = '[{"a": 0.30000000000000000001}]'
    assert env.findall("$[?double(@.a) == 0.60000000000000000002]", data) == [
        {"a": Decimal("0.30000000000000000001")}
    ]


def test_typeof_decimal_is_number(env: JSONPathEnvironment) -> None:
    data = '[{"a": 1.5}, {"a": true}, {"a": "1.5"}]'
    assert env.findall("$[?typeof(@.a) == 'number']", data) == [{"a": Decimal("1.5")}]
    assert env.findall("$[?isinstance(@.a, 'number')]", data) == [
        {"a": Decimal("1.5")}
    ]


def test_sync_and_async_consistency(env: JSONPathEnvironment) -> None:
    data = "[9007199254740992, 9007199254740993, 0.30000000000000000001, 1e400]"
    queries = [
        "$[?@ == 9007199254740993]",
        "$[?@ > 9007199254740991]",
        "$[?@ == 0.30000000000000000001]",
        "$[?@ == 1e400]",
        "$[?@ < 1]",
    ]
    for query in queries:
        sync_result = env.findall(query, data)

        async def coro() -> list:
            return await env.findall_async(query, data)

        assert asyncio.run(coro()) == sync_result


def test_compiled_paths_do_not_mix_number_modes() -> None:
    # The same query string compiled by environments with different number
    # modes keeps each mode's semantics, even with filter caching enabled.
    query = "$[?@ == 9007199254740993]"
    data = [9007199254740992, 9007199254740993]

    float_env = JSONPathEnvironment(filter_caching=True)
    precise_env = JSONPathEnvironment(number_mode="precise", filter_caching=True)

    float_path = float_env.compile(query)
    precise_path = precise_env.compile(query)

    # Repeated evaluation exercises cached filter expressions.
    for _ in range(3):
        assert float_path.findall(data) == [9007199254740992]
        assert precise_path.findall(data) == [9007199254740993]


def test_global_decimal_context_is_untouched(env: JSONPathEnvironment) -> None:
    before = decimal.getcontext().copy()
    env.findall("$[?@ == 0.30000000000000000001]", "[0.30000000000000000001, 1e400]")
    env.findall("$[?@ < 1e999999999]", "[1e999999999]")
    assert decimal.getcontext().prec == before.prec
    assert decimal.getcontext().rounding == before.rounding
    assert decimal.getcontext().Emax == before.Emax
    assert decimal.getcontext().Emin == before.Emin
