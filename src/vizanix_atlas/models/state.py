"""Universal Market State.

``MarketState`` is Atlas's central object: one reproducible description of an
asset's observable global market at one snapshot time. It is composed of nested
typed sections rather than a flat record of hundreds of fields, so that a consumer
can take the part they need and so that a missing section is obviously missing.

Nothing here produces a recommendation. Sections expose measured components, and
:class:`PressureComponents` and :class:`CrowdingComponents` are deliberately
vectors rather than scores (see ``docs/MARKET_STATE.md``).
"""

from __future__ import annotations

from pydantic import Field

from vizanix_atlas.core.numeric import safe_divide
from vizanix_atlas.models.base import AtlasModel
from vizanix_atlas.models.enums import (
    CollectionTier,
    ExclusionReason,
    GenomeStatus,
    InstrumentType,
    PriceSource,
    ReferencePriceMethod,
    ResolutionState,
    Universe,
)
from vizanix_atlas.models.quality import (
    DataQuality,
    Provenance,
    QuoteConversion,
    WindowCoverage,
)


class ReferencePrice(AtlasModel):
    """The global reference price and how it was formed.

    Named ``reference_price`` rather than ``price`` because no single true price
    exists across a fragmented market, and because this value is not executable.
    """

    value: float | None = Field(default=None, gt=0)
    method: ReferencePriceMethod
    venue_count: int = Field(default=0, ge=0)
    included_venue_count: int = Field(default=0, ge=0)
    excluded_venue_count: int = Field(default=0, ge=0)
    excluded_reasons: dict[str, int] = Field(default_factory=dict)
    min_qualified_price: float | None = Field(default=None, gt=0)
    max_qualified_price: float | None = Field(default=None, gt=0)
    provenance_ref: str | None = Field(
        default=None,
        description=(
            "Key into the evidence table. Provenance is referenced rather than inlined "
            "so a state row does not carry a large array (see docs/STORAGE.md)."
        ),
    )

    @property
    def available(self) -> bool:
        """Whether a usable reference price was produced."""
        return self.value is not None


class Dispersion(AtlasModel):
    """How far venue prices sit from each other.

    Uses robust statistics throughout, so that one malfunctioning venue widens the
    measurement without destroying it.
    """

    price_dispersion_bps: float | None = Field(default=None, ge=0)
    weighted_mad_bps: float | None = Field(default=None, ge=0)
    p10_p90_spread_bps: float | None = Field(default=None, ge=0)
    max_absolute_deviation_bps: float | None = Field(default=None, ge=0)
    contributing_venue_count: int = Field(default=0, ge=0)


class VolumeState(AtlasModel):
    """Venue-reported trading volume, segmented by market type.

    Every field is described as *reported*: Atlas relays what venues publish and
    does not independently verify it.
    """

    reported_spot_volume_24h_usd: float | None = Field(default=None, ge=0)
    reported_perp_volume_24h_usd: float | None = Field(default=None, ge=0)
    reported_futures_volume_24h_usd: float | None = Field(default=None, ge=0)
    reported_option_volume_24h_usd: float | None = Field(default=None, ge=0)
    reported_base_volume_24h: float | None = Field(default=None, ge=0)
    instrument_count: int = Field(default=0, ge=0)
    venues_reporting_quote_volume: int = Field(
        default=0,
        ge=0,
        description=(
            "Venues that published quote-currency volume directly. Venues publishing "
            "only base volume are excluded from the USD totals rather than converted "
            "with a price they did not quote against."
        ),
    )

    @property
    def reported_volume_24h_usd(self) -> float | None:
        """Spot, perpetual, futures and option reported volume combined.

        Returns ``None`` when no segment had a value, rather than 0, so that a market
        with no volume data is distinguishable from one with genuinely no volume.
        """
        parts = [
            self.reported_spot_volume_24h_usd,
            self.reported_perp_volume_24h_usd,
            self.reported_futures_volume_24h_usd,
            self.reported_option_volume_24h_usd,
        ]
        present = [p for p in parts if p is not None]
        return sum(present) if present else None

    @property
    def perp_to_spot_ratio(self) -> float | None:
        """Reported perpetual volume over reported spot volume."""
        return safe_divide(self.reported_perp_volume_24h_usd, self.reported_spot_volume_24h_usd)


