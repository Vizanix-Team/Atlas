"""Market state assembly tests.

An end-to-end check that a state built from realistic observations is internally
consistent, that nulls mean what they say, and that the reference price stays traceable
back to venue rows.
"""

from __future__ import annotations

import pytest

from vizanix_atlas.analytics.state import AssetObservations, build_market_state
from vizanix_atlas.core.config import load_collection_config
from vizanix_atlas.identity.resolver import build_resolver
from vizanix_atlas.models.asset import CanonicalAsset
from vizanix_atlas.models.enums import (
    CollectionTier,
    ContractType,
    ExclusionReason,
    FundingSemantics,
    GenomeStatus,
    InstrumentType,
    OpenInterestUnit,
    ReferencePriceMethod,
    ResolutionState,
)
from vizanix_atlas.models.instrument import Instrument
from vizanix_atlas.models.observations import (
    ObservationTiming,
    OrderBookLevel,
    RawDerivativeObservation,
    RawFxObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)
from vizanix_atlas.models.enums import PriceSource
from vizanix_atlas.normalization.conversion import build_conversion_graph

SNAPSHOT = 1790530200000
OBSERVED = SNAPSHOT - 30_000  # 30 seconds before the snapshot

BTC = CanonicalAsset(
    asset_id="asset:native:bitcoin:BTC",
    symbol="BTC",
    name="Bitcoin",
    chain_slug="bitcoin",
    resolution_state=ResolutionState.RESOLVED,
)
USDT_ID = "asset:evm:1:0xdac17f958d2ee523a2206206994597c13d831ec7"


def timing(moment: int = OBSERVED) -> ObservationTiming:
    return ObservationTiming(collector_receive_time=SNAPSHOT, exchange_event_time=moment)


def spot(venue: str, quote_asset_id: str) -> Instrument:
    return Instrument(
        instrument_id=f"instrument:{venue}:spot:BTC",
        venue_slug=venue,
        instrument_type=InstrumentType.SPOT,
        instrument_class="spot",
        symbol_native="BTCUSDT",
        symbol_normalized="BTC/USDT",
        base_asset_id=BTC.asset_id,
        quote_asset_id=quote_asset_id,
    )


def perp(venue: str, quote_asset_id: str) -> Instrument:
    return Instrument(
        instrument_id=f"instrument:{venue}:linear-perp:BTC",
        venue_slug=venue,
        instrument_type=InstrumentType.PERPETUAL,
        instrument_class="linear-perp",
        symbol_native="BTCUSDT",
        symbol_normalized="BTC/USDT",
        base_asset_id=BTC.asset_id,
        quote_asset_id=quote_asset_id,
        settlement_asset_id=quote_asset_id,
        contract_type=ContractType.LINEAR,
        contract_multiplier=0.01,
        contract_value_asset_id=BTC.asset_id,
        funding_interval_hours=8.0,
        funding_semantics=FundingSemantics.RELATIVE_PER_INTERVAL,
        open_interest_unit=OpenInterestUnit.CONTRACTS,
    )


def ticker(venue: str, price: float, *, quote_volume: float | None = 1e9) -> RawTicker:
    return RawTicker(
        venue_slug=venue,
        symbol_native="BTCUSDT",
        timing=timing(),
        last_price=price,
        bid_price=price - 0.5,
        ask_price=price + 0.5,
        base_volume_24h=(quote_volume / price) if quote_volume else None,
        quote_volume_24h=quote_volume,
    )


def book(venue: str, mid: float) -> RawOrderBook:
    return RawOrderBook(
        venue_slug=venue,
        symbol_native="BTCUSDT",
        timing=timing(),
        bids=tuple(
            OrderBookLevel(price=mid - i, size=1.0) for i in range(1, 6)
        ),
        asks=tuple(
            OrderBookLevel(price=mid + i, size=1.0) for i in range(1, 6)
        ),
    )


