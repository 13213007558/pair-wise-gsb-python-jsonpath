"""Test exact number mode for JSONPathEnvironment."""

import asyncio
import decimal
import io
import sys

import pytest

from jsonpath import JSONPathEnvironment, JSONPathError, JSONPathSyntaxError

BIG_A = 2**53
BIG_B = 2**53 + 1


def run_async(coro):
    return asyncio.run(coro)


def test_integer_literal_preserves_precision() -> None:
    """A literal above 2**53 must not round through a binary float."""
    env = JSONPathEnvironment(exact_numbers=True)
    data = [BIG_A, BIG_B]
    assert env.findall(f"$[?@ == {BIG_B}]", data) == [BIG_B]
    assert env.findall(f"$[?@ != {BIG_B}]", data) == [BIG_A]
    assert env.findall(f"$[?@ > {BIG_A}]", data) == [BIG_B]
    assert env.findall(f"$[?@ < {BIG_A}]", data) == []


def test_integer_literal_precision_async() -> None:
    env = JSONPathEnvironment(exact_numbers=True)
    data = [BIG_A, BIG_B]
    assert run_async(env.findall_async(f"$[?@ == {BIG_B}]", data)) == [BIG_B]
    assert run_async(env.findall_async(f"$[?@ > {BIG_A}]", data)) == [BIG_B]


def test_adjacent_integers_in_json_text() -> None:
    """Numbers in JSON text must use the same exact semantics as literals."""
    env = JSONPathEnvironment(exact_numbers=True)
    json_text = f"[{BIG_A}, {BIG_B}]"
    assert env.findall(f"$[?@ == {BIG_B}]", json_text) == [BIG_B]
    assert run_async(env.findall_async(f"$[?@ != {BIG_B}]", json_text)) == [BIG_A]


def test_high_precision_decimal_literal() -> None:
    """Decimal literals and parsed JSON floats compare by exact value."""
    env = JSONPathEnvironment(exact_numbers=True)
    assert env.findall("$[?@ == 0.3]", "[0.30000000000000004, 0.3]") == [
        decimal.Decimal("0.3")
    ]
    assert env.findall("$[?@ != 0.3]", "[0.30000000000000004, 0.3]") == [
        decimal.Decimal("0.30000000000000004")
    ]
    assert env.findall("$[?@ > 0.3]", "[0.30000000000000004]") == [
        decimal.Decimal("0.30000000000000004")
    ]


def test_high_precision_decimal_literal_async() -> None:
    env = JSONPathEnvironment(exact_numbers=True)
    result = run_async(env.findall_async("$[?@ == 0.3]", "[0.30000000000000004, 0.3]"))
    assert result == [decimal.Decimal("0.3")]


def test_decimal_data_from_file() -> None:
    env = JSONPathEnvironment(exact_numbers=True)
    assert env.findall("$[?@ == 0.3]", io.StringIO("[0.3]")) == [decimal.Decimal("0.3")]


def test_huge_exponent_is_exact() -> None:
    """Huge positive exponents must not collapse to float infinity."""
    env = JSONPathEnvironment(exact_numbers=True)
    json_text = "[1e100000, 2e100000]"
    assert env.findall("$[?@ == 2e100000]", json_text) == [decimal.Decimal("2E+100000")]
    assert env.findall("$[?@ < 2e100000]", json_text) == [decimal.Decimal("1E+100000")]
    assert env.findall("$[?@ == 1e100000]", json_text) != env.findall(
        "$[?@ == 2e100000]", json_text
    )


def test_huge_negative_exponent_is_exact() -> None:
    env = JSONPathEnvironment(exact_numbers=True)
    json_text = "[1e-100000]"
    assert env.findall("$[?@ == 1e-100000]", json_text) == [
        decimal.Decimal("1E-100000")
    ]
    assert env.findall("$[?@ > 0]", json_text) == [decimal.Decimal("1E-100000")]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_json_numbers_rejected_in_json_text(constant: str) -> None:
    env = JSONPathEnvironment(exact_numbers=True)
    with pytest.raises(JSONPathError):
        env.findall("$", f"[{constant}]")


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_json_numbers_rejected_in_file_data(constant: str) -> None:
    env = JSONPathEnvironment(exact_numbers=True)
    with pytest.raises(JSONPathError):
        env.findall("$", io.StringIO(f"[{constant}]"))