class DepthBand(AtlasModel):
    """Visible resting liquidity within one distance band of the reference price."""

    distance_bps: int = Field(gt=0)
    bid_depth_usd: float | None = Field(default=None, ge=0)
    ask_depth_usd: float | None = Field(default=None, ge=0)

    @property
    def total_depth_usd(self) -> float | None:
        """Both sides combined, or ``None`` when neither side was measured."""
        present = [d for d in (self.bid_depth_usd, self.ask_depth_usd) if d is not None]
        return sum(present) if present else None


class ImpactEstimate(AtlasModel):
    """The cost of consuming a given notional of visible liquidity.

    Called an *observable order-book impact estimate* rather than slippage, because
    it models only the visible book at one instant: no fees, no latency, no hidden
    liquidity, no market response, no cancellation.
    """

    notional_usd: float = Field(gt=0)
    buy_impact_bps: float | None = Field(default=None, ge=0)
    sell_impact_bps: float | None = Field(default=None, ge=0)
    buy_depth_sufficient: bool = Field(
        default=False,
        description="Whether the sampled book held enough visible ask liquidity to fill.",
    )
    sell_depth_sufficient: bool = False


class VenueLiquidity(AtlasModel):
    """One venue's contribution to an asset's liquidity surface."""

    venue_slug: str
    instrument_id: str
    instrument_type: InstrumentType
    spread_bps: float | None = Field(default=None, ge=0)
    bands: tuple[DepthBand, ...] = ()
    depth_share: float | None = Field(
        default=None, ge=0, le=1, description="This venue's share of 50 bps depth."
    )
    book_truncated: bool = False
    observed_at: int | None = None


class LiquidityState(AtlasModel):
    """An asset's liquidity across venues.

    The surface preserves venue contributions rather than only the aggregate, so a
    consumer can see whether depth is one venue or ten.
    """

    best_spread_bps: float | None = Field(default=None, ge=0)
    median_spread_bps: float | None = Field(default=None, ge=0)
    bands: tuple[DepthBand, ...] = ()
    impacts: tuple[ImpactEstimate, ...] = ()
    by_venue: tuple[VenueLiquidity, ...] = ()
    venue_count: int = Field(default=0, ge=0)
    largest_venue_share: float | None = Field(default=None, ge=0, le=1)
    concentration_hhi: float | None = Field(default=None, ge=0, le=1)
    book_imbalance_50bps: float | None = Field(
        default=None,
        ge=-1,
        le=1,
        description=(
            "(bid depth - ask depth) / (bid depth + ask depth) at 50 bps. A structural "
            "measurement of the visible book, not a directional signal."
        ),
    )

    def band(self, distance_bps: int) -> DepthBand | None:
        """Return the depth band at ``distance_bps``, or ``None`` if not measured."""
        return next((b for b in self.bands if b.distance_bps == distance_bps), None)


class FundingState(AtlasModel):
    """Perpetual funding across venues.

    Native intervals are preserved and the 8-hour equivalent is a linear rescaling,
    never a compounded one. Venues whose funding semantics Atlas cannot document are
    excluded rather than assumed.
    """

    rate_8h_median: float | None = None
    rate_8h_min: float | None = None
    rate_8h_max: float | None = None
    annualised_simple: float | None = Field(
        default=None, description="rate_8h_median * 3 * 365. Simple, deliberately not compounded."
    )
    dispersion_bps: float | None = Field(default=None, ge=0)
    venue_count: int = Field(default=0, ge=0)
    excluded_venue_count: int = Field(default=0, ge=0)
    intervals_observed_hours: tuple[float, ...] = Field(
        default=(),
        description="Distinct native funding intervals among contributing venues, sorted.",
    )
    provenance_ref: str | None = None


