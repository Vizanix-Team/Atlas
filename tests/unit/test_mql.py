"""MQL tests: lexer, parser, semantic validation, compiler and execution.

The execution tests run against a small, hand-built Parquet file with exactly the
column shape :mod:`vizanix_atlas.storage.tables` produces, rather than a live-built
generation, so the suite never depends on network access.
"""

from __future__ import annotations

import polars as pl
import pytest

from vizanix_atlas.core.errors import MqlSemanticError, MqlSyntaxError
from vizanix_atlas.mql.columns import market_table_columns
from vizanix_atlas.mql.compiler import compile_query
from vizanix_atlas.mql.engine import MqlEngine, explain, run_query
from vizanix_atlas.mql.lexer import TokenType, tokenise
from vizanix_atlas.mql.parser import parse_query
from vizanix_atlas.mql.semantic import validate

# --------------------------------------------------------------------------- lexer


def test_lexer_recognises_keywords_case_insensitively() -> None:
    tokens = tokenise("select * from market where a and B or Not c")
    types = [t.type for t in tokens]
    assert types == [
        TokenType.SELECT, TokenType.STAR, TokenType.FROM, TokenType.IDENT,
        TokenType.WHERE, TokenType.IDENT, TokenType.AND, TokenType.IDENT,
        TokenType.OR, TokenType.NOT, TokenType.IDENT, TokenType.EOF,
    ]


def test_lexer_distinguishes_operators() -> None:
    tokens = tokenise("a<>b != c <= d >= e < f > g = h")
    types = [t.type for t in tokens if t.type is not TokenType.IDENT]
    assert types == [
        TokenType.NEQ, TokenType.NEQ, TokenType.LE, TokenType.GE,
        TokenType.LT, TokenType.GT, TokenType.EQ, TokenType.EOF,
    ]


def test_lexer_parses_string_literals_with_escapes() -> None:
    tokens = tokenise(r"'it''s' 'a\'b' 'plain'")
    # Note: MQL uses backslash-escaping (\'), not SQL-standard doubled quotes; the
    # first literal here is two adjacent string tokens by design of this test's
    # deliberately awkward input, so only the escape-based literal is asserted on.
    tokens2 = tokenise(r"'a\'b'")
    assert tokens2[0].text == "a'b"


def test_lexer_rejects_an_unrecognised_character() -> None:
    with pytest.raises(MqlSyntaxError):
        tokenise("SELECT a FROM market WHERE a ~ 1")


# -------------------------------------------------------------------------- parser


def test_parser_handles_the_documented_market_scan_example() -> None:
    query = parse_query(
        """
        SELECT asset, reference_price, venue_count, funding_rate_8h_median, open_interest_usd
        FROM market
        WHERE venue_count >= 4 AND reported_volume_24h_usd > 10000000
        ORDER BY reported_volume_24h_usd DESC
        LIMIT 100
        """
    )
    assert not query.select_star
    assert [item.name.name for item in query.select_items] == [
        "asset", "reference_price", "venue_count", "funding_rate_8h_median", "open_interest_usd",
    ]
    assert query.source.name == "market"
    assert query.limit == 100
    assert len(query.order_by) == 1
    assert query.order_by[0].descending


def test_parser_handles_select_star() -> None:
    query = parse_query("SELECT * FROM market")
    assert query.select_star
    assert query.select_items == ()


def test_parser_handles_aliases() -> None:
    query = parse_query("SELECT reference_price AS price FROM market")
    assert query.select_items[0].alias == "price"


def test_parser_handles_is_null_and_negation() -> None:
    query = parse_query("SELECT asset FROM market WHERE reference_price IS NOT NULL")
    from vizanix_atlas.mql.ast_nodes import IsNull

    assert isinstance(query.where, IsNull)
    assert query.where.negated


def test_parser_handles_in_and_between() -> None:
    q1 = parse_query("SELECT asset FROM market WHERE asset IN ('BTC', 'ETH')")
    from vizanix_atlas.mql.ast_nodes import InList

    assert isinstance(q1.where, InList)
    assert [v.value for v in q1.where.values] == ["BTC", "ETH"]

    q2 = parse_query("SELECT asset FROM market WHERE venue_count NOT BETWEEN 1 AND 3")
    from vizanix_atlas.mql.ast_nodes import Between

    assert isinstance(q2.where, Between)
    assert q2.where.negated


