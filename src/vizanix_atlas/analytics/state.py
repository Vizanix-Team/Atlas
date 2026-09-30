"""Assembling the Universal Market State.

This module is where every layer meets. It takes validated observations for one asset and
produces the :class:`~vizanix_atlas.models.state.MarketState` that Atlas exists to publish,
along with the per-venue decomposition and the provenance behind each derived value.

The order of operations matters and is fixed:

1. Convert every instrument's quote currency to USD, or mark it unconvertible.
2. Build price candidates and compute the reference price, which everything else needs.
3. Value volume, but only where the venue published quote volume.
4. Normalise derivatives against the reference price.
5. Measure liquidity in bands around the reference price.
6. Derive structural metrics from the above.
7. Compute windowed metrics, subject to their history gates.
8. Attach coverage, freshness and quality.

Every stage records what it excluded. A published state can always answer "why is this
number null" and "which venues is it based on".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from vizanix_atlas.analytics.derivatives import (
    FundingObservation,
    MarkObservation,
    OpenInterestObservation,
    aggregate_funding,
    aggregate_open_interest,
    compute_basis,
    normalise_funding,
    normalise_open_interest,
    open_interest_to_volume,
)
from vizanix_atlas.analytics.liquidity import BookSample, aggregate_liquidity
from vizanix_atlas.analytics.reference_price import PriceCandidate, compute_reference_price
from vizanix_atlas.analytics.robust import median
from vizanix_atlas.analytics.series import (
    PriceSeries,
    compute_returns,
    compute_technical,
    compute_volatility,
)
from vizanix_atlas.analytics.structure import (
    CrowdingInputs,
    PressureInputs,
    compute_crowding,
    compute_fragmentation,
    compute_genome,
    compute_pressure,
    genome_features,
)
from vizanix_atlas.analytics.volume import (
    VolumeContribution,
    aggregate_volume,
    derivative_volume_usd,
    perp_dominance,
    value_volume,
    venue_volume_shares,
)
from vizanix_atlas.core.atlas_time import from_epoch_ms
from vizanix_atlas.core.config import CollectionConfig
from vizanix_atlas.core.numeric import relative_bps
from vizanix_atlas.core.versions import METHODOLOGY_VERSION, SCHEMA_VERSION
from vizanix_atlas.models.asset import CanonicalAsset
from vizanix_atlas.models.enums import (
    CollectionTier,
    ExclusionReason,
    InstrumentType,
    ObservationOrigin,
    PriceSource,
)
from vizanix_atlas.models.instrument import Instrument
from vizanix_atlas.models.observations import RawDerivativeObservation, RawOrderBook, RawTicker
from vizanix_atlas.models.quality import (
    Coverage,
    DataQuality,
    Freshness,
    Provenance,
    ProvenanceEntry,
    QuoteConversion,
)
from vizanix_atlas.models.state import (
    AssetStateBundle,
    LiquidationState,
    MarketState,
    MarketStructure,
    VenueAssetState,
)
from vizanix_atlas.normalization.conversion import ConversionGraph

#: Liquidation aggregation is not published in this release. No supported venue exposes a
#: public liquidation feed whose semantics Atlas could verify (every enabled venue declares
#: `liquidations: not_available`), so an aggregate would rest on nothing. Stated here and
#: in the published state rather than implied by absence.
_LIQUIDATION_NOTE: Final = (
    "No enabled venue exposes a public liquidation feed with verified semantics, so no "
    "aggregate is published. See docs/METHODOLOGY.md."
)


@dataclass(slots=True)
class AssetObservations:
    """Everything collected for one asset in one generation.

    Assembled by the pipeline before analytics runs, so that the state builder is a pure
    function of its inputs and therefore reproducible from a frozen release.
    """

    asset: CanonicalAsset
    #: Validated ``(ticker, instrument)`` pairs.
    tickers: list[tuple[RawTicker, Instrument]] = field(default_factory=list)
    #: Validated ``(observation, instrument)`` pairs.
    derivatives: list[tuple[RawDerivativeObservation, Instrument]] = field(default_factory=list)
    #: Validated ``(book, instrument)`` pairs.
    order_books: list[tuple[RawOrderBook, Instrument]] = field(default_factory=list)
    #: Observations rejected before analytics, with their reasons.
    pre_excluded: list[tuple[str, Instrument, ExclusionReason]] = field(default_factory=list)
    #: Venues whose catalogue lists this asset, whether or not they reported.
    listing_venues: set[str] = field(default_factory=set)
    #: Venues expected to report this generation, for the coverage ratio.
    expected_venues: set[str] = field(default_factory=set)
    #: Venue symbols that plausibly matched this asset but were not merged.
    ambiguous_identity_count: int = 0
    #: This asset's reference-price history, for the windowed metrics.
    history: PriceSeries = field(default_factory=lambda: PriceSeries(points=()))
    #: Historical series for the standardised pressure and crowding components.
    funding_history: tuple[float, ...] = ()
    basis_history: tuple[float, ...] = ()
    open_interest_previous_usd: float | None = None
    open_interest_24h_ago_usd: float | None = None
    volume_baseline_usd: float | None = None


def build_market_state(
    observations: AssetObservations,
    *,
    conversion: ConversionGraph,
    config: CollectionConfig,
    generation_id: str,
    snapshot_effective_time: int,
    observed_through: int | None = None,
    coverage_tier: CollectionTier = CollectionTier.A_UNIVERSAL,
    origin: ObservationOrigin = ObservationOrigin.SCHEDULED_SNAPSHOT,
) -> AssetStateBundle:
    """Build one asset's market state, venue decomposition and provenance.

    Args:
        observations: Everything collected for this asset.
        conversion: The observed quote-conversion graph.
        config: Collection policy.
        generation_id: Immutable generation identifier.
        snapshot_effective_time: The scheduled slot this state is attributed to. Used for
            partitioning and for the published ``observed_at``.
        observed_through: When collection finished. Ages are measured against this rather
            than against the slot, because the slot is floored to its window start and an
            observation arriving after it would otherwise show a negative age. Defaults to
            the slot when not supplied.
        coverage_tier: The deepest tier reached for this asset.
        origin: How the observations were obtained.

    """
    age_reference = observed_through or snapshot_effective_time
    conversions = _conversions_for(observations, conversion)

    candidates, pre_excluded = _price_candidates(observations, conversions, age_reference)
    price_result = compute_reference_price(
        candidates,
        quality=config.quality,
        config=config.reference_price,
        methodology_version=METHODOLOGY_VERSION,
        excluded_before=pre_excluded,
    )
    reference_price = price_result.value

    volume_contributions = _volume_contributions(observations, conversions)
    volume = aggregate_volume(volume_contributions)
    volume_shares = venue_volume_shares(volume_contributions)

    funding_observations, funding_excluded = _funding(observations)
    funding = aggregate_funding(funding_observations, funding_excluded)

    open_interest_observations = _open_interest(
        observations, conversions, reference_price=reference_price
    )
    open_interest = aggregate_open_interest(open_interest_observations)

    marks = _marks(observations, conversions)
    basis = compute_basis(marks, reference_price=reference_price)

    liquidity = aggregate_liquidity(
        _book_samples(observations, conversions),
        reference_price=reference_price,
        bands_bps=config.tiers.b_liquidity.depth_bands_bps,
        impact_notionals_usd=config.tiers.b_liquidity.impact_notionals_usd,
    )

    structure = _structure(observations)
    fragmentation = compute_fragmentation(
        venue_count=price_result.reference_price.venue_count,
        volume_shares=volume_shares,
        liquidity=liquidity,
        price_dispersion_bps=price_result.dispersion.price_dispersion_bps,
        funding_dispersion_bps=funding.dispersion_bps,
        spot_median_usd=_median_price(candidates, spot=True),
        derivative_median_usd=_median_price(candidates, spot=False),
        reference_price=reference_price,
    )

    volatility = compute_volatility(observations.history, config.history)
    returns = compute_returns(observations.history, config.history)
    technical = compute_technical(observations.history, config.history)

    derivative_volume = derivative_volume_usd(volume)
    oi_to_volume = open_interest_to_volume(open_interest.total_usd, derivative_volume)

    pressure = compute_pressure(
        PressureInputs(
            book_imbalance_50bps=liquidity.book_imbalance_50bps,
            funding_8h_current=funding.rate_8h_median,
            funding_8h_history=observations.funding_history,
            basis_bps_current=basis.perp_basis_bps_median,
            basis_bps_history=observations.basis_history,
            open_interest_usd_current=open_interest.total_usd,
            open_interest_usd_previous=observations.open_interest_previous_usd,
            volume_usd_current=volume.reported_volume_24h_usd,
            volume_usd_baseline=observations.volume_baseline_usd,
            baseline_window="24h",
            baseline_coverage=observations.history.coverage(
                "24h", required=config.history.required_for("realised_volatility_24h")
            ),
        )
    )
    crowding = compute_crowding(
        CrowdingInputs(
            funding_8h_current=funding.rate_8h_median,
            funding_8h_history_30d=observations.funding_history,
            funding_coverage=observations.history.coverage(
                "30d", required=config.history.required_for("funding_percentile_30d")
            ),
            basis_bps_current=basis.perp_basis_bps_median,
            basis_bps_history_30d=observations.basis_history,
            oi_to_volume=oi_to_volume,
            perp_dominance=perp_dominance(volume),
            open_interest_usd_current=open_interest.total_usd,
            open_interest_usd_24h_ago=observations.open_interest_24h_ago_usd,
        )
    )

    genome = compute_genome(
        observations_available=len(observations.history.points),
        config=config.history.genome,
        features=genome_features(
            volatility_24h=volatility.realised_24h,
            depth_50bps_usd=(
                band.total_depth_usd if (band := liquidity.band(50)) is not None else None
            ),
            venue_count=price_result.reference_price.venue_count,
            effective_venue_count=fragmentation.effective_venue_count,
            perp_dominance=perp_dominance(volume),
            funding_8h_median=funding.rate_8h_median,
            price_dispersion_bps=price_result.dispersion.price_dispersion_bps,
            oi_to_volume=oi_to_volume,
        ),
    )

    quality = _quality(
        observations,
        age_reference=age_reference,
        price_result=price_result,
        funding_venue_count=funding.venue_count,
        open_interest_venue_count=open_interest.venue_count,
        liquidity_venue_count=liquidity.venue_count,
        generation_id=generation_id,
        snapshot_effective_time=snapshot_effective_time,
        origin=origin,
    )

    state = MarketState(
        asset_id=observations.asset.asset_id,
        symbol=observations.asset.symbol,
        name=observations.asset.name,
        resolution_state=observations.asset.resolution_state,
        observed_at=snapshot_effective_time,
        coverage_tier=coverage_tier,
        reference_price=price_result.reference_price.model_copy(
            update={"provenance_ref": f"{observations.asset.asset_id}#reference_price"}
        ),
        returns=returns,
        dispersion=price_result.dispersion,
        volume=volume,
        volatility=volatility,
        liquidity=liquidity,
        structure=structure,
        funding=funding.model_copy(
            update={"provenance_ref": f"{observations.asset.asset_id}#funding"}
        ),
        open_interest=open_interest.model_copy(
            update={"provenance_ref": f"{observations.asset.asset_id}#open_interest"}
        ),
        basis=basis,
        liquidations=LiquidationState(
            aggregate_available=False,
            venue_count=0,
            note=_LIQUIDATION_NOTE,
        ),
        fragmentation=fragmentation,
        technical=technical,
        pressure=pressure,
        crowding=crowding,
        genome=genome,
        quality=quality,
    )

    venues = _venue_states(
        observations,
        conversions,
        price_result=price_result,
        volume_shares=volume_shares,
        liquidity=liquidity,
        funding_observations=funding_observations,
        open_interest_observations=open_interest_observations,
        marks=marks,
        reference_price=reference_price,
        snapshot_effective_time=snapshot_effective_time,
    )

    return AssetStateBundle(
        state=state,
        venues=tuple(sorted(venues, key=lambda v: v.venue_slug)),
        provenance=(price_result.provenance,),
    )


# --------------------------------------------------------------------------- helpers


def _conversions_for(
    observations: AssetObservations, conversion: ConversionGraph
) -> dict[str, QuoteConversion | None]:
    """Resolve a USD conversion for every quote currency in play.

    Cached per asset because one asset commonly has a dozen instruments sharing three
    quote currencies.
    """
    quote_asset_ids = {
        instrument.quote_asset_id
        for source in (
            observations.tickers,
            observations.derivatives,
            observations.order_books,
        )
        for _, instrument in source
    }
    return {asset_id: conversion.to_usd(asset_id) for asset_id in sorted(quote_asset_ids)}


def _rate(conversions: dict[str, QuoteConversion | None], instrument: Instrument) -> float | None:
    """Return the usable USD rate for an instrument's quote currency, or ``None``."""
    entry = conversions.get(instrument.quote_asset_id)
    return entry.rate if entry is not None and entry.usable else None