class OpenInterestState(AtlasModel):
    """Aggregate derivative open interest.

    Only instruments whose open-interest unit is known, and whose contract
    multiplier is published when the unit is contracts, are converted. The rest are
    counted in ``excluded_instrument_count`` and their raw values remain available.
    """

    total_usd: float | None = Field(default=None, ge=0)
    total_base: float | None = Field(default=None, ge=0)
    perp_usd: float | None = Field(default=None, ge=0)
    futures_usd: float | None = Field(default=None, ge=0)
    option_usd: float | None = Field(default=None, ge=0)
    venue_count: int = Field(default=0, ge=0)
    instrument_count: int = Field(default=0, ge=0)
    excluded_instrument_count: int = Field(
        default=0, ge=0, description="Instruments whose open interest could not be converted."
    )
    provenance_ref: str | None = None

    @property
    def to_volume_ratio_input(self) -> float | None:
        """Derivative open interest used as the numerator of the OI-to-volume ratio."""
        return self.total_usd


class BasisState(AtlasModel):
    """Derivative premium over the reference price.

    Always computed from venue mark prices, because last traded prices on thin
    contracts produce a basis that reflects one stale trade rather than the market.
    """

    perp_basis_bps_median: float | None = None
    perp_basis_bps_min: float | None = None
    perp_basis_bps_max: float | None = None
    futures_basis_bps_median: float | None = None
    price_source: PriceSource = Field(
        default=PriceSource.MARK, description="Which derivative price the basis was computed from."
    )
    venue_count: int = Field(default=0, ge=0)


class LiquidationState(AtlasModel):
    """Liquidation observations, kept venue-scoped by default.

    Liquidation semantics differ so much between venues that an aggregate is only
    published when the contributing venues' semantics are compatible. Otherwise the
    per-venue observations stand alone.
    """

    aggregate_available: bool = False
    aggregate_long_usd: float | None = Field(default=None, ge=0)
    aggregate_short_usd: float | None = Field(default=None, ge=0)
    venue_count: int = Field(default=0, ge=0)
    incompatible_semantics_venue_count: int = Field(default=0, ge=0)
    note: str | None = Field(
        default=None, description="Why an aggregate was or was not published."
    )


class VolatilityState(AtlasModel):
    """Realised volatility over several windows, with window coverage attached."""

    realised_1h: float | None = Field(default=None, ge=0)
    realised_4h: float | None = Field(default=None, ge=0)
    realised_24h: float | None = Field(default=None, ge=0)
    realised_7d: float | None = Field(default=None, ge=0)
    atr_14_bps: float | None = Field(default=None, ge=0)
    sampling_interval_minutes: int = Field(
        default=15, gt=0, description="The snapshot cadence the series was sampled at."
    )
    annualisation: str = Field(
        default="sqrt_time",
        description="How volatility was annualised. Stated so it is never ambiguous.",
    )
    windows: tuple[WindowCoverage, ...] = ()


class ReturnsState(AtlasModel):
    """Fractional change in the reference price over several windows."""

    change_1h: float | None = None
    change_24h: float | None = None
    change_7d: float | None = None
    windows: tuple[WindowCoverage, ...] = ()


class TechnicalState(AtlasModel):
    """Technical indicators over the reference-price series.

    Deliberately a small part of Atlas. These are measurements of the input series,
    not recommendations, and Atlas does not emit buy or sell labels.
    """

    rsi_14: float | None = Field(default=None, ge=0, le=100)
    ema_12: float | None = Field(default=None, gt=0)
    ema_26: float | None = Field(default=None, gt=0)
    macd: float | None = None
    macd_signal: float | None = None
    bollinger_width_bps: float | None = Field(default=None, ge=0)
    observations_used: int = Field(default=0, ge=0)
    note: str = Field(
        default=(
            "Computed on Atlas snapshot-cadence reference prices, so values are not "
            "comparable to indicators computed on a venue's own candles."
        ),
    )


class FragmentationState(AtlasModel):
    """How fragmented an asset's market is across venues."""

    venue_count: int = Field(default=0, ge=0)
    effective_venue_count: float | None = Field(default=None, ge=0)
    volume_concentration_hhi: float | None = Field(default=None, ge=0, le=1)
    liquidity_concentration_hhi: float | None = Field(default=None, ge=0, le=1)
    largest_venue_volume_share: float | None = Field(default=None, ge=0, le=1)
    price_dispersion_bps: float | None = Field(default=None, ge=0)
    funding_dispersion_bps: float | None = Field(default=None, ge=0)
    spot_derivative_fragmentation: float | None = Field(default=None, ge=0)
    fragmentation_index: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Composite of venue breadth, volume concentration and price dispersion.",
    )


