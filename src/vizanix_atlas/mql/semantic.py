"""MQL semantic validation.

The parser only checks grammar; this stage checks meaning: that every referenced
metric exists, is filterable where used as a filter, and that ``market`` is the only
source MQL knows. A query that parses but names an unknown metric is rejected here
with a suggestion, the same way the metric registry itself resolves an unknown name
(see ``vizanix_atlas.core.metrics_registry.MetricRegistry.get``), so the two error
messages a user sees for "misspelled metric name" are consistent everywhere in Atlas.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from vizanix_atlas.core.errors import MqlSemanticError
from vizanix_atlas.mql.ast_nodes import (
    And,
    Between,
    BoolExpr,
    Comparison,
    Identifier,
    InList,
    IsNull,
    Not,
    Operand,
    Or,
    Query,
    SelectItem,
)
from vizanix_atlas.mql.columns import ColumnInfo, market_table_columns, star_columns

#: The only source MQL currently understands. Documented as a deliberate limitation
#: (see ``docs/MQL.md``): MQL is a query language over the current asset-state table,
#: not a general database, and there is no remote query server to add more sources to.
_SOURCE_NAME = "market"


@dataclass(frozen=True, slots=True)
class ResolvedSelectItem:
    """One output column: its exposed name and the metric it reads."""

    output_name: str
    column: ColumnInfo


@dataclass(frozen=True, slots=True)
class ValidatedQuery:
    """A query whose every identifier has been checked against the schema."""

    select: tuple[ResolvedSelectItem, ...]
    where: BoolExpr | None
    order_by: tuple[tuple[ColumnInfo, bool], ...]
    limit: int | None


def _suggest(name: str, known: dict[str, ColumnInfo]) -> str:
    """Build a "did you mean" suffix for an unknown metric name."""
    matches = difflib.get_close_matches(name, known, n=3, cutoff=0.6)
    if not matches:
        return ""
    return f" Did you mean: {', '.join(matches)}?"


def _resolve(identifier: Identifier, known: dict[str, ColumnInfo]) -> ColumnInfo:
    """Resolve one identifier to its column, or raise with a suggestion.

    Raises:
        MqlSemanticError: If ``identifier`` is not a metric MQL exposes.
    """
    column = known.get(identifier.name)
    if column is None:
        raise MqlSemanticError(
            f"unknown metric {identifier.name!r}.{_suggest(identifier.name, known)}",
            position=identifier.position,
        )
    return column


def _check_operand(operand: Operand, known: dict[str, ColumnInfo]) -> None:
    """Validate an operand that appears inside a WHERE clause."""
    if isinstance(operand, Identifier):
        column = _resolve(operand, known)
        if column.metric is not None and not column.metric.filterable:
            raise MqlSemanticError(
                f"{operand.name!r} may be selected but not filtered or ordered on",
                position=operand.position,
            )


def _check_bool_expr(expr: BoolExpr, known: dict[str, ColumnInfo]) -> None:
    """Recursively validate every identifier inside a WHERE clause."""
    match expr:
        case Comparison(left=left, right=right):
            _check_operand(left, known)
            _check_operand(right, known)
        case IsNull(operand=operand):
            _check_operand(operand, known)
        case InList(operand=operand):
            _check_operand(operand, known)
        case Between(operand=operand, low=low, high=high):
            _check_operand(operand, known)
            _check_operand(low, known)
            _check_operand(high, known)
        case Not(operand=inner):
            _check_bool_expr(inner, known)
        case And(left=left, right=right) | Or(left=left, right=right):
            _check_bool_expr(left, known)
            _check_bool_expr(right, known)


def validate(query: Query) -> ValidatedQuery:
    """Validate a parsed query against the schema MQL exposes.

    Raises:
        MqlSemanticError: If the source is not ``market``, a metric name is unknown,
            a non-filterable metric is used in ``WHERE`` or ``ORDER BY``, or
            ``SELECT *`` is combined with an explicit column list.
    """
    if query.source.name != _SOURCE_NAME:
        raise MqlSemanticError(
            f"unknown source {query.source.name!r}; MQL currently exposes only "
            f"{_SOURCE_NAME!r}",
            position=query.source.position,
        )

    known = market_table_columns()

    if query.select_star:
        select = tuple(
            ResolvedSelectItem(output_name=name, column=known[name]) for name in star_columns()
        )
    else:
        select = tuple(
            ResolvedSelectItem(
                output_name=item.alias or item.name.name,
                column=_resolve(item.name, known),
            )
            for item in query.select_items
        )
        _check_no_duplicate_aliases(select)

    if query.where is not None:
        _check_bool_expr(query.where, known)

    order_by = tuple(
        (_resolve(item.name, known), item.descending) for item in query.order_by
    )
    for column, _ in order_by:
        if column.metric is not None and not column.metric.filterable:
            raise MqlSemanticError(
                f"{column.name!r} may be selected but not filtered or ordered on"
            )

    return ValidatedQuery(select=select, where=query.where, order_by=order_by, limit=query.limit)


def _check_no_duplicate_aliases(select: tuple[ResolvedSelectItem, ...]) -> None:
    """Reject a query whose output columns would collide.

    Raises:
        MqlSemanticError: If two selected columns share an output name, which would
            otherwise silently produce a result table with an ambiguous column.
    """
    seen: dict[str, int] = {}
    for item in select:
        seen[item.output_name] = seen.get(item.output_name, 0) + 1
    duplicates = sorted(name for name, count in seen.items() if count > 1)
    if duplicates:
        raise MqlSemanticError(
            f"selected columns must have distinct names; duplicated: {', '.join(duplicates)}"
        )
