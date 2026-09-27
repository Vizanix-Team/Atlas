"""Table builders.

Turns an in-memory :class:`~vizanix_atlas.discovery.pipeline.Generation` into the
Parquet tables Atlas publishes. Each function here owns exactly one table's shape, so
adding a column means changing one function and one place in ``docs/METRICS.md`` or
``docs/DATA_MODEL.md``, not hunting through the dataset builder.

Column order matters for stable diffs between generations and is fixed by each
function's own column list, not by dict insertion order from whatever model happened
to produce the row.
"""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl

from vizanix_atlas.core.identifiers import shard_for
from vizanix_atlas.discovery.pipeline import Generation
from vizanix_atlas.models.asset import AssetAlias, AssetRelationship, CanonicalAsset
from vizanix_atlas.models.instrument import Instrument
from vizanix_atlas.models.observations import AdapterHealth
from vizanix_atlas.models.quality import QualityEvent
from vizanix_atlas.models.state import AssetStateBundle
from vizanix_atlas.storage.flatten import flatten_model, flatten_models

#: Canonical column order for each published table. Declared once here rather than
#: left to whatever order a flattened dict produces, so that the same generation
#: always serialises byte-identical Parquet (see docs/METHODOLOGY.md on determinism).
ASSET_COLUMNS: tuple[str, ...] = (
    "asset_id", "symbol", "name", "chain_slug", "chain_id", "contract_address",
    "resolution_state", "is_stablecoin", "is_fiat", "tracks_asset_id",
    "first_seen_at", "last_seen_at", "venue_count",
)
ASSET_ALIAS_COLUMNS: tuple[str, ...] = (
    "asset_id", "venue_slug", "venue_symbol", "resolution_state", "evidence",
    "confidence_note",
)
ASSET_RELATIONSHIP_COLUMNS: tuple[str, ...] = (
    "from_asset_id", "to_asset_id", "relationship", "source", "note",
)
INSTRUMENT_COLUMNS: tuple[str, ...] = (
    "instrument_id", "venue_slug", "instrument_type", "instrument_class",
    "symbol_native", "symbol_normalized", "base_asset_id", "quote_asset_id",
    "settlement_asset_id", "underlying_asset_id", "contract_type",
    "contract_multiplier", "contract_value_asset_id", "settlement_period",
    "tick_size", "quantity_step", "minimum_quantity", "minimum_notional",
    "expiry", "strike", "option_type", "funding_interval_hours",
    "funding_semantics", "open_interest_unit", "active", "listing_time",
    "delisting_time", "first_seen_at", "last_seen_at",
)
ADAPTER_HEALTH_COLUMNS: tuple[str, ...] = (
    "venue_slug", "status", "duration_ms", "requests_attempted",
    "requests_successful", "timeouts", "rate_limit_responses",
    "application_errors", "parse_failures", "http_errors", "instrument_count",
    "ticker_count", "derivative_observation_count", "order_book_count",
    "bytes_received", "circuit_opened", "error_types", "unknown_enum_values",
    "notes", "clock_skew_ms",
)
QUALITY_EVENT_COLUMNS: tuple[str, ...] = (
    "generation_id", "venue_slug", "instrument_id", "asset_id", "event_type",
    "severity", "detail", "observed_at",
)
VENUE_ASSET_STATE_COLUMNS: tuple[str, ...] = (
    "asset_id", "venue_slug", "observed_at", "price_usd", "price_source",
    "deviation_bps", "reference_weight", "spread_bps",
    "reported_base_volume_24h", "reported_quote_volume_24h",
    "reported_volume_24h_usd", "volume_share", "liquidity_share",
    "depth_50bps_usd", "funding_rate_raw", "funding_interval_hours",
    "funding_rate_8h", "mark_price_usd", "index_price_usd",
    "open_interest_raw", "open_interest_usd", "instrument_count",
    "conversion__from_asset_id", "conversion__to_asset_id", "conversion__rate",
    "conversion__method", "conversion__observed_at",
    "conversion__source_venue_slug", "conversion__path",
    "conversion__deviation_from_parity_bps",
    "included_in_reference_price", "exclusion_reason",
)