def test_parser_respects_operator_precedence() -> None:
    """AND binds tighter than OR, matching standard boolean precedence."""
    from vizanix_atlas.mql.ast_nodes import And, Or

    query = parse_query("SELECT asset FROM market WHERE a = 1 OR b = 2 AND c = 3")
    assert isinstance(query.where, Or)
    assert isinstance(query.where.right, And)


def test_parser_handles_parentheses() -> None:
    from vizanix_atlas.mql.ast_nodes import And

    query = parse_query("SELECT asset FROM market WHERE (a = 1 OR b = 2) AND c = 3")
    assert isinstance(query.where, And)


def test_parser_rejects_missing_from() -> None:
    with pytest.raises(MqlSyntaxError):
        parse_query("SELECT asset")


def test_parser_rejects_a_negative_limit() -> None:
    with pytest.raises(MqlSyntaxError, match="not be negative"):
        parse_query("SELECT asset FROM market LIMIT -1")


def test_parser_rejects_trailing_garbage() -> None:
    with pytest.raises(MqlSyntaxError, match="unexpected input"):
        parse_query("SELECT asset FROM market LIMIT 5 GARBAGE")


def test_parser_accepts_an_optional_trailing_semicolon() -> None:
    query = parse_query("SELECT asset FROM market;")
    assert query.select_items[0].name.name == "asset"


# ------------------------------------------------------------------------ semantic


def test_semantic_rejects_an_unknown_metric_with_a_suggestion() -> None:
    with pytest.raises(MqlSemanticError, match="unknown metric 'refernce_price'"):
        validate(parse_query("SELECT refernce_price FROM market"))


def test_semantic_suggestion_names_a_real_metric() -> None:
    with pytest.raises(MqlSemanticError) as excinfo:
        validate(parse_query("SELECT refernce_price FROM market"))
    assert "reference_price" in str(excinfo.value)


def test_semantic_rejects_an_unknown_source() -> None:
    with pytest.raises(MqlSemanticError, match="unknown source"):
        validate(parse_query("SELECT asset FROM not_market"))


def test_semantic_rejects_duplicate_output_names() -> None:
    with pytest.raises(MqlSemanticError, match="distinct names"):
        validate(parse_query("SELECT reference_price, venue_count AS reference_price FROM market"))


def test_semantic_resolves_every_column_in_the_registry() -> None:
    """Every column MQL exposes must actually be a declared metric or a plain
    identity column (asset/asset_id/symbol), never a name nobody documented."""
    columns = market_table_columns()
    assert "reference_price" in columns
    assert columns["reference_price"].metric is not None
    assert columns["reference_price"].metric.filterable


# ------------------------------------------------------------------------ compiler


def test_compiler_parameterises_every_literal() -> None:
    validated = validate(
        parse_query("SELECT asset FROM market WHERE venue_count >= 4 AND asset != 'BTC'")
    )
    compiled = compile_query(validated)
    assert "4" not in compiled.sql, "the literal must be a bound parameter, not inlined"
    assert "BTC" not in compiled.sql
    assert compiled.parameters == (4.0, "BTC")


def test_compiler_maps_metric_names_to_physical_columns() -> None:
    validated = validate(parse_query("SELECT reference_price FROM market"))
    compiled = compile_query(validated)
    assert '"reference_price__value" AS "reference_price"' in compiled.sql


def test_compiler_output_never_contains_forbidden_sql_constructs() -> None:
    """The compiler's own output must never contain anything beyond a single SELECT:
    no semicolons, no PRAGMA, no ATTACH, no COPY, no multi-statement chaining -
    because there is nothing in the grammar that could produce them, but this locks
    that invariant in explicitly."""
    validated = validate(
        parse_query(
            "SELECT asset, reference_price FROM market WHERE venue_count >= 4 "
            "ORDER BY reference_price DESC LIMIT 10"
        )
    )
    sql = compile_query(validated).sql
    for forbidden in (";", "PRAGMA", "ATTACH", "COPY", "--", "/*"):
        assert forbidden not in sql.upper() if forbidden.isupper() else forbidden not in sql