def test_booleans_are_not_numbers() -> None:
    env = JSONPathEnvironment(exact_numbers=True)
    data = [True, False, 0, 1]
    assert env.findall("$[?@ < 2]", data) == [0, 1]
    assert env.findall("$[?@ > -1]", data) == [0, 1]
    assert env.findall("$[?@ == 1]", data) == [1]
    assert env.findall("$[?@ == true]", data) == [True]


def test_caller_floats_are_kept_not_decoded() -> None:
    """Caller-supplied floats keep float semantics; no fake text recovery."""
    env = JSONPathEnvironment(exact_numbers=True)
    value = 0.1 + 0.2
    assert value != 0.3
    assert env.findall("$[?@ == 0.3]", [value]) == []
    assert env.findall("$[?@ != 0.3]", [value]) == [value]
    # Exactly representable floats compare equal to their Decimal literal.
    assert env.findall("$[?@ == 0.5]", [0.5]) == [0.5]
    assert isinstance(env.findall("$[?@ == 0.5]", [0.5])[0], float)


def test_caller_decimal_kept() -> None:
    env = JSONPathEnvironment(exact_numbers=True)
    value = decimal.Decimal("1.5")
    assert env.findall("$[?@ == 1.5]", [value]) == [value]


def test_typeof_recognizes_decimal_as_number() -> None:
    env = JSONPathEnvironment(exact_numbers=True)
    data = '[{"x": 1.5}, {"x": 2}, {"x": "s"}]'
    assert env.findall("$[?typeof(@.x) == 'number']", data) == [
        {"x": decimal.Decimal("1.5")},
        {"x": 2},
    ]
    assert env.findall("$[?isinstance(@.x, 'number')]", data) == [
        {"x": decimal.Decimal("1.5")},
        {"x": 2},
    ]
    assert env.findall("$[?isinstance(@.x, 'decimal')]", data) == [
        {"x": decimal.Decimal("1.5")}
    ]


def test_sync_and_async_match() -> None:
    env = JSONPathEnvironment(exact_numbers=True)
    json_text = "[9007199254740992, 9007199254740993, 0.30000000000000004]"
    queries = [
        "$[?@ == 9007199254740993]",
        "$[?@ > 9007199254740992]",
        "$[?@ != 0.3]",
        "$[?@ < 1e100000]",
    ]
    for query in queries:
        assert env.findall(query, json_text) == run_async(
            env.findall_async(query, json_text)
        )


def test_environments_do_not_mix_number_modes() -> None:
    """Compiled paths and caches stay scoped to their environment's mode."""
    exact_env = JSONPathEnvironment(exact_numbers=True)
    float_env = JSONPathEnvironment()
    query = f"$[?@ == {BIG_B}]"
    exact_path = exact_env.compile(query)
    float_path = float_env.compile(query)
    assert exact_path.findall([BIG_A, BIG_B]) == [BIG_B]
    # Default mode keeps its historic int(float(...)) behavior.
    assert float_path.findall([BIG_A, BIG_B]) == [BIG_A]
    # Repeated evaluation of the cached exact filter expression stays exact.
    for _ in range(5):
        assert exact_path.findall([BIG_A, BIG_B]) == [BIG_B]
        assert float_path.findall([BIG_A, BIG_B]) == [BIG_A]


def test_default_mode_unchanged() -> None:
    env = JSONPathEnvironment()
    assert env.findall(f"$[?@ == {BIG_B}]", [BIG_A, BIG_B]) == [BIG_A]
    assert env.findall("$[?@ == 0.3]", "[0.3]") == [0.3]
    assert env.findall("$[?@ < 2]", [True, 1]) == [True, 1]
    import math

    assert math.isnan(env.findall("$", "[NaN]")[0][0])


def test_global_decimal_context_unchanged() -> None:
    context = decimal.getcontext()
    attrs = ("prec", "rounding", "Emax", "Emin", "capitals")
    before = {name: getattr(context, name) for name in attrs}
    env = JSONPathEnvironment(exact_numbers=True)
    env.findall("$[?@ == 1e100000]", "[1e100000]")
    env.findall("$[?@ == 0.3]", "[0.30000000000000004, 0.3]")
    after = {name: getattr(context, name) for name in attrs}
    assert after == before


def test_integer_literal_too_big() -> None:
    limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if not limit or limit <= 0:
        pytest.skip("Python has no integer string conversion digit limit")
    env = JSONPathEnvironment(exact_numbers=True)
    with pytest.raises(JSONPathSyntaxError):
        env.compile("$[?@ == " + "9" * (limit + 100) + "]")