def _price_candidates(
    observations: AssetObservations,
    conversions: dict[str, QuoteConversion | None],
    snapshot_effective_time: int,
) -> tuple[list[PriceCandidate], list[tuple[PriceCandidate, ExclusionReason]]]:
    """Build price candidates, separating those that cannot be converted to USD."""
    candidates: list[PriceCandidate] = []
    excluded: list[tuple[PriceCandidate, ExclusionReason]] = []

    weighting_volume = {
        instrument.instrument_id: _weighting_volume_usd(ticker, instrument, conversions)
        for ticker, instrument in observations.tickers
    }

    for ticker, instrument in observations.tickers:
        if instrument.instrument_type is InstrumentType.OPTION:
            # An option's price is a premium, not the underlying's price. Options are
            # therefore not price sources at all, rather than being sources that get
            # excluded: recording 1,200 BTC option strikes as rejected price candidates
            # would bury the provenance of the thirteen venues that actually set the price.
            # Option activity still reaches the state through structure counts and
            # reported_option_volume_24h_usd.
            continue
        pick = ticker.reference_candidate
        if pick is None:
            continue
        raw_price, source = pick
        venue_time = ticker.timing.exchange_event_time or ticker.timing.exchange_server_time
        age = (
            (from_epoch_ms(snapshot_effective_time) - from_epoch_ms(venue_time)).total_seconds()
            if venue_time is not None
            else None
        )
        rate = _rate(conversions, instrument)
        candidate = PriceCandidate(
            venue_slug=ticker.venue_slug,
            instrument=instrument,
            # Price is set below; a placeholder here keeps the dataclass frozen.
            price_usd=raw_price * rate if rate is not None else raw_price,
            price_source=source,
            observed_at=venue_time,
            age_seconds=age,
            raw_price=raw_price,
            reported_volume_usd=weighting_volume.get(instrument.instrument_id),
            conversion=conversions.get(instrument.quote_asset_id),
        )
        if rate is None:
            excluded.append((candidate, ExclusionReason.INVALID_QUOTE_CONVERSION))
        else:
            candidates.append(candidate)

    for venue_slug, instrument, reason in observations.pre_excluded:
        excluded.append(
            (
                PriceCandidate(
                    venue_slug=venue_slug,
                    instrument=instrument,
                    price_usd=0.0,
                    price_source=PriceSource.LAST,
                    observed_at=None,
                    age_seconds=None,
                    raw_price=0.0,
                    reported_volume_usd=None,
                    conversion=None,
                ),
                reason,
            )
        )
    return candidates, excluded


