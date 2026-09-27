"""Liquidity measurement tests.

The arithmetic differs by contract denomination, and getting it wrong changes depth by
orders of magnitude without any obvious symptom. Each denomination is tested explicitly.
"""

from __future__ import annotations

import pytest

from vizanix_atlas.analytics.liquidity import (
    BookSample,
    aggregate_liquidity,
    depth_within,
    measure_instrument,
    walk_book_impact,
)
from vizanix_atlas.models.enums import ContractType, InstrumentType, OpenInterestUnit
from vizanix_atlas.models.instrument import Instrument
from vizanix_atlas.models.observations import ObservationTiming, OrderBookLevel, RawOrderBook

REFERENCE = 100.0
BANDS = (5, 10, 25, 50, 100)


def spot_instrument(venue: str = "okx") -> Instrument:
    return Instrument(
        instrument_id=f"instrument:{venue}:spot:BTC-USDT",
        venue_slug=venue,
        instrument_type=InstrumentType.SPOT,
        instrument_class="spot",
        symbol_native="BTC-USDT",
        symbol_normalized="BTC/USDT",
        base_asset_id="asset:native:bitcoin:BTC",
        quote_asset_id="asset:evm:1:0xdac17f958d2ee523a2206206994597c13d831ec7",
    )


def derivative_instrument(*, inverse: bool, multiplier: float | None) -> Instrument:
    return Instrument(
        instrument_id=f"instrument:okx:{'inverse' if inverse else 'linear'}-perp:BTC",
        venue_slug="okx",
        instrument_type=InstrumentType.PERPETUAL,
        instrument_class=f"{'inverse' if inverse else 'linear'}-perp",
        symbol_native="BTC-USD-SWAP" if inverse else "BTC-USDT-SWAP",
        symbol_normalized="BTC/USD",
        base_asset_id="asset:native:bitcoin:BTC",
        quote_asset_id="asset:fiat:usd",
        contract_type=ContractType.INVERSE if inverse else ContractType.LINEAR,
        contract_multiplier=multiplier,
        open_interest_unit=OpenInterestUnit.CONTRACTS,
        funding_interval_hours=8.0,
    )