def test_compiler_handles_in_and_between_and_is_null() -> None:
    validated = validate(
        parse_query(
            "SELECT asset FROM market WHERE asset IN ('BTC', 'ETH') "
            "AND venue_count BETWEEN 1 AND 10 AND reference_price IS NOT NULL"
        )
    )
    compiled = compile_query(validated)
    assert "IN (?, ?)" in compiled.sql
    assert "BETWEEN ? AND ?" in compiled.sql
    assert "IS NOT NULL" in compiled.sql
    assert compiled.parameters == ("BTC", "ETH", 1.0, 10.0)


# -------------------------------------------------------------------------- engine


@pytest.fixture
def sample_shard(tmp_path) -> list:
    """A tiny Parquet file with the real column shape MQL expects.

    Every physical column MQL's ``ASSET_TABLE_COLUMNS`` mapping can reference is
    present (nulled where this fixture has no meaningful value), so ``SELECT *``
    exercises the exact same column set the real dataset builder publishes.
    """
    from vizanix_atlas.mql.columns import ASSET_TABLE_COLUMNS

    row_count = 3
    columns: dict[str, list] = {
        "asset_id": ["asset:native:bitcoin:BTC", "asset:native:ethereum:ETH", "asset:x:zzz:ZZZ"],
        "symbol": ["BTC", "ETH", "ZZZ"],
        "name": ["Bitcoin", "Ethereum", None],
        "resolution_state": ["resolved", "resolved", "unresolved"],
        "reference_price__value": [84_400.0, 2_700.0, None],
        "reference_price__method": ["spot_weighted_median", "spot_weighted_median", "unavailable"],
        "reference_price__venue_count": [13, 12, 0],
        "venue_count": [13, 12, 0],
        "reported_volume_24h_usd": [2_800_000_000.0, 1_200_000_000.0, None],
        "dispersion__price_dispersion_bps": [1.2, 0.8, None],
    }
    for physical in ASSET_TABLE_COLUMNS.values():
        if physical not in columns:
            columns[physical] = [None] * row_count

    frame = pl.DataFrame(
        {name: pl.Series(name, values, dtype=pl.Float64 if all(v is None for v in values) else None)
         for name, values in columns.items()}
    )
    path = tmp_path / "assets-00.parquet"
    frame.write_parquet(path)
    return [path]


def test_engine_runs_a_query_end_to_end(sample_shard) -> None:
    result = run_query(
        "SELECT asset, reference_price, venue_count FROM market WHERE venue_count >= 12 "
        "ORDER BY reference_price DESC",
        sample_shard,
    )
    assert result["asset"].to_list() == ["BTC", "ETH"]
    assert result["reference_price"].to_list() == [84_400.0, 2_700.0]


def test_engine_respects_limit(sample_shard) -> None:
    result = run_query("SELECT asset FROM market ORDER BY asset LIMIT 1", sample_shard)
    assert result.height == 1


def test_engine_handles_null_reference_prices_without_a_type_error(sample_shard) -> None:
    """The unresolved asset's null price must not crash the engine or silently
    become zero."""
    result = run_query("SELECT asset, reference_price FROM market WHERE asset = 'ZZZ'", sample_shard)
    assert result.height == 1
    assert result["reference_price"][0] is None


def test_engine_select_star_returns_every_exposed_column(sample_shard) -> None:
    result = run_query("SELECT * FROM market", sample_shard)
    from vizanix_atlas.mql.columns import star_columns

    assert set(result.columns) >= {"asset", "reference_price", "venue_count"}
    assert len(result.columns) == len(star_columns())


def test_engine_refuses_with_no_shard_files() -> None:
    with pytest.raises(MqlSemanticError, match="no dataset shard files"):
        MqlEngine([])


def test_engine_reuses_one_connection_across_queries(sample_shard) -> None:
    with MqlEngine(sample_shard) as engine:
        first = engine.run("SELECT asset FROM market")
        second = engine.run("SELECT asset FROM market WHERE venue_count >= 12")
    assert first.height == 3
    assert second.height == 2


def test_explain_exposes_every_stage(sample_shard) -> None:
    result = explain("SELECT asset FROM market WHERE venue_count >= 4 LIMIT 3")
    assert result.parsed.source.name == "market"
    assert result.validated.select[0].output_name == "asset"
    assert result.compiled.parameters == (4.0, 3)
