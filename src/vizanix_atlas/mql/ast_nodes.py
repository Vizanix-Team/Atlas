"""MQL abstract syntax tree.

Frozen dataclasses throughout: a parsed query is a value, not something a later stage
mutates in place. The semantic validator and the compiler each build their own new
structure from what they are given rather than editing the parser's output, which is
what keeps ``atlas query --explain`` able to show the same AST a real run compiled
from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class Identifier:
    """A bare column/metric name, as written in the query."""

    name: str
    position: int


@dataclass(frozen=True, slots=True)
class Literal_:  # noqa: N801 - trailing underscore avoids shadowing typing.Literal
    """A literal value: a number, a string, a boolean, or ``NULL``."""

    value: float | str | bool | None
    position: int


Operand = Identifier | Literal_

ComparisonOperator = Literal["=", "!=", "<", "<=", ">", ">="]


@dataclass(frozen=True, slots=True)
class Comparison:
    """``<left> <op> <right>``."""

    left: Operand
    operator: ComparisonOperator
    right: Operand


@dataclass(frozen=True, slots=True)
class IsNull:
    """``<operand> IS [NOT] NULL``."""

    operand: Operand
    negated: bool


@dataclass(frozen=True, slots=True)
class InList:
    """``<operand> [NOT] IN (<literals...>)``."""

    operand: Operand
    values: tuple[Literal_, ...]
    negated: bool


@dataclass(frozen=True, slots=True)
class Between:
    """``<operand> [NOT] BETWEEN <low> AND <high>``."""

    operand: Operand
    low: Operand
    high: Operand
    negated: bool


@dataclass(frozen=True, slots=True)
class Not:
    """``NOT <expr>``."""

    operand: BoolExpr


@dataclass(frozen=True, slots=True)
class And:
    """``<left> AND <right>``."""

    left: BoolExpr
    right: BoolExpr


@dataclass(frozen=True, slots=True)
class Or:
    """``<left> OR <right>``."""

    left: BoolExpr
    right: BoolExpr


BoolExpr = Comparison | IsNull | InList | Between | Not | And | Or


@dataclass(frozen=True, slots=True)
class SelectItem:
    """One selected column, with an optional alias."""

    name: Identifier
    alias: str | None = None


@dataclass(frozen=True, slots=True)
class OrderItem:
    """One ``ORDER BY`` term."""

    name: Identifier
    descending: bool = False


@dataclass(frozen=True, slots=True)
class Query:
    """A complete, parsed MQL query."""

    select_star: bool
    select_items: tuple[SelectItem, ...]
    source: Identifier
    where: BoolExpr | None
    order_by: tuple[OrderItem, ...]
    limit: int | None