def _usd_volume(
    ticker: RawTicker,
    instrument: Instrument,
    conversions: dict[str, QuoteConversion | None],
) -> float | None:
    """Return an instrument's reported quote volume in USD, if the venue published it.

    This is the figure Atlas **publishes**. It uses only venue-reported quote volume;
    nothing is derived. See :func:`_weighting_volume_usd` for the separate, internal
    quantity used to weight the reference price.
    """
    rate = _rate(conversions, instrument)
    if rate is None or ticker.quote_volume_24h is None:
        return None
    return ticker.quote_volume_24h * rate


def _weighting_volume_usd(
    ticker: RawTicker,
    instrument: Instrument,
    conversions: dict[str, QuoteConversion | None],
) -> float | None:
    """Return a USD volume estimate used **only** to weight the reference price.

    Distinct from :func:`_usd_volume`, and the distinction matters. Coinbase, Kraken,
    Bitstamp and Bitfinex publish base volume but no quote volume. Those are among the
    most useful venues Atlas has, because they quote against actual dollars rather than a
    stablecoin. Weighting them by their absent quote volume gave them the minimum weight
    and effectively removed the USD-native venues from the estimator.

    So for weighting, and only for weighting, base volume is valued at that venue's own
    observed price. No figure derived this way is ever published as a volume: the published
    totals still count only what a venue actually reported, which is why
    ``venues_reporting_quote_volume`` exists. The two quantities are kept in separate
    functions so the boundary cannot be blurred by accident.
    """
    rate = _rate(conversions, instrument)
    if rate is None:
        return None
    if ticker.quote_volume_24h is not None:
        return ticker.quote_volume_24h * rate
    pick = ticker.reference_candidate
    if ticker.base_volume_24h is None or pick is None:
        return None
    return ticker.base_volume_24h * pick[0] * rate


