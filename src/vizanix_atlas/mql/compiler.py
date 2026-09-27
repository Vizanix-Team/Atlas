"""Compiling a validated query to DuckDB SQL.

The output here is always a single ``SELECT`` over one DuckDB view (``market``),
built entirely from validated physical column names and parameter placeholders.
There is no string concatenation of user-controlled text into the SQL body: every
literal value in the query becomes a bound parameter, and every identifier was
already checked against a fixed allow-list in :mod:`vizanix_atlas.mql.semantic`
before this module ever sees it. This is what makes it safe to hand the compiled SQL
straight to DuckDB with no further sandboxing (see ``docs/MQL.md``, "MQL should
understand Atlas metric names" and "no filesystem/network SQL operations").
"""

from __future__ import annotations

from dataclasses import dataclass

from vizanix_atlas.mql.ast_nodes import (
    And,
    Between,
    BoolExpr,
    Comparison,
    Identifier,
    InList,
    IsNull,
    Literal_,
    Not,
    Operand,
    Or,
)
from vizanix_atlas.mql.columns import ColumnInfo, market_table_columns
from vizanix_atlas.mql.semantic import ValidatedQuery

#: The DuckDB view name every compiled query reads from. Registered by the engine over
#: the downloaded Parquet shards before a compiled query runs.
MARKET_VIEW = "market"


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    """A safe, parameterised SQL statement ready to execute.

    Attributes:
        sql: The statement text, with ``?`` placeholders for every literal value.
        parameters: The values to bind to those placeholders, in order.
    """

    sql: str
    parameters: tuple[object, ...]


def _quote_identifier(name: str) -> str:
    """Quote a physical column name for use in SQL.

    Every name passed here came from :mod:`vizanix_atlas.mql.columns`, a fixed
    dictionary of Atlas's own column names, never from raw user input, so quoting is
    purely defensive: it lets a column name contain characters SQL would otherwise
    treat specially (Atlas's flattened names use a double underscore, which needs no
    escaping, but quoting costs nothing and removes a class of future foot-gun if a
    column name ever does).
    """
    return '"' + name.replace('"', '""') + '"'


class _Compiler:
    """Accumulates SQL fragments and bound parameters while walking the AST.

    Resolves every :class:`Identifier` against the fixed metric-to-column mapping
    directly (the same source of truth :mod:`vizanix_atlas.mql.semantic` validated
    against), rather than threading a partial column list through from the caller.
    Semantic validation already guarantees every identifier the compiler encounters
    is a real, known metric, so this lookup cannot fail here.
    """

    def __init__(self, known: dict[str, ColumnInfo]) -> None:
        self.parameters: list[object] = []
        self._known = known

    def _bind(self, value: object) -> str:
        self.parameters.append(value)
        return "?"

    def _operand(self, operand: Operand) -> str:
        if isinstance(operand, Identifier):
            return _quote_identifier(self._known[operand.name].physical_column)
        assert isinstance(operand, Literal_)
        return self._bind(operand.value)

    def compile_bool_expr(self, expr: BoolExpr) -> str:
        """Compile one WHERE-clause expression, recursively."""
        match expr:
            case Comparison(left=left, operator=operator, right=right):
                return f"{self._operand(left)} {operator} {self._operand(right)}"
            case IsNull(operand=operand, negated=negated):
                keyword = "IS NOT NULL" if negated else "IS NULL"
                return f"{self._operand(operand)} {keyword}"
            case InList(operand=operand, values=values, negated=negated):
                keyword = "NOT IN" if negated else "IN"
                bound = ", ".join(self._bind(v.value) for v in values)
                return f"{self._operand(operand)} {keyword} ({bound})"
            case Between(operand=operand, low=low, high=high, negated=negated):
                keyword = "NOT BETWEEN" if negated else "BETWEEN"
                return (
                    f"{self._operand(operand)} {keyword} "
                    f"{self._operand(low)} AND {self._operand(high)}"
                )
            case Not(operand=inner):
                return f"NOT ({self.compile_bool_expr(inner)})"
            case And(left=left, right=right):
                return f"({self.compile_bool_expr(left)}) AND ({self.compile_bool_expr(right)})"
            case Or(left=left, right=right):
                return f"({self.compile_bool_expr(left)}) OR ({self.compile_bool_expr(right)})"
        raise AssertionError(f"unhandled boolean expression node: {expr!r}")  # pragma: no cover


def compile_query(query: ValidatedQuery) -> CompiledQuery:
    """Compile a validated query into parameterised SQL against :data:`MARKET_VIEW`."""
    known = market_table_columns()
    compiler = _Compiler(known)

    select_sql = ", ".join(
        f"{_quote_identifier(item.column.physical_column)} AS {_quote_identifier(item.output_name)}"
        for item in query.select
    )
    parts = [f"SELECT {select_sql}", f"FROM {MARKET_VIEW}"]

    if query.where is not None:
        parts.append(f"WHERE {compiler.compile_bool_expr(query.where)}")

    if query.order_by:
        order_sql = ", ".join(
            f"{_quote_identifier(column.physical_column)} {'DESC' if descending else 'ASC'}"
            for column, descending in query.order_by
        )
        parts.append(f"ORDER BY {order_sql}")

    if query.limit is not None:
        parts.append(f"LIMIT {compiler._bind(query.limit)}")

    return CompiledQuery(sql=" ".join(parts), parameters=tuple(compiler.parameters))
