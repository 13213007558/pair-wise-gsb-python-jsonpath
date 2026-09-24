import json
import re
from decimal import Decimal
from io import IOBase
from typing import Any

from .exceptions import JSONPathError

_RE_PROBABLY_MALFORMED = re.compile(r"[\{\}\[\]]")


def _reject_non_json_number(value: str) -> Any:
    # A json.loads() parse_constant hook used to reject NaN and Infinity.
    raise JSONPathError(
        f"non-JSON number {value!r} is not allowed in exact number mode"
    )


def load_data(data: object, *, exact_numbers: bool = False) -> Any:
    if isinstance(data, str):
        try:
            if exact_numbers:
                return json.loads(
                    data,
                    parse_float=Decimal,
                    parse_constant=_reject_non_json_number,
                )
            return json.loads(data)
        except json.JSONDecodeError:
            # Overly simple way to detect a malformed JSON document vs a
            # top-level string only document
            if _RE_PROBABLY_MALFORMED.search(data):
                raise
            return data
    if isinstance(data, IOBase):
        if exact_numbers:
            return json.loads(
                data.read(),
                parse_float=Decimal,
                parse_constant=_reject_non_json_number,
            )
        return json.loads(data.read())
    return data