@pytest.fixture
def config():
    return load_collection_config()


@pytest.fixture
def conversion():
    """A graph in which USDT converts to USD at an observed, slightly-off-parity rate."""
    raws = [
        RawInstrument(
            venue_slug="coinbase", symbol_native="USDTUSD", instrument_class="spot",
            instrument_type="spot", base_symbol_native="USDT", quote_symbol_native="USD",
        ),
        RawInstrument(
            venue_slug="okx", symbol_native="BTCUSDT", instrument_class="spot",
            instrument_type="spot", base_symbol_native="BTC", quote_symbol_native="USDT",
        ),
    ]
    resolver = build_resolver(raws)
    return build_conversion_graph(
        [
            RawFxObservation(
                venue_slug="coinbase", symbol_native="USDT-USD", timing=timing(),
                from_symbol_native="USDT", to_symbol_native="USD", rate=0.9997,
                source_price=PriceSource.MID,
            )
        ],
        resolver,
        load_collection_config().quality,
    )


def test_state_is_internally_consistent(config, conversion) -> None:
    observations = AssetObservations(
        asset=BTC,
        tickers=[
            (ticker("okx", 84400.0), spot("okx", USDT_ID)),
            (ticker("bitget", 84410.0), spot("bitget", USDT_ID)),
            (ticker("gateio", 84405.0), spot("gateio", USDT_ID)),
            (ticker("mexc", 84395.0), spot("mexc", USDT_ID)),
        ],
        listing_venues={"okx", "bitget", "gateio", "mexc"},
        expected_venues={"okx", "bitget", "gateio", "mexc"},
    )
    bundle = build_market_state(
        observations,
        conversion=conversion,
        config=config,
        generation_id="20260927T173000Z-test",
        snapshot_effective_time=SNAPSHOT,
    )
    state = bundle.state

    assert state.asset_id == BTC.asset_id
    assert state.reference_price.available
    assert state.reference_price.method is ReferencePriceMethod.SPOT_WEIGHTED_MEDIAN
    assert state.venue_count == 4
    # Prices are USD-normalised, so they sit slightly below the USDT-quoted figures.
    assert state.reference_price.value is not None
    assert 84_300 < state.reference_price.value < 84_410

    # The invariant: everything considered is accounted for.
    considered = (
        state.reference_price.included_venue_count + state.reference_price.excluded_venue_count
    )
    assert considered == len(observations.tickers)

    # Coverage is complete and consistent.
    assert state.quality.coverage.coverage_ratio == pytest.approx(1.0)
    assert not state.quality.coverage.partial_data
    assert 0.0 <= state.quality.coverage.coverage_ratio <= 1.0

    # Freshness is measured from venue timestamps.
    assert state.quality.freshness.freshest_observation_age_seconds == pytest.approx(30.0)
    assert state.quality.freshness.ticker_age_seconds == pytest.approx(30.0)
    # No funding or books were collected, so those ages are null rather than zero.
    assert state.quality.freshness.funding_age_seconds is None
    assert state.quality.freshness.order_book_age_seconds is None

    # Every USD figure carries its conversion provenance.
    venue = bundle.venues[0]
    assert venue.conversion is not None
    assert venue.conversion.usable
    assert venue.conversion.rate == pytest.approx(0.9997)


def test_missing_families_are_null_not_zero(config, conversion) -> None:
    """An asset with no derivatives must not report zero funding or zero open interest."""
    observations = AssetObservations(
        asset=BTC,
        tickers=[(ticker("okx", 84400.0), spot("okx", USDT_ID))],
        listing_venues={"okx"},
        expected_venues={"okx"},
    )
    state = build_market_state(
        observations,
        conversion=conversion,
        config=config,
        generation_id="g",
        snapshot_effective_time=SNAPSHOT,
    ).state

    assert state.funding.rate_8h_median is None
    assert state.funding.venue_count == 0
    assert state.open_interest.total_usd is None
    assert state.basis.perp_basis_bps_median is None
    assert state.liquidity.venue_count == 0
    assert state.liquidity.bands == ()
    # A single venue has no dispersion, which is null rather than zero.
    assert state.dispersion.price_dispersion_bps is None
    assert state.reference_price.method is ReferencePriceMethod.SINGLE_VENUE_SPOT