class PressureComponents(AtlasModel):
    """A vector of observable market-pressure components.

    Deliberately not collapsed into a score. Components absent for an asset are
    ``None``, and ``components_available`` says how many were computed, so that two
    assets are only compared on the same basis.
    """

    book_imbalance: float | None = Field(default=None, ge=-1, le=1)
    funding_deviation_z: float | None = None
    basis_deviation_z: float | None = None
    oi_change: float | None = None
    volume_acceleration: float | None = Field(default=None, ge=0)
    spot_flow_proxy: float | None = Field(
        default=None,
        description=(
            "Reserved for aggressive spot flow. Null in this release: Atlas does not "
            "collect a public trade tape, so there is no honest way to populate it."
        ),
    )
    derivative_flow_proxy: float | None = Field(
        default=None, description="Reserved for aggressive derivative flow. Null in this release."
    )
    components_available: int = Field(default=0, ge=0)
    baseline_window: str | None = Field(
        default=None, description="Window the standardised components were measured against."
    )
    baseline_coverage: WindowCoverage | None = None


class CrowdingComponents(AtlasModel):
    """A vector of observable positioning-crowding components.

    As with pressure, these are components rather than a ``Crowding: HIGH`` label.
    """

    funding_percentile_30d: float | None = Field(default=None, ge=0, le=100)
    oi_to_volume: float | None = Field(default=None, ge=0)
    perp_dominance: float | None = Field(default=None, ge=0, le=1)
    oi_change_24h: float | None = None
    basis_percentile_30d: float | None = Field(default=None, ge=0, le=100)
    components_available: int = Field(default=0, ge=0)
    baseline_coverage: WindowCoverage | None = None


class VenueAssetState(AtlasModel):
    """One venue's view of one asset.

    The row that makes venue decomposition visible: what this venue quoted, how far
    it sat from the reference price, what weight it received and whether it was
    included.
    """

    asset_id: str
    venue_slug: str
    observed_at: int
    price_usd: float | None = Field(default=None, gt=0)
    price_source: PriceSource | None = None
    deviation_bps: float | None = None
    reference_weight: float | None = Field(default=None, ge=0)
    spread_bps: float | None = Field(default=None, ge=0)
    reported_base_volume_24h: float | None = Field(default=None, ge=0)
    reported_quote_volume_24h: float | None = Field(default=None, ge=0)
    reported_volume_24h_usd: float | None = Field(default=None, ge=0)
    volume_share: float | None = Field(default=None, ge=0, le=1)
    liquidity_share: float | None = Field(default=None, ge=0, le=1)
    depth_50bps_usd: float | None = Field(default=None, ge=0)
    funding_rate_raw: float | None = None
    funding_interval_hours: float | None = Field(default=None, gt=0)
    funding_rate_8h: float | None = None
    mark_price_usd: float | None = Field(default=None, gt=0)
    index_price_usd: float | None = Field(default=None, gt=0)
    open_interest_raw: float | None = Field(default=None, ge=0)
    open_interest_usd: float | None = Field(default=None, ge=0)
    instrument_count: int = Field(default=0, ge=0)
    conversion: QuoteConversion | None = None
    included_in_reference_price: bool = False
    exclusion_reason: ExclusionReason | None = None


class MarketStructure(AtlasModel):
    """Which market segments exist for an asset and on how many venues."""

    spot_venue_count: int = Field(default=0, ge=0)
    perp_venue_count: int = Field(default=0, ge=0)
    futures_venue_count: int = Field(default=0, ge=0)
    option_venue_count: int = Field(default=0, ge=0)
    spot_instrument_count: int = Field(default=0, ge=0)
    perp_instrument_count: int = Field(default=0, ge=0)
    futures_instrument_count: int = Field(default=0, ge=0)
    option_instrument_count: int = Field(default=0, ge=0)
    quote_currencies: tuple[str, ...] = Field(
        default=(), description="Distinct quote asset IDs observed, sorted."
    )
    settlement_currencies: tuple[str, ...] = ()