def _frame(rows: Sequence[dict], columns: tuple[str, ...]) -> pl.DataFrame:
    """Build a DataFrame with an exact, stable column order.

    Every declared column is present even when every row happened to omit it (an empty
    generation, or a field that is ``None`` everywhere), so the published schema never
    silently drops a column because no row this run had a value for it.
    """
    # infer_schema_length=None scans every row before choosing a column's type. The
    # default (100) infers from a sample, and a column that is None in its first
    # hundred rows and a string afterwards (chain_slug on the great majority of
    # unresolved assets, for example) would otherwise raise mid-build instead of
    # producing a nullable string column.
    frame = pl.DataFrame(list(rows), infer_schema_length=None) if rows else pl.DataFrame()
    for column in columns:
        if column not in frame.columns:
            frame = frame.with_columns(pl.lit(None).alias(column))
    return frame.select(list(columns))


def build_asset_table(assets: Sequence[CanonicalAsset]) -> pl.DataFrame:
    """Build the ``assets`` table: one row per canonical asset."""
    return _frame(flatten_models(list(assets)), ASSET_COLUMNS)


def build_asset_alias_table(aliases: Sequence[AssetAlias]) -> pl.DataFrame:
    """Build the ``asset_aliases`` table: every venue symbol mapping, with evidence."""
    return _frame(flatten_models(list(aliases)), ASSET_ALIAS_COLUMNS)


def build_asset_relationship_table(
    relationships: Sequence[AssetRelationship],
) -> pl.DataFrame:
    """Build the ``asset_relationships`` table: the asset graph's edges."""
    return _frame(flatten_models(list(relationships)), ASSET_RELATIONSHIP_COLUMNS)


def build_instrument_table(instruments: Sequence[Instrument]) -> pl.DataFrame:
    """Build the ``instruments`` table: every normalised, deduplicated instrument."""
    return _frame(flatten_models(list(instruments)), INSTRUMENT_COLUMNS)


def build_adapter_health_table(health: Sequence[AdapterHealth]) -> pl.DataFrame:
    """Build the ``adapter_health`` table: one row per venue per generation."""
    return _frame(flatten_models(list(health)), ADAPTER_HEALTH_COLUMNS)


def build_quality_event_table(events: Sequence[QualityEvent]) -> pl.DataFrame:
    """Build the ``quality_events`` table: every recorded validation rejection."""
    return _frame(flatten_models(list(events)), QUALITY_EVENT_COLUMNS)


def build_venue_asset_state_table(bundles: Sequence[AssetStateBundle]) -> pl.DataFrame:
    """Build the ``venue_asset_state`` table: the per-venue decomposition of every asset.

    This is the table that makes a reference price auditable from published data
    alone: it carries each venue's price, weight and inclusion decision.
    """
    rows = [flatten_model(venue) for bundle in bundles for venue in bundle.venues]
    return _frame(rows, VENUE_ASSET_STATE_COLUMNS)


def market_state_columns(bundles: Sequence[AssetStateBundle]) -> tuple[str, ...]:
    """Derive the ``asset_market_state`` column order from the schema itself.

    Unlike the other tables, ``MarketState`` is large and its sections are added to
    over time, so its column list is derived once by flattening the model rather than
    hand-maintained. Determinism still holds: the order comes from the model's own
    declared field order, not from row content, so it is identical for every
    generation built from the same software version.
    """
    if not bundles:
        return ()
    return tuple(flatten_model(bundles[0].state).keys())


def build_asset_market_state_table(
    bundles: Sequence[AssetStateBundle], *, shard_count: int
) -> pl.DataFrame:
    """Build the sharded ``asset_market_state`` table.

    Adds ``shard_index``, computed the same deterministic way the SDK computes it on
    read (``sha256(asset_id) mod shard_count``), so writer and reader always agree on
    which shard an asset lives in without either having to ask the other.
    """
    columns = market_state_columns(bundles)
    rows = [flatten_model(bundle.state) for bundle in bundles]
    frame = _frame(rows, columns)
    shard_indices = [shard_for(bundle.state.asset_id, shard_count) for bundle in bundles]
    return frame.with_columns(pl.Series("shard_index", shard_indices, dtype=pl.Int32))


def split_by_shard(frame: pl.DataFrame, *, shard_count: int) -> dict[int, pl.DataFrame]:
    """Partition a sharded table into one DataFrame per shard index.

    Every shard from 0 to ``shard_count - 1`` that has at least one row is present;
    shards with no assets are simply absent rather than published as empty files.
    """
    if "shard_index" not in frame.columns:
        raise ValueError("frame has no shard_index column")
    return {
        int(shard): group.drop("shard_index")
        for (shard,), group in frame.group_by(["shard_index"], maintain_order=True)
    }
