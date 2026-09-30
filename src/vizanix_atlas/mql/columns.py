"""Metric name to physical column mapping.

Atlas's Universal Market Schema is deliberately nested
(``MarketState.reference_price.value``), and the storage layer flattens that nesting
into columns named ``reference_price__value`` (see
``docs/DATA_MODEL.md`` and :mod:`vizanix_atlas.storage.flatten`). MQL, on the other
hand, is meant to read like the metric registry - ``reference_price``, not
``reference_price__value``. This module is the one place that bridges the two names,
so a query and the registry's own documentation always agree on what a metric is
called.

Not every metric the registry declares is queryable from a single asset-state row
today: some (``PERCENTILE``, ``CHANGE`` over a window, the multi-generation history
functions) need more than one generation's data, which is a follow-on capability
tracked in ``docs/ROADMAP.md`` rather than something this module pretends to support.
A metric absent from :data:`ASSET_TABLE_COLUMNS` is not selectable, and MQL says so
with a specific reason rather than a bare "unknown identifier".
"""

from __future__ import annotations

from dataclasses import dataclass

from vizanix_atlas.core.metrics_registry import REGISTRY, Metric

#: MQL's ``market`` table exposes one row per asset. Each entry maps a metric name (as
#: written in a query) to the physical column name in the published
#: ``asset_market_state`` Parquet files. Metrics computed from a model *property*
#: (``reported_volume_24h_usd``, ``venue_count``) are materialised as real columns by
#: :func:`vizanix_atlas.storage.tables.build_asset_market_state_table`, specifically so
#: this mapping can point straight at a column rather than needing SQL to re-derive them.
ASSET_TABLE_COLUMNS: dict[str, str] = {
    "asset": "symbol",
    "asset_id": "asset_id",
    "symbol": "symbol",
    "asset_name": "name",
    "resolution_state": "resolution_state",
    "observed_at": "observed_at",
    "coverage_tier": "coverage_tier",
    "reference_price": "reference_price__value",
    "reference_price_method": "reference_price__method",
    "reference_price_venue_count": "reference_price__venue_count",
    "included_venue_count": "reference_price__included_venue_count",
    "excluded_venue_count": "reference_price__excluded_venue_count",
    "venue_count": "venue_count",
    "min_qualified_price": "reference_price__min_qualified_price",
    "max_qualified_price": "reference_price__max_qualified_price",
    "price_change_1h": "returns__change_1h",
    "price_change_24h": "returns__change_24h",
    "price_change_7d": "returns__change_7d",
    "price_dispersion_bps": "dispersion__price_dispersion_bps",
    "weighted_mad_bps": "dispersion__weighted_mad_bps",
    "p10_p90_spread_bps": "dispersion__p10_p90_spread_bps",
    "reported_spot_volume_24h_usd": "volume__reported_spot_volume_24h_usd",
    "reported_perp_volume_24h_usd": "volume__reported_perp_volume_24h_usd",
    "reported_futures_volume_24h_usd": "volume__reported_futures_volume_24h_usd",
    "reported_volume_24h_usd": "reported_volume_24h_usd",
    "realised_volatility_1h": "volatility__realised_1h",
    "realised_volatility_4h": "volatility__realised_4h",
    "realised_volatility_24h": "volatility__realised_24h",
    "realised_volatility_7d": "volatility__realised_7d",
    "atr_14_bps": "volatility__atr_14_bps",
    "best_spread_bps": "liquidity__best_spread_bps",
    "median_spread_bps": "liquidity__median_spread_bps",
    "liquidity_venue_count": "liquidity__venue_count",
    "largest_venue_liquidity_share": "liquidity__largest_venue_share",
    "liquidity_concentration_hhi": "liquidity__concentration_hhi",
    "spot_venue_count": "structure__spot_venue_count",
    "perp_venue_count": "structure__perp_venue_count",
    "funding_rate_8h_median": "funding__rate_8h_median",
    "funding_rate_annualised_simple": "funding__annualised_simple",
    "funding_venue_count": "funding__venue_count",
    "funding_dispersion_bps": "funding__dispersion_bps",
    "open_interest_usd": "open_interest__total_usd",
    "open_interest_base": "open_interest__total_base",
    "open_interest_venue_count": "open_interest__venue_count",
    "perp_basis_bps_median": "basis__perp_basis_bps_median",
    "futures_basis_bps_median": "basis__futures_basis_bps_median",
    "effective_venue_count": "fragmentation__effective_venue_count",
    "volume_concentration_hhi": "fragmentation__volume_concentration_hhi",
    "largest_venue_volume_share": "fragmentation__largest_venue_volume_share",
    "fragmentation_index": "fragmentation__fragmentation_index",
    "rsi_14": "technical__rsi_14",
    "ema_12": "technical__ema_12",
    "ema_26": "technical__ema_26",
    "crowding_funding_percentile_30d": "crowding__funding_percentile_30d",
    "crowding_oi_to_volume": "crowding__oi_to_volume",
    "crowding_perp_dominance": "crowding__perp_dominance",
    "genome_status": "genome__status",
    "coverage_ratio": "quality__coverage__coverage_ratio",
    "freshest_observation_age_seconds": "quality__freshness__freshest_observation_age_seconds",
    "oldest_observation_age_seconds": "quality__freshness__oldest_observation_age_seconds",
    "ambiguous_identity_count": "quality__coverage__ambiguous_identity_count",
    "partial_data": "is_partial",
}

#: Columns kept out of MQL's ``SELECT *`` expansion. Large JSON-serialised sequence
#: columns (order-book bands, per-venue liquidity, provenance references) belong to
#: the detailed tables, not a query result meant to be read as a table of numbers.
_STAR_EXCLUDED_SUFFIXES = ("_ref", "note")


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """One MQL-visible column: its metric name, physical column, and metadata."""

    name: str
    physical_column: str
    metric: Metric | None


def _registry_metric(name: str) -> Metric | None:
    """Look up ``name`` in the metric registry, tolerating a name MQL adds itself
    (``asset``) that the registry does not declare under exactly that spelling.
    """
    try:
        return REGISTRY.get(name)
    except Exception:  # noqa: BLE001 - REGISTRY.get raises MetricUnknown; absence is fine here
        return None


def market_table_columns() -> dict[str, ColumnInfo]:
    """Return every column MQL's ``market`` table exposes, keyed by metric name."""
    return {
        name: ColumnInfo(name=name, physical_column=physical, metric=_registry_metric(name))
        for name, physical in ASSET_TABLE_COLUMNS.items()
    }


def star_columns() -> tuple[str, ...]:
    """Return the metric names ``SELECT *`` expands to.

    Excludes columns that are not useful in a flat result table (nothing here is
    hidden from ``atlas query``; a caller can still name any excluded column
    explicitly).
    """
    return tuple(
        name
        for name in ASSET_TABLE_COLUMNS
        if not any(name.endswith(suffix) for suffix in _STAR_EXCLUDED_SUFFIXES)
    )


def filterable_column_names() -> frozenset[str]:
    """Return the metric names usable in a ``WHERE`` or ``ORDER BY`` clause."""
    return frozenset(ASSET_TABLE_COLUMNS)