def _volume_contributions(
    observations: AssetObservations, conversions: dict[str, QuoteConversion | None]
) -> list[VolumeContribution]:
    """Build one volume contribution per instrument."""
    return [
        value_volume(
            venue_slug=ticker.venue_slug,
            instrument=instrument,
            base_volume=ticker.base_volume_24h,
            quote_volume=ticker.quote_volume_24h,
            quote_to_usd=_rate(conversions, instrument),
        )
        for ticker, instrument in observations.tickers
    ]


def _funding(observations: AssetObservations) -> tuple[list[FundingObservation], int]:
    """Normalise every funding observation, counting those that could not be."""
    marks = {
        instrument.instrument_id: observation.mark_price
        for observation, instrument in observations.derivatives
        if observation.mark_price is not None
    }
    normalised: list[FundingObservation] = []
    excluded = 0
    for observation, instrument in observations.derivatives:
        if observation.funding_rate_raw is None:
            continue
        result = normalise_funding(
            observation, instrument, mark_price=marks.get(instrument.instrument_id)
        )
        if isinstance(result, ExclusionReason):
            excluded += 1
        else:
            normalised.append(result)
    return normalised, excluded


def _open_interest(
    observations: AssetObservations,
    conversions: dict[str, QuoteConversion | None],
    *,
    reference_price: float | None,
) -> list[OpenInterestObservation]:
    """Normalise every open-interest observation."""
    results: list[OpenInterestObservation] = []
    for observation, instrument in observations.derivatives:
        converted = normalise_open_interest(
            observation,
            instrument,
            reference_price=reference_price,
            quote_to_usd=_rate(conversions, instrument),
        )
        if converted is not None:
            results.append(converted)
    return results


