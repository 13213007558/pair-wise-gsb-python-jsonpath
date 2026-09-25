"""Classes modeling the JSONPath spec type system for function extensions."""
from abc import ABC
from abc import abstractmethod
from enum import Enum
from typing import Any
from typing import List


class ExpressionType(Enum):
    """The type of a filter function argument or return value."""

    VALUE = 1
    LOGICAL = 2
    NODES = 3


class FilterFunction(ABC):
    """Base class for typed function extensions."""

    pure: bool = False
    """Whether or not calling this function is free of observable side effects.

    When `True`, plans compiled with `jsonpath.compile_many()` may share the
    result of evaluating a filter that uses this function between queries with
    a common prefix. When `False` (the default), such filters are always
    evaluated once per query, preserving any side effects.
    """

    @property
    @abstractmethod
    def arg_types(self) -> List[ExpressionType]:
        """Argument types expected by the filter function."""

    @property
    @abstractmethod
    def return_type(self) -> ExpressionType:
        """The type of the value returned by the filter function."""

    @abstractmethod
    def __call__(self, *args: Any, **kwds: Any) -> Any:
        """Called the filter function."""