def test_unconvertible_quote_currency_excludes_the_venue(config) -> None:
    """With no observed USDT rate, a USDT-quoted venue contributes no USD price."""
    from vizanix_atlas.normalization.conversion import ConversionGraph

    empty_graph = ConversionGraph(quality=config.quality)
    observations = AssetObservations(
        asset=BTC,
        tickers=[(ticker("okx", 84400.0), spot("okx", USDT_ID))],
        listing_venues={"okx"},
        expected_venues={"okx"},
    )
    state = build_market_state(
        observations,
        conversion=empty_graph,
        config=config,
        generation_id="g",
        snapshot_effective_time=SNAPSHOT,
    ).state

    assert state.reference_price.value is None
    assert state.reference_price.method is ReferencePriceMethod.UNAVAILABLE
    assert (
        state.reference_price.excluded_reasons.get(
            ExclusionReason.INVALID_QUOTE_CONVERSION.value
        )
        == 1
    )


def test_derivatives_contribute_funding_open_interest_and_basis(config, conversion) -> None:
    perp_instrument = perp("bitget", USDT_ID)
    observations = AssetObservations(
        asset=BTC,
        tickers=[
            (ticker("okx", 84400.0), spot("okx", USDT_ID)),
            (ticker("bitget", 84405.0), perp_instrument),
        ],
        derivatives=[
            (
                RawDerivativeObservation(
                    venue_slug="bitget",
                    symbol_native="BTCUSDT",
                    timing=timing(),
                    mark_price=84450.0,
                    funding_rate_raw=0.000037,
                    funding_interval_hours=8.0,
                    open_interest_raw=3_135_780.0,
                    open_interest_unit=OpenInterestUnit.CONTRACTS,
                ),
                perp_instrument,
            )
        ],
        listing_venues={"okx", "bitget"},
        expected_venues={"okx", "bitget"},
    )
    state = build_market_state(
        observations,
        conversion=conversion,
        config=config,
        generation_id="g",
        snapshot_effective_time=SNAPSHOT,
    ).state

    assert state.funding.venue_count == 1
    assert state.funding.rate_8h_median == pytest.approx(0.000037)
    assert state.funding.intervals_observed_hours == (8.0,)
    assert state.open_interest.total_usd is not None and state.open_interest.total_usd > 0
    # 3,135,780 contracts * 0.01 BTC = 31,357.8 BTC.
    assert state.open_interest.total_base == pytest.approx(31_357.8)
    assert state.basis.perp_basis_bps_median is not None
    assert state.structure.perp_venue_count == 1
    assert state.structure.spot_venue_count == 1

    # Spot was available, so the derivative was recorded as set aside rather than dropped.
    assert (
        state.reference_price.excluded_reasons.get(ExclusionReason.SPOT_PREFERRED.value) == 1
    )


