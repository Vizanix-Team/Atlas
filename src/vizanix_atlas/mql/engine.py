"""The MQL execution engine.

Ties the parser, semantic validator and compiler together and runs the result against
DuckDB, reading directly from the Parquet shards a client already has on local disk.
There is no remote query server anywhere in this path (see ``docs/MQL.md``): every
call in this module operates on files the caller names explicitly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb
import polars as pl

from vizanix_atlas.core.errors import MqlSemanticError
from vizanix_atlas.mql.ast_nodes import Query
from vizanix_atlas.mql.compiler import MARKET_VIEW, CompiledQuery, compile_query
from vizanix_atlas.mql.parser import parse_query
from vizanix_atlas.mql.semantic import ValidatedQuery, validate


@dataclass(frozen=True, slots=True)
class ExplainedQuery:
    """Every intermediate stage of compiling a query, for ``atlas query --explain``."""

    text: str
    parsed: Query
    validated: ValidatedQuery
    compiled: CompiledQuery


def explain(text: str) -> ExplainedQuery:
    """Run a query through every stage short of execution.

    Raises:
        MqlSyntaxError: On a grammar violation.
        MqlSemanticError: On an unknown or misused metric.

    """
    parsed = parse_query(text)
    validated = validate(parsed)
    compiled = compile_query(validated)
    return ExplainedQuery(text=text, parsed=parsed, validated=validated, compiled=compiled)


class MqlEngine:
    """Executes MQL queries against a set of local Parquet shards.

    One engine per dataset directory; it registers the ``market`` view once and reuses
    the same in-process DuckDB connection for every query run against it, which is
    what makes repeated queries in one CLI session or notebook cheap.
    """

    def __init__(self, shard_paths: Sequence[Path]) -> None:
        """Create an engine over a fixed set of shard files.

        Raises:
            MqlSemanticError: If ``shard_paths`` is empty; there is no honest way to
                answer a query with zero data files.

        """
        if not shard_paths:
            raise MqlSemanticError("no dataset shard files were provided to query")
        self._connection = duckdb.connect(database=":memory:")
        # A glob-free, explicit file list, so the engine only ever reads exactly the
        # files the caller named rather than whatever else DuckDB's own glob
        # expansion might turn up in the same directory.
        placeholders = ", ".join(f"'{path.as_posix()}'" for path in shard_paths)
        self._connection.execute(
            f"CREATE VIEW {MARKET_VIEW} AS SELECT * FROM read_parquet([{placeholders}])"
        )

    def run(self, text: str) -> pl.DataFrame:
        """Parse, validate, compile and execute one query.

        Raises:
            MqlSyntaxError: On a grammar violation.
            MqlSemanticError: On an unknown or misused metric.

        """
        compiled = explain(text).compiled
        return self._connection.execute(compiled.sql, compiled.parameters).pl()

    def close(self) -> None:
        """Close the underlying DuckDB connection."""
        self._connection.close()

    def __enter__(self) -> MqlEngine:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def run_query(text: str, shard_paths: Sequence[Path]) -> pl.DataFrame:
    """Run one query against a set of shard files without keeping an engine open.

    Prefer :class:`MqlEngine` directly when running more than one query against the
    same dataset, so the Parquet files are not re-scanned for each.
    """
    with MqlEngine(shard_paths) as engine:
        return engine.run(text)