def _marks(
    observations: AssetObservations, conversions: dict[str, QuoteConversion | None]
) -> list[MarkObservation]:
    """Build USD-valued mark observations for basis.

    An instrument whose quote currency cannot be converted is omitted, because a basis in
    an unconverted currency is not comparable to one in USD.
    """
    marks: list[MarkObservation] = []
    for observation, instrument in observations.derivatives:
        if observation.mark_price is None:
            continue
        rate = _rate(conversions, instrument)
        if rate is None:
            continue
        marks.append(
            MarkObservation(
                venue_slug=observation.venue_slug,
                instrument=instrument,
                mark_price_usd=observation.mark_price * rate,
            )
        )
    return marks


def _book_samples(
    observations: AssetObservations, conversions: dict[str, QuoteConversion | None]
) -> list[BookSample]:
    """Build book samples for liquidity measurement."""
    return [
        BookSample(
            venue_slug=book.venue_slug,
            instrument=instrument,
            book=book,
            quote_to_usd=_rate(conversions, instrument),
            observed_at=book.timing.exchange_event_time or book.timing.exchange_server_time,
        )
        for book, instrument in observations.order_books
    ]


def _structure(observations: AssetObservations) -> MarketStructure:
    """Count which market segments exist and on how many venues."""

    def venues(kind: InstrumentType) -> set[str]:
        return {
            instrument.venue_slug
            for _, instrument in observations.tickers
            if instrument.instrument_type is kind
        }

    def instruments(kind: InstrumentType) -> int:
        return sum(
            1 for _, instrument in observations.tickers if instrument.instrument_type is kind
        )

    all_instruments = [instrument for _, instrument in observations.tickers]
    return MarketStructure(
        spot_venue_count=len(venues(InstrumentType.SPOT)),
        perp_venue_count=len(venues(InstrumentType.PERPETUAL)),
        futures_venue_count=len(venues(InstrumentType.FUTURE)),
        option_venue_count=len(venues(InstrumentType.OPTION)),
        spot_instrument_count=instruments(InstrumentType.SPOT),
        perp_instrument_count=instruments(InstrumentType.PERPETUAL),
        futures_instrument_count=instruments(InstrumentType.FUTURE),
        option_instrument_count=instruments(InstrumentType.OPTION),
        quote_currencies=tuple(sorted({i.quote_asset_id for i in all_instruments})),
        settlement_currencies=tuple(
            sorted({i.settlement_asset_id for i in all_instruments if i.settlement_asset_id})
        ),
    )