def test_order_books_produce_a_liquidity_surface(config, conversion) -> None:
    observations = AssetObservations(
        asset=BTC,
        tickers=[
            (ticker("okx", 84400.0), spot("okx", USDT_ID)),
            (ticker("bitget", 84405.0), spot("bitget", USDT_ID)),
        ],
        order_books=[
            (book("okx", 84400.0), spot("okx", USDT_ID)),
            (book("bitget", 84405.0), spot("bitget", USDT_ID)),
        ],
        listing_venues={"okx", "bitget"},
        expected_venues={"okx", "bitget"},
    )
    bundle = build_market_state(
        observations,
        conversion=conversion,
        config=config,
        generation_id="g",
        snapshot_effective_time=SNAPSHOT,
        coverage_tier=CollectionTier.B_LIQUIDITY,
    )
    state = bundle.state

    assert state.coverage_tier is CollectionTier.B_LIQUIDITY
    assert state.liquidity.venue_count == 2
    assert len(state.liquidity.by_venue) == 2
    # Venue contributions are preserved, which is what makes the surface useful.
    assert all(v.depth_share is not None for v in state.liquidity.by_venue)
    assert sum(v.depth_share for v in state.liquidity.by_venue) == pytest.approx(1.0)
    assert state.quality.coverage.liquidity_venue_count == 2
    assert state.quality.freshness.order_book_age_seconds == pytest.approx(30.0)
    # Impact estimates exist; the large ones are null because the sampled books are thin.
    assert state.liquidity.impacts
    biggest = max(state.liquidity.impacts, key=lambda i: i.notional_usd)
    assert biggest.buy_impact_bps is None
    assert biggest.buy_depth_sufficient is False


def test_windowed_metrics_are_null_without_history(config, conversion) -> None:
    """At launch Atlas has no history, and says so rather than inventing values."""
    observations = AssetObservations(
        asset=BTC,
        tickers=[(ticker("okx", 84400.0), spot("okx", USDT_ID))],
        listing_venues={"okx"},
        expected_venues={"okx"},
    )
    state = build_market_state(
        observations,
        conversion=conversion,
        config=config,
        generation_id="g",
        snapshot_effective_time=SNAPSHOT,
    ).state

    assert state.volatility.realised_24h is None
    assert state.returns.change_24h is None
    assert state.technical.rsi_14 is None
    assert state.crowding.funding_percentile_30d is None
    assert state.genome.status is GenomeStatus.INSUFFICIENT_HISTORY
    assert state.genome.features == {}
    # The window coverage says exactly how far short the history falls.
    coverage = {w.window: w for w in state.volatility.windows}
    assert coverage["24h"].observations_present == 0
    assert coverage["24h"].observations_required > 0
    assert not coverage["24h"].sufficient


def test_pressure_and_crowding_are_vectors_with_availability_counts(config, conversion) -> None:
    observations = AssetObservations(
        asset=BTC,
        tickers=[(ticker("okx", 84400.0), spot("okx", USDT_ID))],
        order_books=[(book("okx", 84400.0), spot("okx", USDT_ID))],
        listing_venues={"okx"},
        expected_venues={"okx"},
    )
    state = build_market_state(
        observations,
        conversion=conversion,
        config=config,
        generation_id="g",
        snapshot_effective_time=SNAPSHOT,
    ).state

    # Book imbalance is computable from one snapshot; the standardised components are not.
    assert state.pressure.book_imbalance is not None
    assert state.pressure.funding_deviation_z is None
    assert state.pressure.components_available >= 1
    # Aggressive flow needs a trade tape Atlas does not collect, so it stays null.
    assert state.pressure.spot_flow_proxy is None
    assert state.pressure.derivative_flow_proxy is None
    assert state.crowding.components_available >= 0


def test_liquidations_are_declared_unavailable_rather_than_zero(config, conversion) -> None:
    observations = AssetObservations(
        asset=BTC,
        tickers=[(ticker("okx", 84400.0), spot("okx", USDT_ID))],
        listing_venues={"okx"},
        expected_venues={"okx"},
    )
    state = build_market_state(
        observations, conversion=conversion, config=config,
        generation_id="g", snapshot_effective_time=SNAPSHOT,
    ).state

    assert state.liquidations.aggregate_available is False
    assert state.liquidations.aggregate_long_usd is None
    assert state.liquidations.aggregate_short_usd is None
    assert state.liquidations.note is not None and "verified semantics" in state.liquidations.note


