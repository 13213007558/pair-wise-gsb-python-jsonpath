import json
import re
from decimal import Decimal
from io import IOBase
from typing import Any

from ._types import NUMBER_MODE_PRECISE
from .exceptions import JSONPathTypeError

_RE_PROBABLY_MALFORMED = re.compile(r"[\{\}\[\]]")


def _reject_constant(value: str) -> Any:
    raise JSONPathTypeError(
        f"invalid JSON number {value!r}: NaN and Infinity are not valid JSON"
    )


def load_data(data: object, *, number_mode: str = "float") -> Any:
    if number_mode == NUMBER_MODE_PRECISE:
        # Parse JSON numbers exactly. Integers are arbitrary precision ints
        # and non-integers are Decimals, never binary floats. NaN and
        # Infinity are rejected as they are not valid JSON numbers.
        parse_float = Decimal
        parse_constant = _reject_constant
    else:
        parse_float = None
        parse_constant = None

    if isinstance(data, str):
        try:
            return json.loads(
                data, parse_float=parse_float, parse_constant=parse_constant
            )
        except json.JSONDecodeError:
            # Overly simple way to detect a malformed JSON document vs a
            # top-level string only document
            if _RE_PROBABLY_MALFORMED.search(data):
                raise
            return data
    if isinstance(data, IOBase):
        return json.loads(
            data.read(), parse_float=parse_float, parse_constant=parse_constant
        )
    return data