def _median_price(candidates: Sequence[PriceCandidate], *, spot: bool) -> float | None:
    """Return the median USD price among spot or derivative candidates."""
    prices = [c.price_usd for c in candidates if c.is_spot is spot]
    return median(prices)


def _quality(
    observations: AssetObservations,
    *,
    age_reference: int,
    price_result: object,
    funding_venue_count: int,
    open_interest_venue_count: int,
    liquidity_venue_count: int,
    generation_id: str,
    snapshot_effective_time: int,
    origin: ObservationOrigin,
) -> DataQuality:
    """Assemble the quality record, including per-family freshness."""
    reference = price_result.reference_price  # type: ignore[attr-defined]
    ages = _ages(observations, age_reference)

    coverage = Coverage(
        price_venue_count=reference.venue_count,
        liquidity_venue_count=liquidity_venue_count,
        funding_venue_count=funding_venue_count,
        open_interest_venue_count=open_interest_venue_count,
        listing_venue_count=len(observations.listing_venues),
        expected_venue_count=len(observations.expected_venues),
        partial_data=reference.venue_count < len(observations.expected_venues),
        ambiguous_identity_count=observations.ambiguous_identity_count,
    )
    freshness = Freshness(
        snapshot_effective_time=snapshot_effective_time,
        freshest_observation_age_seconds=min(ages["all"]) if ages["all"] else None,
        oldest_observation_age_seconds=max(ages["all"]) if ages["all"] else None,
        ticker_age_seconds=min(ages["ticker"]) if ages["ticker"] else None,
        funding_age_seconds=min(ages["funding"]) if ages["funding"] else None,
        open_interest_age_seconds=min(ages["open_interest"]) if ages["open_interest"] else None,
        order_book_age_seconds=min(ages["order_book"]) if ages["order_book"] else None,
    )
    return DataQuality(
        coverage=coverage,
        freshness=freshness,
        generation_id=generation_id,
        schema_version=SCHEMA_VERSION,
        methodology_version=METHODOLOGY_VERSION,
        origin=origin,
        excluded_observation_count=reference.excluded_venue_count,
        exclusion_reasons=dict(reference.excluded_reasons),
    )