class GenomeState(AtlasModel):
    """Market Genome lifecycle and, once available, its feature vector.

    Features stay ``None`` until the minimum history exists. Atlas reports
    ``warming_up`` rather than emitting a fingerprint from insufficient data.
    """

    status: GenomeStatus = GenomeStatus.INSUFFICIENT_HISTORY
    observations_available: int = Field(default=0, ge=0)
    observations_required: int = Field(default=0, ge=0)
    features: dict[str, float] = Field(
        default_factory=dict,
        description="Normalised behavioural features. Empty unless status is available.",
    )
    feature_version: str | None = None


class MarketState(AtlasModel):
    """One asset's observable global market state at one snapshot time.

    The object Atlas exists to produce. Sections are nested models; a section whose
    inputs were unavailable is present with null fields rather than absent, so that
    a consumer never has to guess whether a key is missing or the data was.
    """

    asset_id: str
    symbol: str
    name: str | None = None
    resolution_state: ResolutionState
    observed_at: int = Field(description="Snapshot effective time in epoch milliseconds.")
    coverage_tier: CollectionTier

    reference_price: ReferencePrice
    returns: ReturnsState = ReturnsState()
    dispersion: Dispersion = Dispersion()
    volume: VolumeState = VolumeState()
    volatility: VolatilityState = VolatilityState()
    liquidity: LiquidityState = LiquidityState()
    structure: MarketStructure = MarketStructure()
    funding: FundingState = FundingState()
    open_interest: OpenInterestState = OpenInterestState()
    basis: BasisState = BasisState()
    liquidations: LiquidationState = LiquidationState()
    fragmentation: FragmentationState = FragmentationState()
    technical: TechnicalState = TechnicalState()
    pressure: PressureComponents = PressureComponents()
    crowding: CrowdingComponents = CrowdingComponents()
    genome: GenomeState = GenomeState()
    quality: DataQuality

    @property
    def venue_count(self) -> int:
        """Distinct venues contributing a qualified observation."""
        return self.reference_price.venue_count

    @property
    def is_partial(self) -> bool:
        """Whether an expected venue failed to contribute to this state."""
        return self.quality.coverage.partial_data


class MarketOverview(AtlasModel):
    """Market-wide statistics over one named universe.

    Every figure names its universe, because breadth over all listings and breadth
    over liquid assets are different measurements.
    """

    universe: Universe
    observed_at: int
    generation_id: str
    asset_count: int = Field(default=0, ge=0)
    qualified_asset_count: int = Field(default=0, ge=0)
    instrument_count: int = Field(default=0, ge=0)
    venue_count: int = Field(default=0, ge=0)

    reported_spot_volume_24h_usd: float | None = Field(default=None, ge=0)
    reported_perp_volume_24h_usd: float | None = Field(default=None, ge=0)
    reported_futures_volume_24h_usd: float | None = Field(default=None, ge=0)
    open_interest_usd: float | None = Field(default=None, ge=0)

    breadth_advancing: float | None = Field(default=None, ge=0, le=1)
    breadth_advancing_sample: int = Field(default=0, ge=0)
    median_volatility_24h: float | None = Field(default=None, ge=0)
    volatility_sample: int = Field(default=0, ge=0)
    median_funding_8h: float | None = None
    funding_sample: int = Field(default=0, ge=0)
    median_venue_count: float | None = Field(default=None, ge=0)
    median_price_dispersion_bps: float | None = Field(default=None, ge=0)

    universe_criteria: str = Field(
        description="The deterministic rule that defined this universe, in words."
    )


class AssetStateBundle(AtlasModel):
    """A market state together with its venue decomposition and provenance.

    What the SDK hands back from ``atlas.asset("BTC")``: the state, the per-venue
    rows behind it, and the evidence for each derived value.
    """

    state: MarketState
    venues: tuple[VenueAssetState, ...] = ()
    provenance: tuple[Provenance, ...] = ()

    def provenance_for(self, metric: str) -> Provenance | None:
        """Return the evidence set for ``metric``, or ``None`` if not recorded."""
        return next((p for p in self.provenance if p.metric == metric), None)