def test_reference_price_is_traceable_to_venue_rows(config, conversion) -> None:
    """A skeptical reader must be able to reconstruct the price from published rows."""
    observations = AssetObservations(
        asset=BTC,
        tickers=[
            (ticker("okx", 84400.0), spot("okx", USDT_ID)),
            (ticker("bitget", 84410.0), spot("bitget", USDT_ID)),
            (ticker("gateio", 84405.0), spot("gateio", USDT_ID)),
        ],
        listing_venues={"okx", "bitget", "gateio"},
        expected_venues={"okx", "bitget", "gateio"},
    )
    bundle = build_market_state(
        observations, conversion=conversion, config=config,
        generation_id="g", snapshot_effective_time=SNAPSHOT,
    )

    provenance = bundle.provenance_for("reference_price")
    assert provenance is not None
    assert len(provenance.entries) == 3
    assert provenance.methodology_version

    for entry in provenance.included:
        assert entry.weight is not None and entry.weight > 0
        assert entry.normalised_price_usd is not None
        assert entry.conversion is not None
        assert entry.price_source is not None
        assert entry.age_seconds == pytest.approx(30.0)

    # Weights sum to 1 across the included observations.
    assert sum(e.weight for e in provenance.included) == pytest.approx(1.0)

    # The venue rows carry the same weights, so the dataset alone is sufficient.
    weights_from_venues = {
        v.venue_slug: v.reference_weight for v in bundle.venues if v.included_in_reference_price
    }
    assert len(weights_from_venues) == 3
    assert sum(weights_from_venues.values()) == pytest.approx(1.0)
    for venue in bundle.venues:
        assert venue.deviation_bps is not None
        assert venue.volume_share is not None
    assert sum(v.volume_share for v in bundle.venues) == pytest.approx(1.0)


def test_venue_publishing_no_quote_volume_is_excluded_from_usd_totals(
    config, conversion
) -> None:
    """Kraken and Coinbase publish base volume only; their volume is not invented."""
    observations = AssetObservations(
        asset=BTC,
        tickers=[
            (ticker("okx", 84400.0, quote_volume=1e9), spot("okx", USDT_ID)),
            (ticker("kraken", 84405.0, quote_volume=None), spot("kraken", USDT_ID)),
        ],
        listing_venues={"okx", "kraken"},
        expected_venues={"okx", "kraken"},
    )
    state = build_market_state(
        observations, conversion=conversion, config=config,
        generation_id="g", snapshot_effective_time=SNAPSHOT,
    ).state

    # Only the venue that published quote volume enters the total.
    assert state.volume.venues_reporting_quote_volume == 1
    assert state.volume.instrument_count == 2
    assert state.volume.reported_spot_volume_24h_usd == pytest.approx(1e9 * 0.9997)
    # Both venues still count as price sources.
    assert state.venue_count == 2


def test_state_building_is_deterministic(config, conversion) -> None:
    """The same observations must produce the same state, whatever the order."""
    pairs = [
        (ticker("okx", 84400.0), spot("okx", USDT_ID)),
        (ticker("bitget", 84410.0), spot("bitget", USDT_ID)),
        (ticker("gateio", 84405.0), spot("gateio", USDT_ID)),
    ]
    venues = {"okx", "bitget", "gateio"}

    first = build_market_state(
        AssetObservations(asset=BTC, tickers=list(pairs), listing_venues=venues,
                          expected_venues=venues),
        conversion=conversion, config=config, generation_id="g",
        snapshot_effective_time=SNAPSHOT,
    )
    second = build_market_state(
        AssetObservations(asset=BTC, tickers=list(reversed(pairs)), listing_venues=venues,
                          expected_venues=venues),
        conversion=conversion, config=config, generation_id="g",
        snapshot_effective_time=SNAPSHOT,
    )

    assert first.state.reference_price.value == second.state.reference_price.value
    assert first.state.model_dump_json() == second.state.model_dump_json()
    assert first.venues == second.venues