def _ages(observations: AssetObservations, snapshot_effective_time: int) -> dict[str, list[float]]:
    """Collect observation ages per family.

    Only venue-supplied timestamps contribute. A venue publishing no timestamp is absent
    from these lists rather than counted as zero seconds old, which would report a
    timestampless venue as perfectly fresh.
    """
    ages: dict[str, list[float]] = {
        "all": [],
        "ticker": [],
        "funding": [],
        "open_interest": [],
        "order_book": [],
    }
    reference = from_epoch_ms(snapshot_effective_time)

    def record(family: str, moment: int | None) -> None:
        if moment is None:
            return
        age = (reference - from_epoch_ms(moment)).total_seconds()
        ages[family].append(age)
        ages["all"].append(age)

    for ticker, _ in observations.tickers:
        record("ticker", ticker.timing.exchange_event_time or ticker.timing.exchange_server_time)
    for observation, _ in observations.derivatives:
        moment = observation.timing.exchange_event_time or observation.timing.exchange_server_time
        if observation.funding_rate_raw is not None:
            record("funding", moment)
        if observation.open_interest_raw is not None:
            record("open_interest", moment)
    for book, _ in observations.order_books:
        record("order_book", book.timing.exchange_event_time or book.timing.exchange_server_time)
    return ages


def _representative_ticker(
    venue_tickers: Sequence[tuple[RawTicker, Instrument]],
    by_instrument: dict[str, ProvenanceEntry],
    conversions: dict[str, QuoteConversion | None],
) -> tuple[RawTicker, Instrument]:
    """Pick the one ticker that summarises a venue in the per-venue decomposition.

    Preference, in order: a price source that actually entered the reference price;
    a non-option instrument (an option's price is a premium, never the underlying's
    price); spot over derivatives; an instrument whose quote converts to USD; the heaviest
    reference weight; the largest weighting volume; and finally the instrument id, so the
    choice is stable across runs.
    """

    def key(item: tuple[RawTicker, Instrument]) -> tuple[object, ...]:
        ticker, instrument = item
        entry = by_instrument.get(instrument.instrument_id)
        return (
            not (entry is not None and entry.included),
            instrument.instrument_type is InstrumentType.OPTION,
            instrument.instrument_type is not InstrumentType.SPOT,
            _rate(conversions, instrument) is None,
            -(entry.weight if entry is not None and entry.weight is not None else 0.0),
            -(_weighting_volume_usd(ticker, instrument, conversions) or 0.0),
            instrument.instrument_id,
        )

    return min(venue_tickers, key=key)


