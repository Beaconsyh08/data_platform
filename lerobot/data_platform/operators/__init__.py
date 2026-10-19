"""Portable operator contracts and lazy discovery for Data Platform computations."""

from .contracts import ExecutionContext, OperatorResult, OperatorSpec
from .registry import execute_operator, get_operator, list_operators, register_operator

__all__ = [
    "ExecutionContext",
    "OperatorResult",
    "OperatorSpec",
    "execute_operator",
    "get_operator",
    "list_operators",
    "register_operator",
]
