"""The metric registry.

Every metric Atlas publishes is declared exactly once, here. Three consumers read
these declarations, which is why duplicating them would drift:

- ``docs/METRICS.md`` is generated from the registry (``scripts/generate.py``), and
  CI fails if the committed file no longer matches.
- MQL validates identifiers against the registry, so an unknown metric is a
  semantic error with a suggestion rather than a confusing SQL failure.
- The dataset builder uses ``column`` and ``unit`` to lay out the published tables.

A metric that is not declared here cannot be selected, filtered on, or published.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from vizanix_atlas.core.errors import MetricUnknown
from vizanix_atlas.core.versions import METHODOLOGY_VERSION


class Unit(StrEnum):
    """The unit a metric is expressed in.

    Declared explicitly because the difference between a ratio and a percentage,
    or between base units and USD, is the most common source of silent error in
    market data.
    """

    USD = "usd"
    BASE_ASSET = "base_asset"
    QUOTE_ASSET = "quote_asset"
    CONTRACTS = "contracts"
    RATIO = "ratio"
    BASIS_POINTS = "bps"
    PERCENT = "percent"
    COUNT = "count"
    SECONDS = "seconds"
    HOURS = "hours"
    Z_SCORE = "z_score"
    PERCENTILE = "percentile"
    PRICE = "price"
    TIMESTAMP = "timestamp"
    IDENTIFIER = "identifier"
    ENUM = "enum"
    BOOLEAN = "boolean"


class Scope(StrEnum):
    """The dimension a metric is defined over.

    A metric's scope determines which table it lives in and which MQL source can
    select it.
    """

    ASSET = "asset"
    VENUE_ASSET = "venue_asset"
    INSTRUMENT = "instrument"
    VENUE = "venue"
    MARKET = "market"


class MissingSemantics(StrEnum):
    """What a null in this metric means.

    Atlas refuses to collapse these into one another (see ``docs/QUALITY.md``).
    """

    NOT_COLLECTED = "not_collected"
    NOT_SUPPORTED = "not_supported"
    INSUFFICIENT_COVERAGE = "insufficient_coverage"
    INSUFFICIENT_HISTORY = "insufficient_history"
    INVALID_INPUT = "invalid_input"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class Metric:
    """A published metric's complete definition.

    Attributes:
        name: The identifier used in MQL and in published column names.
        column: Physical column name, when it differs from ``name``.
        dtype: Logical type: ``float``, ``int``, ``str``, ``bool`` or ``timestamp``.
        unit: The unit the value is expressed in.
        scope: The dimension the metric is defined over.
        description: One sentence, rendered into the data dictionary.
        formula: The derivation, in the notation used by ``docs/METHODOLOGY.md``.
            ``None`` for values read directly from a venue.
        inputs: Metric or raw-field names this value is computed from.
        minimum_coverage: Fewest qualifying venues required before Atlas will
            publish a value, for cross-venue aggregates.
        minimum_observations: Fewest historical observations required, for metrics
            over a rolling window.
        missing_semantics: What a null means for this metric.
        caveats: Things a user must know before relying on the value.
        filterable: Whether MQL may use it in a ``WHERE`` clause.
        since: Schema version the metric first appeared in.

    """

    name: str
    dtype: str
    unit: Unit
    scope: Scope
    description: str
    column: str | None = None
    formula: str | None = None
    inputs: tuple[str, ...] = ()
    minimum_coverage: int | None = None
    minimum_observations: int | None = None
    missing_semantics: MissingSemantics = MissingSemantics.NOT_COLLECTED
    caveats: tuple[str, ...] = ()
    filterable: bool = True
    methodology_version: str = METHODOLOGY_VERSION
    since: str = "1.0.0"

    @property
    def physical_column(self) -> str:
        """The column name used in published Parquet files."""
        return self.column or self.name


@dataclass(slots=True)
class MetricRegistry:
    """An ordered, name-indexed collection of metric definitions."""

    _metrics: dict[str, Metric] = field(default_factory=dict)

    def register(self, metric: Metric) -> Metric:
        """Add ``metric``.

        Raises:
            ValueError: If the name is already registered. A duplicate declaration
                means two parts of the codebase disagree about a metric's meaning.

        """
        if metric.name in self._metrics:
            raise ValueError(f"metric already registered: {metric.name}")
        self._metrics[metric.name] = metric
        return metric

    def get(self, name: str) -> Metric:
        """Look up a metric by name.

        Raises:
            MetricUnknown: If unknown. The message includes close matches, because
                the most common cause is a typo or a remembered older name.

        """
        try:
            return self._metrics[name]
        except KeyError:
            suggestions = difflib.get_close_matches(name, self._metrics, n=3, cutoff=0.6)
            raise MetricUnknown(
                f"unknown metric {name!r}",
                suggestions=suggestions,
            ) from None

    def __contains__(self, name: object) -> bool:
        return name in self._metrics

    def __iter__(self) -> Iterator[Metric]:
        return iter(self._metrics.values())

    def __len__(self) -> int:
        return len(self._metrics)

    def names(self) -> tuple[str, ...]:
        """Return every registered metric name in declaration order."""
        return tuple(self._metrics)

    def by_scope(self, scope: Scope) -> tuple[Metric, ...]:
        """Return the metrics defined over ``scope``, in declaration order."""
        return tuple(m for m in self._metrics.values() if m.scope is scope)

    def filterable_names(self, scope: Scope) -> tuple[str, ...]:
        """Return names MQL may filter on within ``scope``."""
        return tuple(m.name for m in self.by_scope(scope) if m.filterable)


def _build_registry() -> MetricRegistry:
    """Declare every published metric.

    Grouped to match the sections of ``docs/METHODOLOGY.md``.
    """
    reg = MetricRegistry()
    add = reg.register

    # ---------------------------------------------------------------- identity
    add(
        Metric(
            name="asset",
            dtype="str",
            unit=Unit.IDENTIFIER,
            scope=Scope.ASSET,
            description="Canonical asset symbol, unique within the published universe.",
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="asset_id",
            dtype="str",
            unit=Unit.IDENTIFIER,
            scope=Scope.ASSET,
            description="Canonical asset identifier, for example asset:native:bitcoin:BTC.",
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="asset_name",
            dtype="str",
            unit=Unit.IDENTIFIER,
            scope=Scope.ASSET,
            description="Human-readable asset name where at least one venue supplies one.",
            filterable=False,
        )
    )
    add(
        Metric(
            name="resolution_state",
            dtype="str",
            unit=Unit.ENUM,
            scope=Scope.ASSET,
            description=(
                "Identity confidence: resolved, probable, ambiguous, unresolved or manual_override."
            ),
            caveats=("Only resolved and manual_override identities are aggregated across venues.",),
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="observed_at",
            dtype="timestamp",
            unit=Unit.TIMESTAMP,
            scope=Scope.ASSET,
            description="Snapshot effective time this state row belongs to.",
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="coverage_tier",
            dtype="str",
            unit=Unit.ENUM,
            scope=Scope.ASSET,
            description="Deepest collection tier reached for this asset: A, B or C.",
            caveats=("Order-book metrics exist only for assets that reached tier B.",),
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )

    # --------------------------------------------------------- reference price
    add(
        Metric(
            name="reference_price",
            dtype="float",
            unit=Unit.USD,
            scope=Scope.ASSET,
            description="Global reference price: a robust, liquidity-weighted cross-venue estimate.",
            formula=(
                "weighted_median({p_v}, {w_v}) over qualified venue observations v, where "
                "p_v is the USD-normalised mid or last price and w_v is the capped weight"
            ),
            inputs=("venue_price_usd", "venue_reference_weight"),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
            caveats=(
                "Not a traded price and not executable.",
                "Named reference_price rather than price because no single true price exists.",
            ),
        )
    )
    add(
        Metric(
            name="reference_price_method",
            dtype="str",
            unit=Unit.ENUM,
            scope=Scope.ASSET,
            description=(
                "How reference_price was produced: spot_weighted_median, "
                "single_venue_spot, derivative_fallback or unavailable."
            ),
            caveats=("derivative_fallback means no qualifying spot venue was available.",),
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="reference_price_venue_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.ASSET,
            description="Number of venue observations included in reference_price.",
            inputs=("included_venue_count",),
        )
    )
    add(
        Metric(
            name="included_venue_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.ASSET,
            description="Venue observations that passed every qualification test.",
        )
    )
    add(
        Metric(
            name="excluded_venue_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.ASSET,
            description="Venue observations considered but excluded, with reasons recorded.",
            caveats=(
                "included_venue_count + excluded_venue_count equals observations considered.",
            ),
        )
    )
    add(
        Metric(
            name="venue_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.ASSET,
            description="Distinct venues contributing any qualified observation for this asset.",
        )
    )
    add(
        Metric(
            name="instrument_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.ASSET,
            description="Distinct instruments contributing any qualified observation.",
        )
    )

    # ------------------------------------------------------------- dispersion
    add(
        Metric(
            name="price_dispersion_bps",
            dtype="float",
            unit=Unit.BASIS_POINTS,
            scope=Scope.ASSET,
            description="Weighted median absolute deviation of venue prices from reference_price.",
            formula="10000 * weighted_median(|p_v - reference_price| / reference_price, w_v)",
            inputs=("reference_price", "venue_price_usd"),
            minimum_coverage=2,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
            caveats=("A robust statistic: one erroneous venue cannot dominate it.",),
        )
    )
    add(
        Metric(
            name="p10_p90_spread_bps",
            dtype="float",
            unit=Unit.BASIS_POINTS,
            scope=Scope.ASSET,
            description="Distance between the 10th and 90th percentile of qualified venue prices.",
            formula="10000 * (quantile(p_v, 0.90) - quantile(p_v, 0.10)) / reference_price",
            inputs=("reference_price", "venue_price_usd"),
            minimum_coverage=4,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
        )
    )
    add(
        Metric(
            name="min_qualified_price",
            dtype="float",
            unit=Unit.USD,
            scope=Scope.ASSET,
            description="Lowest USD-normalised price among included venue observations.",
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
        )
    )
    add(
        Metric(
            name="max_qualified_price",
            dtype="float",
            unit=Unit.USD,
            scope=Scope.ASSET,
            description="Highest USD-normalised price among included venue observations.",
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
        )
    )

    # ----------------------------------------------------------------- volume
    add(
        Metric(
            name="reported_spot_volume_24h_usd",
            dtype="float",
            unit=Unit.USD,
            scope=Scope.ASSET,
            description="Sum of venue-reported 24-hour spot volume, USD-normalised.",
            formula="sum over deduplicated spot instruments of quote_volume_24h * fx_rate_to_usd",
            inputs=("venue_quote_volume_24h", "fx_rate_to_usd"),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
            caveats=(
                "Venue-reported and not independently verified by Atlas.",
                "Instruments are deduplicated per venue so one market is counted once.",
            ),
        )
    )
    add(
        Metric(
            name="reported_perp_volume_24h_usd",
            dtype="float",
            unit=Unit.USD,
            scope=Scope.ASSET,
            description="Sum of venue-reported 24-hour perpetual volume, USD-normalised.",
            inputs=("venue_quote_volume_24h", "fx_rate_to_usd"),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
            caveats=("Venue-reported and not independently verified by Atlas.",),
        )
    )
    add(
        Metric(
            name="reported_futures_volume_24h_usd",
            dtype="float",
            unit=Unit.USD,
            scope=Scope.ASSET,
            description="Sum of venue-reported 24-hour dated-futures volume, USD-normalised.",
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
            caveats=("Venue-reported and not independently verified by Atlas.",),
        )
    )
    add(
        Metric(
            name="reported_volume_24h_usd",
            dtype="float",
            unit=Unit.USD,
            scope=Scope.ASSET,
            description="Spot, perpetual and dated-futures reported volume combined.",
            formula=(
                "reported_spot_volume_24h_usd + reported_perp_volume_24h_usd "
                "+ reported_futures_volume_24h_usd"
            ),
            inputs=(
                "reported_spot_volume_24h_usd",
                "reported_perp_volume_24h_usd",
                "reported_futures_volume_24h_usd",
            ),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
            caveats=(
                "Combines spot and derivative activity, which are not equivalent exposures.",
                "Use the per-segment metrics when that distinction matters.",
            ),
        )
    )
    add(
        Metric(
            name="perp_to_spot_volume_ratio",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description="Reported perpetual volume divided by reported spot volume.",
            formula="reported_perp_volume_24h_usd / reported_spot_volume_24h_usd",
            inputs=("reported_perp_volume_24h_usd", "reported_spot_volume_24h_usd"),
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
        )
    )

    # -------------------------------------------------------------- liquidity
    for bps in (5, 10, 25, 50, 100):
        for side in ("bid", "ask"):
            add(
                Metric(
                    name=f"observable_{side}_depth_{bps}bps_usd",
                    dtype="float",
                    unit=Unit.USD,
                    scope=Scope.ASSET,
                    description=(
                        f"Visible {side} liquidity within {bps} bps of reference_price, "
                        "summed across sampled venues."
                    ),
                    formula=(
                        f"sum over sampled instruments of sum(price_i * size_i * fx_rate_to_usd) "
                        f"for levels within {bps} bps of reference_price on the {side} side"
                    ),
                    inputs=("reference_price", "order_book_levels", "fx_rate_to_usd"),
                    minimum_coverage=1,
                    missing_semantics=MissingSemantics.NOT_COLLECTED,
                    caveats=(
                        "Visible resting liquidity at one sampling instant only.",
                        "Hidden and iceberg liquidity is not represented.",
                        "Only instruments selected into tier B are sampled.",
                    ),
                )
            )
    add(
        Metric(
            name="observable_depth_50bps_usd",
            dtype="float",
            unit=Unit.USD,
            scope=Scope.ASSET,
            description="Combined bid and ask visible liquidity within 50 bps of reference_price.",
            formula="observable_bid_depth_50bps_usd + observable_ask_depth_50bps_usd",
            inputs=("observable_bid_depth_50bps_usd", "observable_ask_depth_50bps_usd"),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.NOT_COLLECTED,
        )
    )
    add(
        Metric(
            name="best_spread_bps",
            dtype="float",
            unit=Unit.BASIS_POINTS,
            scope=Scope.ASSET,
            description="Narrowest top-of-book spread observed across sampled venues.",
            formula="min over sampled instruments of 10000 * (ask - bid) / ((ask + bid) / 2)",
            inputs=("venue_spread_bps",),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.NOT_COLLECTED,
            caveats=("Quoted spread, not an achieved execution cost.",),
        )
    )
    add(
        Metric(
            name="median_spread_bps",
            dtype="float",
            unit=Unit.BASIS_POINTS,
            scope=Scope.ASSET,
            description="Median top-of-book spread across sampled venues.",
            inputs=("venue_spread_bps",),
            minimum_coverage=2,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
        )
    )
    for notional in (10_000, 100_000, 1_000_000):
        for side in ("buy", "sell"):
            label = f"{notional // 1000}k" if notional < 1_000_000 else "1m"
            add(
                Metric(
                    name=f"observable_{side}_impact_{label}_bps",
                    dtype="float",
                    unit=Unit.BASIS_POINTS,
                    scope=Scope.ASSET,
                    description=(
                        f"Volume-weighted cost of consuming {notional:,} USD of visible "
                        f"{'ask' if side == 'buy' else 'bid'} liquidity, versus reference_price."
                    ),
                    formula=(
                        "10000 * (vwap_of_consumed_levels - reference_price) / reference_price, "
                        "walking the aggregated book outward; null when visible depth is insufficient"
                    ),
                    inputs=("reference_price", "order_book_levels"),
                    minimum_coverage=1,
                    missing_semantics=MissingSemantics.NOT_COLLECTED,
                    caveats=(
                        "An observable order-book impact estimate, not predicted slippage.",
                        "Excludes fees, latency, market response, hidden liquidity and cancellation.",
                        "Null when the sampled book does not contain enough visible depth.",
                    ),
                )
            )
    add(
        Metric(
            name="liquidity_venue_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.ASSET,
            description="Venues contributing a sampled order book to the liquidity metrics.",
            caveats=("Usually lower than venue_count, because books are sampled selectively.",),
        )
    )
    add(
        Metric(
            name="largest_venue_liquidity_share",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description="Share of 50 bps visible depth contributed by the single largest venue.",
            formula="max_v(depth_50bps_usd_v) / sum_v(depth_50bps_usd_v)",
            inputs=("observable_depth_50bps_usd",),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.NOT_COLLECTED,
        )
    )
    add(
        Metric(
            name="liquidity_concentration_hhi",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description="Herfindahl-Hirschman index of 50 bps visible depth across venues.",
            formula="sum_v (share_v ^ 2), where share_v is venue v's fraction of 50 bps depth",
            inputs=("observable_depth_50bps_usd",),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.NOT_COLLECTED,
            caveats=("A structural measure. Atlas does not label any value good or bad.",),
        )
    )

    # ------------------------------------------------------------ derivatives
    add(
        Metric(
            name="funding_rate_8h_median",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description="Median 8-hour-equivalent funding rate across qualified perpetuals.",
            formula="median_v(funding_rate_raw_v * 8 / funding_interval_hours_v)",
            inputs=("venue_funding_rate_raw", "venue_funding_interval_hours"),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
            caveats=(
                "Linear rescaling to an 8-hour window. No compounding is applied.",
                "Venues with undocumented funding semantics are excluded, not assumed.",
            ),
        )
    )
    add(
        Metric(
            name="funding_rate_annualised_simple",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description="funding_rate_8h_median scaled to a year with simple, non-compounded scaling.",
            formula="funding_rate_8h_median * 3 * 365",
            inputs=("funding_rate_8h_median",),
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
            caveats=(
                "Simple scaling, deliberately not compounded.",
                "Assumes the current rate persists, which it does not.",
            ),
        )
    )
    add(
        Metric(
            name="funding_venue_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.ASSET,
            description="Venues contributing a funding observation with documented semantics.",
        )
    )
    add(
        Metric(
            name="funding_dispersion_bps",
            dtype="float",
            unit=Unit.BASIS_POINTS,
            scope=Scope.ASSET,
            description="Spread between the highest and lowest 8-hour-equivalent funding rate.",
            formula="10000 * (max_v(funding_8h_v) - min_v(funding_8h_v))",
            inputs=("venue_funding_rate_raw",),
            minimum_coverage=2,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
        )
    )
    add(
        Metric(
            name="open_interest_usd",
            dtype="float",
            unit=Unit.USD,
            scope=Scope.ASSET,
            description="Aggregate derivative open interest, USD-normalised.",
            formula=(
                "sum over derivative instruments of the venue's USD open interest where "
                "published, else oi_base_equivalent * reference_price"
            ),
            inputs=("venue_open_interest_raw", "reference_price"),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
            caveats=(
                "Only instruments with a known contract multiplier are converted.",
                "Instruments whose open-interest unit is ambiguous are excluded, not guessed.",
            ),
        )
    )
    add(
        Metric(
            name="open_interest_base",
            dtype="float",
            unit=Unit.BASE_ASSET,
            scope=Scope.ASSET,
            description="Aggregate derivative open interest expressed in the base asset.",
            inputs=("venue_open_interest_raw",),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
        )
    )
    add(
        Metric(
            name="open_interest_venue_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.ASSET,
            description="Venues contributing a convertible open-interest observation.",
        )
    )
    add(
        Metric(
            name="oi_to_volume_ratio",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description="Open interest divided by reported 24-hour derivative volume.",
            formula=(
                "open_interest_usd / (reported_perp_volume_24h_usd "
                "+ reported_futures_volume_24h_usd)"
            ),
            inputs=("open_interest_usd", "reported_perp_volume_24h_usd"),
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
        )
    )
    add(
        Metric(
            name="perp_basis_bps_median",
            dtype="float",
            unit=Unit.BASIS_POINTS,
            scope=Scope.ASSET,
            description="Median perpetual mark price premium over reference_price.",
            formula="median_v(10000 * (mark_price_usd_v - reference_price) / reference_price)",
            inputs=("venue_mark_price_usd", "reference_price"),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
            caveats=("Computed from venue mark prices, never from last traded prices.",),
        )
    )
    add(
        Metric(
            name="futures_basis_bps_median",
            dtype="float",
            unit=Unit.BASIS_POINTS,
            scope=Scope.ASSET,
            description="Median dated-futures mark price premium over reference_price.",
            formula="median_v(10000 * (mark_price_usd_v - reference_price) / reference_price)",
            inputs=("venue_mark_price_usd", "reference_price"),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
            caveats=(
                "Aggregates across expiries, so it is not a term-structure measurement.",
                "Computed from venue mark prices, never from last traded prices.",
            ),
        )
    )
    add(
        Metric(
            name="perp_venue_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.ASSET,
            description="Venues listing a qualified perpetual on this asset.",
        )
    )
    add(
        Metric(
            name="spot_venue_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.ASSET,
            description="Venues listing a qualified spot market on this asset.",
        )
    )

    # ------------------------------------------------------------- volatility
    for window in ("1h", "4h", "24h", "7d"):
        add(
            Metric(
                name=f"realised_volatility_{window}",
                dtype="float",
                unit=Unit.RATIO,
                scope=Scope.ASSET,
                description=f"Annualised realised volatility of reference_price over {window}.",
                formula=(
                    "stdev(log(p_t / p_{t-1})) * sqrt(observations_per_year), computed on the "
                    "snapshot series of reference_price"
                ),
                inputs=("reference_price",),
                minimum_observations={"1h": 4, "4h": 12, "24h": 48, "7d": 240}[window],
                missing_semantics=MissingSemantics.INSUFFICIENT_HISTORY,
                caveats=(
                    "Sampled at the snapshot cadence, so intra-snapshot movement is invisible.",
                    "Null rather than approximate when the window has too few observations.",
                ),
            )
        )
    for window in ("1h", "24h", "7d"):
        add(
            Metric(
                name=f"price_change_{window}",
                dtype="float",
                unit=Unit.RATIO,
                scope=Scope.ASSET,
                description=f"Fractional change in reference_price over {window}.",
                formula="reference_price / reference_price_lagged - 1",
                inputs=("reference_price",),
                minimum_observations=2,
                missing_semantics=MissingSemantics.INSUFFICIENT_HISTORY,
            )
        )
    add(
        Metric(
            name="volatility_window_coverage",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description="Fraction of expected snapshots present in the 24-hour volatility window.",
            formula="observations_present / observations_expected",
            caveats=("A value below 1 means scheduled snapshots are missing from the window.",),
        )
    )

    # ------------------------------------------------------------- technicals
    add(
        Metric(
            name="rsi_14",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description="14-period Wilder relative strength index of the reference_price series.",
            formula="100 - 100 / (1 + average_gain / average_loss), Wilder smoothing",
            inputs=("reference_price",),
            minimum_observations=15,
            missing_semantics=MissingSemantics.INSUFFICIENT_HISTORY,
            caveats=(
                "A measurement of the input series, not a recommendation.",
                "Computed on snapshot-cadence data, so it is not comparable to a chart indicator.",
            ),
        )
    )
    add(
        Metric(
            name="atr_14_bps",
            dtype="float",
            unit=Unit.BASIS_POINTS,
            scope=Scope.ASSET,
            description="14-period average true range of reference_price, relative to price.",
            formula="10000 * wilder_mean(true_range_t) / reference_price",
            inputs=("reference_price",),
            minimum_observations=15,
            missing_semantics=MissingSemantics.INSUFFICIENT_HISTORY,
        )
    )
    for span in (12, 26):
        add(
            Metric(
                name=f"ema_{span}",
                dtype="float",
                unit=Unit.USD,
                scope=Scope.ASSET,
                description=f"{span}-period exponential moving average of reference_price.",
                formula=f"EMA with alpha = 2 / ({span} + 1)",
                inputs=("reference_price",),
                minimum_observations=span,
                missing_semantics=MissingSemantics.INSUFFICIENT_HISTORY,
            )
        )

    # ---------------------------------------------------------- fragmentation
    add(
        Metric(
            name="effective_venue_count",
            dtype="float",
            unit=Unit.COUNT,
            scope=Scope.ASSET,
            description="Inverse-HHI effective number of venues by reported volume share.",
            formula="1 / sum_v(volume_share_v ^ 2)",
            inputs=("reported_volume_24h_usd",),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
            caveats=(
                "Five venues at equal share give 5; five venues with one dominant give near 1.",
            ),
        )
    )
    add(
        Metric(
            name="volume_concentration_hhi",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description="Herfindahl-Hirschman index of reported volume share across venues.",
            formula="sum_v (volume_share_v ^ 2)",
            inputs=("reported_volume_24h_usd",),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
        )
    )
    add(
        Metric(
            name="largest_venue_volume_share",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description="Reported volume share of the single largest venue.",
            inputs=("reported_volume_24h_usd",),
            minimum_coverage=1,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
        )
    )
    add(
        Metric(
            name="fragmentation_index",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description=(
                "Composite market fragmentation on a 0-to-1 scale, from venue breadth, "
                "volume concentration and price dispersion."
            ),
            formula=(
                "mean of three bounded components: (1 - 1/effective_venue_count), "
                "(1 - largest_venue_volume_share), min(price_dispersion_bps / 50, 1)"
            ),
            inputs=("effective_venue_count", "largest_venue_volume_share", "price_dispersion_bps"),
            minimum_coverage=2,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
            caveats=(
                "A composite. Read the three components when the driver matters.",
                "Higher means more fragmented, not better or worse.",
            ),
        )
    )
    add(
        Metric(
            name="spot_derivative_fragmentation",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description="Absolute price separation between spot and derivative venues, as a ratio.",
            formula="|median(spot_price_usd) - median(derivative_mark_usd)| / reference_price",
            inputs=("venue_price_usd", "venue_mark_price_usd"),
            minimum_coverage=2,
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
        )
    )

    # ------------------------------------------------------- pressure vectors
    for component, description in (
        ("book_imbalance", "Visible bid depth minus ask depth, scaled by their sum, at 50 bps."),
        ("funding_deviation", "Current 8-hour funding relative to its own recent distribution."),
        ("basis_deviation", "Current perpetual basis relative to its own recent distribution."),
        ("oi_change", "Fractional change in USD open interest over the comparison window."),
        ("volume_acceleration", "Reported volume relative to its recent baseline."),
    ):
        add(
            Metric(
                name=f"pressure_{component}",
                dtype="float",
                unit=Unit.RATIO,
                scope=Scope.ASSET,
                description=description,
                inputs=("reference_price",),
                missing_semantics=MissingSemantics.INSUFFICIENT_HISTORY,
                caveats=(
                    "A component of a vector, deliberately not collapsed into a single score.",
                    "Not a directional signal and not a recommendation.",
                ),
            )
        )
    add(
        Metric(
            name="pressure_components_available",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.ASSET,
            description="How many pressure components could be computed for this asset.",
            caveats=("Compare vectors only between assets with the same components available.",),
        )
    )

    # ----------------------------------------------------------- crowding
    add(
        Metric(
            name="crowding_funding_percentile_30d",
            dtype="float",
            unit=Unit.PERCENTILE,
            scope=Scope.ASSET,
            description="Percentile rank of current 8-hour funding within its own 30-day history.",
            formula="empirical percentile of funding_rate_8h_median within its 30-day window",
            inputs=("funding_rate_8h_median",),
            minimum_observations=720,
            missing_semantics=MissingSemantics.INSUFFICIENT_HISTORY,
            caveats=(
                "Null until a genuine 30-day window exists. Atlas does not label a "
                "shorter window as 30-day.",
            ),
        )
    )
    add(
        Metric(
            name="crowding_oi_to_volume",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description="Open interest relative to reported derivative volume.",
            inputs=("oi_to_volume_ratio",),
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
        )
    )
    add(
        Metric(
            name="crowding_perp_dominance",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description="Perpetual share of combined reported spot and perpetual volume.",
            formula=(
                "reported_perp_volume_24h_usd / (reported_perp_volume_24h_usd "
                "+ reported_spot_volume_24h_usd)"
            ),
            inputs=("reported_perp_volume_24h_usd", "reported_spot_volume_24h_usd"),
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
        )
    )
    add(
        Metric(
            name="crowding_components_available",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.ASSET,
            description="How many crowding components could be computed for this asset.",
        )
    )

    # ------------------------------------------------------------- quality
    add(
        Metric(
            name="coverage_ratio",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.ASSET,
            description="Venues that produced an observation, over venues that list the asset.",
            formula="included_venue_count / listing_venue_count",
            caveats=("Always between 0 and 1 inclusive.",),
        )
    )
    add(
        Metric(
            name="freshest_observation_age_seconds",
            dtype="float",
            unit=Unit.SECONDS,
            scope=Scope.ASSET,
            description="Age of the newest included observation at snapshot effective time.",
        )
    )
    add(
        Metric(
            name="oldest_observation_age_seconds",
            dtype="float",
            unit=Unit.SECONDS,
            scope=Scope.ASSET,
            description="Age of the oldest included observation at snapshot effective time.",
            caveats=("Compare against the freshest age to see how uneven a state's inputs are.",),
        )
    )
    add(
        Metric(
            name="ambiguous_identity_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.ASSET,
            description="Venue symbols that plausibly matched this asset but were not merged.",
            caveats=("A non-zero value means some venue activity is deliberately excluded.",),
        )
    )
    add(
        Metric(
            name="partial_data",
            dtype="bool",
            unit=Unit.BOOLEAN,
            scope=Scope.ASSET,
            description="Whether any expected venue failed to contribute to this state.",
        )
    )
    add(
        Metric(
            name="genome_status",
            dtype="str",
            unit=Unit.ENUM,
            scope=Scope.ASSET,
            description="Market Genome lifecycle: insufficient_history, warming_up or available.",
            caveats=("Genome features are null unless the status is available.",),
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )

    # ------------------------------------------------------- venue-asset scope
    add(
        Metric(
            name="venue_price_usd",
            dtype="float",
            unit=Unit.USD,
            scope=Scope.VENUE_ASSET,
            description="One venue's USD-normalised price for an asset.",
            formula="venue mid price when both sides quote, else last price, times fx_rate_to_usd",
            minimum_coverage=1,
            missing_semantics=MissingSemantics.NOT_COLLECTED,
        )
    )
    add(
        Metric(
            name="venue_reference_weight",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.VENUE_ASSET,
            description="Normalised, capped weight this venue received in reference_price.",
            formula="see docs/METHODOLOGY.md, section Global reference price",
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="venue_deviation_bps",
            dtype="float",
            unit=Unit.BASIS_POINTS,
            scope=Scope.VENUE_ASSET,
            description="This venue's price relative to reference_price, in basis points.",
            formula="10000 * (venue_price_usd - reference_price) / reference_price",
            inputs=("venue_price_usd", "reference_price"),
        )
    )
    add(
        Metric(
            name="venue_spread_bps",
            dtype="float",
            unit=Unit.BASIS_POINTS,
            scope=Scope.VENUE_ASSET,
            description="Top-of-book spread on this venue, in basis points of the mid.",
            formula="10000 * (ask - bid) / ((ask + bid) / 2)",
            missing_semantics=MissingSemantics.NOT_COLLECTED,
            caveats=("Quoted spread at one instant, not an achieved execution cost.",),
        )
    )
    add(
        Metric(
            name="venue_quote_volume_24h",
            dtype="float",
            unit=Unit.QUOTE_ASSET,
            scope=Scope.VENUE_ASSET,
            description="Venue-reported 24-hour volume in the instrument's quote currency.",
            missing_semantics=MissingSemantics.NOT_SUPPORTED,
            caveats=(
                "Venue-reported. Some venues publish only base volume, in which case this is null.",
            ),
        )
    )
    add(
        Metric(
            name="venue_volume_share",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.VENUE_ASSET,
            description="This venue's share of the asset's reported USD volume.",
            inputs=("reported_volume_24h_usd",),
        )
    )
    add(
        Metric(
            name="venue_liquidity_share",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.VENUE_ASSET,
            description="This venue's share of the asset's 50 bps visible depth.",
            inputs=("observable_depth_50bps_usd",),
            missing_semantics=MissingSemantics.NOT_COLLECTED,
        )
    )
    add(
        Metric(
            name="venue_funding_rate_raw",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.VENUE_ASSET,
            description="Funding rate exactly as the venue publishes it, before rescaling.",
            missing_semantics=MissingSemantics.NOT_SUPPORTED,
            caveats=("Interpret only together with venue_funding_interval_hours.",),
        )
    )
    add(
        Metric(
            name="venue_funding_interval_hours",
            dtype="float",
            unit=Unit.HOURS,
            scope=Scope.VENUE_ASSET,
            description="The venue's native funding interval in hours.",
            missing_semantics=MissingSemantics.NOT_SUPPORTED,
            caveats=(
                "Observed intervals range from 1 to 8 hours across venues.",
                "Derived from the venue's own funding timestamps where it does not publish it.",
            ),
        )
    )
    add(
        Metric(
            name="venue_mark_price_usd",
            dtype="float",
            unit=Unit.USD,
            scope=Scope.VENUE_ASSET,
            description="Venue mark price, USD-normalised.",
            missing_semantics=MissingSemantics.NOT_SUPPORTED,
        )
    )
    add(
        Metric(
            name="venue_open_interest_raw",
            dtype="float",
            unit=Unit.CONTRACTS,
            scope=Scope.VENUE_ASSET,
            description="Open interest exactly as the venue publishes it, before conversion.",
            missing_semantics=MissingSemantics.NOT_SUPPORTED,
            caveats=("Interpret only together with the instrument's open-interest unit.",),
        )
    )
    add(
        Metric(
            name="venue_exclusion_reason",
            dtype="str",
            unit=Unit.ENUM,
            scope=Scope.VENUE_ASSET,
            description="Why this observation was excluded, or null when it was included.",
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )

    # ----------------------------------------------------- instrument scope
    add(
        Metric(
            name="instrument_id",
            dtype="str",
            unit=Unit.IDENTIFIER,
            scope=Scope.INSTRUMENT,
            description="Canonical instrument identifier, for example instrument:okx:linear-perp:BTC-USDT-SWAP.",
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="instrument_type",
            dtype="str",
            unit=Unit.ENUM,
            scope=Scope.INSTRUMENT,
            description="spot, perpetual, future or option.",
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="symbol_native",
            dtype="str",
            unit=Unit.IDENTIFIER,
            scope=Scope.INSTRUMENT,
            description="The venue's own symbol, preserved verbatim so it can be used against its API.",
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="contract_multiplier",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.INSTRUMENT,
            description="Units of the contract-value currency represented by one contract.",
            missing_semantics=MissingSemantics.NOT_SUPPORTED,
            caveats=(
                "Never inferred. Null when the venue does not publish it, which excludes the "
                "instrument from open-interest normalisation.",
            ),
        )
    )
    add(
        Metric(
            name="is_inverse",
            dtype="bool",
            unit=Unit.BOOLEAN,
            scope=Scope.INSTRUMENT,
            description="Whether the contract settles in the base asset rather than the quote.",
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="instrument_active",
            dtype="bool",
            unit=Unit.BOOLEAN,
            scope=Scope.INSTRUMENT,
            description="Whether the venue reported the instrument tradeable at discovery time.",
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="listing_time",
            dtype="timestamp",
            unit=Unit.TIMESTAMP,
            scope=Scope.INSTRUMENT,
            description="When the venue says the instrument was listed.",
            missing_semantics=MissingSemantics.NOT_SUPPORTED,
        )
    )
    add(
        Metric(
            name="expiry",
            dtype="timestamp",
            unit=Unit.TIMESTAMP,
            scope=Scope.INSTRUMENT,
            description="Contract expiry; null for spot and perpetual instruments.",
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="first_seen_at",
            dtype="timestamp",
            unit=Unit.TIMESTAMP,
            scope=Scope.INSTRUMENT,
            description="First Atlas discovery run that observed this instrument.",
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="last_seen_at",
            dtype="timestamp",
            unit=Unit.TIMESTAMP,
            scope=Scope.INSTRUMENT,
            description="Most recent Atlas discovery run that observed this instrument.",
            caveats=("An instrument absent from the venue catalogue keeps its last_seen_at.",),
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )

    # ---------------------------------------------------------- venue scope
    add(
        Metric(
            name="venue_id",
            dtype="str",
            unit=Unit.IDENTIFIER,
            scope=Scope.VENUE,
            description="Canonical venue identifier, for example venue:okx.",
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="venue_collection_status",
            dtype="str",
            unit=Unit.ENUM,
            scope=Scope.VENUE,
            description=(
                "success, degraded, failed, disabled or "
                "unavailable_from_collector_network for this generation."
            ),
            caveats=(
                "Describes what Atlas observed from one collector network. Not a "
                "statement about venue reliability.",
            ),
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    add(
        Metric(
            name="venue_instrument_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.VENUE,
            description="Active instruments discovered on this venue.",
        )
    )
    add(
        Metric(
            name="venue_observation_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.VENUE,
            description="Observations this venue contributed to the generation.",
        )
    )
    add(
        Metric(
            name="venue_request_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.VENUE,
            description="HTTP requests Atlas issued to this venue during the run.",
        )
    )
    add(
        Metric(
            name="venue_parse_error_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.VENUE,
            description="Records this venue returned that failed schema validation.",
            caveats=("A sustained non-zero value usually means the venue changed its schema.",),
        )
    )
    add(
        Metric(
            name="venue_rate_limit_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.VENUE,
            description="Rate-limit responses received from this venue during the run.",
        )
    )
    add(
        Metric(
            name="venue_duration_ms",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.VENUE,
            description="Wall-clock duration of this venue's collection, in milliseconds.",
        )
    )
    add(
        Metric(
            name="venue_latest_observation_age_seconds",
            dtype="float",
            unit=Unit.SECONDS,
            scope=Scope.VENUE,
            description="Age of this venue's newest observation at snapshot effective time.",
        )
    )

    # --------------------------------------------------------- market scope
    add(
        Metric(
            name="market_asset_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.MARKET,
            description="Assets with at least one qualified observation in the generation.",
        )
    )
    add(
        Metric(
            name="market_instrument_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.MARKET,
            description="Active instruments observed in the generation.",
        )
    )
    add(
        Metric(
            name="market_venue_count",
            dtype="int",
            unit=Unit.COUNT,
            scope=Scope.MARKET,
            description="Venues that contributed at least one observation.",
        )
    )
    add(
        Metric(
            name="market_reported_spot_volume_24h_usd",
            dtype="float",
            unit=Unit.USD,
            scope=Scope.MARKET,
            description="Reported 24-hour spot volume summed over the universe.",
            caveats=("Venue-reported and not independently verified by Atlas.",),
        )
    )
    add(
        Metric(
            name="market_reported_perp_volume_24h_usd",
            dtype="float",
            unit=Unit.USD,
            scope=Scope.MARKET,
            description="Reported 24-hour perpetual volume summed over the universe.",
            caveats=("Venue-reported and not independently verified by Atlas.",),
        )
    )
    add(
        Metric(
            name="market_open_interest_usd",
            dtype="float",
            unit=Unit.USD,
            scope=Scope.MARKET,
            description="Aggregate derivative open interest over the universe, USD-normalised.",
        )
    )
    add(
        Metric(
            name="market_breadth_advancing",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.MARKET,
            description="Fraction of the universe whose 24-hour reference price change is positive.",
            formula="count(price_change_24h > 0) / count(price_change_24h is not null)",
            inputs=("price_change_24h",),
            missing_semantics=MissingSemantics.INSUFFICIENT_HISTORY,
        )
    )
    add(
        Metric(
            name="market_median_volatility_24h",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.MARKET,
            description="Median 24-hour realised volatility across the universe.",
            inputs=("realised_volatility_24h",),
            missing_semantics=MissingSemantics.INSUFFICIENT_HISTORY,
        )
    )
    add(
        Metric(
            name="market_median_funding_8h",
            dtype="float",
            unit=Unit.RATIO,
            scope=Scope.MARKET,
            description="Median 8-hour-equivalent funding rate across the universe.",
            inputs=("funding_rate_8h_median",),
            missing_semantics=MissingSemantics.INSUFFICIENT_COVERAGE,
        )
    )
    add(
        Metric(
            name="market_universe",
            dtype="str",
            unit=Unit.ENUM,
            scope=Scope.MARKET,
            description="The universe definition these market-wide figures were computed over.",
            caveats=("Market-wide figures are meaningless without their universe.",),
            missing_semantics=MissingSemantics.NOT_APPLICABLE,
        )
    )
    return reg


#: The process-wide metric registry.
REGISTRY: Final = _build_registry()