def book(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> RawOrderBook:
    return RawOrderBook(
        venue_slug="okx",
        symbol_native="BTC-USDT",
        timing=ObservationTiming(collector_receive_time=1790530000000),
        bids=tuple(OrderBookLevel(price=p, size=s) for p, s in bids),
        asks=tuple(OrderBookLevel(price=p, size=s) for p, s in asks),
    )


def test_spot_depth_is_price_times_size() -> None:
    """One level of 2 units at 99.9 is 199.8 of quote notional."""
    levels = (OrderBookLevel(price=99.9, size=2.0),)
    depth = depth_within(
        levels,
        reference_price=REFERENCE,
        distance_bps=50,
        side="bid",
        instrument=spot_instrument(),
        quote_to_usd=1.0,
    )
    assert depth == pytest.approx(199.8)


def test_linear_derivative_depth_applies_the_multiplier() -> None:
    """A contract worth 0.01 base units makes 100 contracts worth 1 base unit."""
    levels = (OrderBookLevel(price=100.0, size=100.0),)
    depth = depth_within(
        levels,
        reference_price=REFERENCE,
        distance_bps=50,
        side="ask",
        instrument=derivative_instrument(inverse=False, multiplier=0.01),
        quote_to_usd=1.0,
    )
    # 100 contracts * 0.01 base each = 1 base unit, at 100 = 100 quote units.
    assert depth == pytest.approx(100.0)


def test_inverse_derivative_depth_ignores_the_price() -> None:
    """An inverse contract's value is fixed in the quote currency.

    A contract worth 100 USD is worth 100 USD whatever the price, so multiplying by the
    price as well would overstate depth by the price itself.
    """
    levels = (OrderBookLevel(price=100.0, size=7.0),)
    depth = depth_within(
        levels,
        reference_price=REFERENCE,
        distance_bps=50,
        side="ask",
        instrument=derivative_instrument(inverse=True, multiplier=100.0),
        quote_to_usd=1.0,
    )
    assert depth == pytest.approx(700.0)


def test_missing_multiplier_yields_no_depth_rather_than_a_guess() -> None:
    levels = (OrderBookLevel(price=100.0, size=5.0),)
    depth = depth_within(
        levels,
        reference_price=REFERENCE,
        distance_bps=50,
        side="ask",
        instrument=derivative_instrument(inverse=False, multiplier=None),
        quote_to_usd=1.0,
    )
    assert depth is None


def test_bands_are_measured_from_the_reference_price() -> None:
    """A venue quoting away from the market has little liquidity *at* the market.

    Measuring from the venue's own mid would hide that.
    """
    # The whole book sits 60 bps below the reference price.
    levels = (OrderBookLevel(price=99.40, size=1000.0),)
    inside_50 = depth_within(
        levels, reference_price=REFERENCE, distance_bps=50, side="bid",
        instrument=spot_instrument(), quote_to_usd=1.0,
    )
    inside_100 = depth_within(
        levels, reference_price=REFERENCE, distance_bps=100, side="bid",
        instrument=spot_instrument(), quote_to_usd=1.0,
    )
    assert inside_50 == pytest.approx(0.0), "nothing lies within 50 bps"
    assert inside_100 is not None and inside_100 > 0, "the level lies within 100 bps"


def test_quote_conversion_is_applied_to_depth() -> None:
    levels = (OrderBookLevel(price=100.0, size=1.0),)
    depth = depth_within(
        levels, reference_price=REFERENCE, distance_bps=50, side="bid",
        instrument=spot_instrument(), quote_to_usd=0.9997,
    )
    assert depth == pytest.approx(100.0 * 0.9997)


def test_impact_returns_none_when_visible_depth_is_insufficient() -> None:
    """A partial fill's average price understates the true cost, so it is not published."""
    levels = (OrderBookLevel(price=100.1, size=1.0),)  # 100.1 USD of visible depth
    impact, sufficient = walk_book_impact(
        levels,
        reference_price=REFERENCE,
        target_notional_usd=1_000_000.0,
        side="buy",
        instrument=spot_instrument(),
        quote_to_usd=1.0,
    )
    assert impact is None
    assert sufficient is False


def test_impact_is_the_vwap_deviation_when_depth_suffices() -> None:
    """Consuming two levels gives the notional-weighted average of their prices."""
    levels = (
        OrderBookLevel(price=100.0, size=5.0),   # 500 USD at 100.0
        OrderBookLevel(price=101.0, size=5.0),   # 505 USD at 101.0
    )
    impact, sufficient = walk_book_impact(
        levels,
        reference_price=REFERENCE,
        target_notional_usd=1000.0,
        side="buy",
        instrument=spot_instrument(),
        quote_to_usd=1.0,
    )
    assert sufficient is True
    # 500 at 100.0 then 500 at 101.0 gives a VWAP of 100.5, which is +50 bps.
    assert impact == pytest.approx(50.0, abs=0.5)


def test_sell_impact_is_reported_as_a_positive_cost() -> None:
    """Both directions are costs, so both are positive and comparable."""
    levels = (
        OrderBookLevel(price=100.0, size=5.0),
        OrderBookLevel(price=99.0, size=5.0),
    )
    impact, sufficient = walk_book_impact(
        levels,
        reference_price=REFERENCE,
        target_notional_usd=990.0,
        side="sell",
        instrument=spot_instrument(),
        quote_to_usd=1.0,
    )
    assert sufficient is True
    assert impact is not None and impact > 0


def test_aggregation_preserves_venue_contributions() -> None:
    """The surface must show how much of the depth is one venue."""
    samples = [
        BookSample(
            venue_slug="big",
            instrument=spot_instrument("big"),
            book=book([(99.9, 100.0)], [(100.1, 100.0)]),
            quote_to_usd=1.0,
            observed_at=1790530000000,
        ),
        BookSample(
            venue_slug="small",
            instrument=spot_instrument("small"),
            book=book([(99.9, 1.0)], [(100.1, 1.0)]),
            quote_to_usd=1.0,
            observed_at=1790530000000,
        ),
    ]
    state = aggregate_liquidity(
        samples,
        reference_price=REFERENCE,
        bands_bps=BANDS,
        impact_notionals_usd=(10_000.0,),
    )

    assert state.venue_count == 2
    assert len(state.by_venue) == 2
    shares = {v.venue_slug: v.depth_share for v in state.by_venue}
    assert shares["big"] is not None and shares["small"] is not None
    assert shares["big"] > 0.98, "the dominant venue's share must be visible"
    assert state.largest_venue_share == pytest.approx(shares["big"])
    # Concentration is high, and Atlas attaches no judgement to that.
    assert state.concentration_hhi is not None and state.concentration_hhi > 0.9
    # Depth aggregates across venues.
    band = state.band(50)
    assert band is not None and band.bid_depth_usd == pytest.approx(99.9 * 101.0)


def test_spread_is_best_and_median_not_a_sum() -> None:
    samples = [
        BookSample(
            venue_slug="tight",
            instrument=spot_instrument("tight"),
            book=book([(99.99, 10.0)], [(100.01, 10.0)]),
            quote_to_usd=1.0,
            observed_at=None,
        ),
        BookSample(
            venue_slug="wide",
            instrument=spot_instrument("wide"),
            book=book([(99.5, 10.0)], [(100.5, 10.0)]),
            quote_to_usd=1.0,
            observed_at=None,
        ),
    ]
    state = aggregate_liquidity(
        samples, reference_price=REFERENCE, bands_bps=BANDS, impact_notionals_usd=()
    )
    assert state.best_spread_bps is not None
    assert state.best_spread_bps == pytest.approx(2.0, abs=0.1)
    assert state.median_spread_bps is not None
    assert state.median_spread_bps > state.best_spread_bps


def test_book_imbalance_is_bounded_and_signed() -> None:
    samples = [
        BookSample(
            venue_slug="v",
            instrument=spot_instrument(),
            book=book([(99.9, 100.0)], [(100.1, 10.0)]),
            quote_to_usd=1.0,
            observed_at=None,
        )
    ]
    state = aggregate_liquidity(
        samples, reference_price=REFERENCE, bands_bps=BANDS, impact_notionals_usd=()
    )
    assert state.book_imbalance_50bps is not None
    assert 0.0 < state.book_imbalance_50bps <= 1.0, "more bids than asks is positive"


def test_no_reference_price_means_no_liquidity_metrics() -> None:
    """Depth bands are defined relative to the reference price."""
    samples = [
        BookSample(
            venue_slug="v",
            instrument=spot_instrument(),
            book=book([(99.9, 10.0)], [(100.1, 10.0)]),
            quote_to_usd=1.0,
            observed_at=None,
        )
    ]
    state = aggregate_liquidity(
        samples, reference_price=None, bands_bps=BANDS, impact_notionals_usd=()
    )
    assert state.venue_count == 0
    assert state.bands == ()


def test_unvaluable_books_are_excluded_not_counted_as_zero() -> None:
    samples = [
        BookSample(
            venue_slug="v",
            instrument=spot_instrument(),
            book=book([(99.9, 10.0)], [(100.1, 10.0)]),
            quote_to_usd=None,
            observed_at=None,
        )
    ]
    state = aggregate_liquidity(
        samples, reference_price=REFERENCE, bands_bps=BANDS, impact_notionals_usd=()
    )
    assert state.venue_count == 0


def test_truncation_is_carried_through() -> None:
    """Depth from a truncated book is a lower bound, and says so."""
    truncated = RawOrderBook(
        venue_slug="okx",
        symbol_native="BTC-USDT",
        timing=ObservationTiming(collector_receive_time=1790530000000),
        bids=(OrderBookLevel(price=99.9, size=1.0),),
        asks=(OrderBookLevel(price=100.1, size=1.0),),
        truncated=True,
    )
    measured = measure_instrument(
        BookSample(
            venue_slug="okx",
            instrument=spot_instrument(),
            book=truncated,
            quote_to_usd=1.0,
            observed_at=None,
        ),
        reference_price=REFERENCE,
        bands_bps=BANDS,
    )
    assert measured is not None
    assert measured.book_truncated is True