def _venue_states(
    observations: AssetObservations,
    conversions: dict[str, QuoteConversion | None],
    *,
    price_result: object,
    volume_shares: dict[str, float],
    liquidity: object,
    funding_observations: Sequence[FundingObservation],
    open_interest_observations: Sequence[OpenInterestObservation],
    marks: Sequence[MarkObservation],
    reference_price: float | None,
    snapshot_effective_time: int,
) -> list[VenueAssetState]:
    """Build the per-venue decomposition of an asset's state.

    This is what makes a reference price auditable from the published dataset alone: each
    venue's price, weight, inclusion and exclusion reason appear as a row.
    """
    provenance: Provenance = price_result.provenance  # type: ignore[attr-defined]
    by_instrument = {entry.instrument_id: entry for entry in provenance.entries}
    liquidity_shares = {
        entry.venue_slug: entry.depth_share
        for entry in liquidity.by_venue  # type: ignore[attr-defined]
    }
    depth_by_venue: dict[str, float] = {}
    for entry in liquidity.by_venue:  # type: ignore[attr-defined]
        band = next((b for b in entry.bands if b.distance_bps == 50), None)
        if band is not None and band.total_depth_usd is not None:
            depth_by_venue[entry.venue_slug] = (
                depth_by_venue.get(entry.venue_slug, 0.0) + band.total_depth_usd
            )

    funding_by_venue = {o.venue_slug: o for o in funding_observations}
    oi_by_venue: dict[str, float] = {}
    oi_raw_by_venue: dict[str, float] = {}
    for observation in open_interest_observations:
        if observation.usd_equivalent is not None:
            oi_by_venue[observation.venue_slug] = (
                oi_by_venue.get(observation.venue_slug, 0.0) + observation.usd_equivalent
            )
        oi_raw_by_venue[observation.venue_slug] = (
            oi_raw_by_venue.get(observation.venue_slug, 0.0) + observation.raw
        )
    mark_by_venue = {m.venue_slug: m.mark_price_usd for m in marks}

    states: list[VenueAssetState] = []

    tickers_by_venue: dict[str, list[tuple[RawTicker, Instrument]]] = {}
    for ticker, instrument in observations.tickers:
        tickers_by_venue.setdefault(ticker.venue_slug, []).append((ticker, instrument))

    for venue in sorted(tickers_by_venue):
        # One row per venue, summarising the instrument that best represents it. The
        # choice must be deterministic and must not depend on the order a venue happened
        # to list its instruments in, or the row would show (for example) a EUR pair that
        # cannot be converted while the same venue's USD pair sits in the reference price.
        ticker, instrument = _representative_ticker(
            tickers_by_venue[venue], by_instrument, conversions
        )
        entry = by_instrument.get(instrument.instrument_id)
        rate = _rate(conversions, instrument)
        is_option = instrument.instrument_type is InstrumentType.OPTION
        pick = None if is_option else ticker.reference_candidate
        price_usd = pick[0] * rate if pick is not None and rate is not None else None
        funding = funding_by_venue.get(venue)

        states.append(
            VenueAssetState(
                asset_id=observations.asset.asset_id,
                venue_slug=venue,
                observed_at=ticker.timing.best_effective_time,
                price_usd=price_usd,
                price_source=pick[1] if pick is not None else None,
                deviation_bps=relative_bps(price_usd, reference_price),
                reference_weight=entry.weight if entry is not None else None,
                spread_bps=ticker.spread_bps,
                reported_base_volume_24h=ticker.base_volume_24h,
                reported_quote_volume_24h=ticker.quote_volume_24h,
                reported_volume_24h_usd=_usd_volume(ticker, instrument, conversions),
                volume_share=volume_shares.get(venue),
                liquidity_share=liquidity_shares.get(venue),
                depth_50bps_usd=depth_by_venue.get(venue),
                funding_rate_raw=funding.rate_raw if funding else None,
                funding_interval_hours=funding.interval_hours if funding else None,
                funding_rate_8h=funding.rate_8h if funding else None,
                mark_price_usd=mark_by_venue.get(venue),
                open_interest_raw=oi_raw_by_venue.get(venue),
                open_interest_usd=oi_by_venue.get(venue),
                instrument_count=sum(1 for _, i in observations.tickers if i.venue_slug == venue),
                conversion=conversions.get(instrument.quote_asset_id),
                included_in_reference_price=entry.included if entry is not None else False,
                exclusion_reason=entry.exclusion_reason if entry is not None else None,
            )
        )
    return states
