"""The live book: merging, the reference price, expiry and stablecoin handling."""

from __future__ import annotations

import pytest

from vizanix_atlas.core.config import load_collection_config
from vizanix_atlas.stream.book import LiveBook
from vizanix_atlas.stream.config import ReferenceSettings
from vizanix_atlas.stream.types import LiveTick
from vizanix_atlas.stream.universe import AssetInfo, InstrumentRef, Universe

BTC = "asset:native:bitcoin:BTC"
USDT = "asset:evm:1:usdt"
USD = "asset:fiat:usd"
T0 = 1_800_000_000_000


def settings(**overrides) -> ReferenceSettings:
    base = {
        "stale_after_seconds": 300,
        "max_deviation_bps": 300,
        "min_venues_for_rejection": 3,
        "disconnect_grace_seconds": 20,
    }
    return ReferenceSettings(**{**base, **overrides})


def make_universe(*, extra: dict | None = None) -> Universe:
    universe = Universe(generation_id="20261012T093000Z", quote_rates={USD: 1.0, USDT: 0.999})
    universe.assets[BTC] = AssetInfo(BTC, "BTC", "Bitcoin", 100_000.0, 2.4e10, 1)
    universe.instruments[("binance", "spot", "BTCUSDT")] = InstrumentRef(BTC, USDT)
    universe.instruments[("okx", "spot", "BTC-USDT")] = InstrumentRef(BTC, USDT)
    universe.instruments[("coinbase", "spot", "BTC-USD")] = InstrumentRef(BTC, USD)
    universe.instruments[("coinbase", "spot", "BTC-USDT")] = InstrumentRef(BTC, USDT)
    universe.instruments[("kraken", "spot", "BTC-USD")] = InstrumentRef(BTC, USD)
    universe.instruments[("bybit", "linear-perp", "BTCUSDT")] = InstrumentRef(BTC, USDT)
    universe.rate_probes[("coinbase", "spot", "USDT-USD")] = USDT
    for key, ref in (extra or {}).items():
        universe.instruments[key] = ref
    return universe


def make_book(universe: Universe | None = None, **overrides) -> LiveBook:
    book = LiveBook(
        reference=settings(**overrides), weighting=load_collection_config().reference_price
    )
    book.set_universe(universe or make_universe())
    for venue, cls in (
        ("binance", "spot"),
        ("okx", "spot"),
        ("coinbase", "spot"),
        ("kraken", "spot"),
        ("bybit", "linear-perp"),
    ):
        book.set_feed_connected(venue, cls, True, T0)
    return book


def tick(venue, symbol, *, cls="spot", at=T0, **fields) -> LiveTick:
    return LiveTick(venue=venue, instrument_class=cls, symbol=symbol, received_ms=at, **fields)


def test_price_is_a_robust_median_across_venues():
    book = make_book()
    book.ingest(
        [
            tick("binance", "BTCUSDT", last=100_100.0, quote_volume_24h=9e8),
            tick("okx", "BTC-USDT", bid=100_090.0, ask=100_110.0, quote_volume_24h=3e8),
            tick("coinbase", "BTC-USD", bid=100_000.0, ask=100_020.0, base_volume_24h=5_000.0),
        ]
    )
    live = book.compute(BTC, T0)
    assert live is not None
    assert live.venues_used == 3 and live.method == "robust_weighted_median"
    # Binance 100100*0.999 = 99999.9, OKX mid 100100*0.999 = 99999.9, Coinbase mid 100010.
    assert 99_990 < live.price < 100_020
    assert live.dispersion_bps is not None and live.dispersion_bps < 5
    assert live.vs_generation_bps == pytest.approx((live.price - 100_000.0) / 100_000.0 * 1e4)


def test_mid_is_preferred_to_last_and_wide_spreads_fall_back_to_last():
    book = make_book()
    book.ingest([tick("coinbase", "BTC-USD", last=99_000.0, bid=100_000.0, ask=100_010.0)])
    quote = book.compute(BTC, T0).quotes[0]
    assert quote.source == "mid" and quote.price_usd == pytest.approx(100_005.0)

    book = make_book()
    # 5% spread: not a live two-sided quote worth a mid.
    book.ingest([tick("coinbase", "BTC-USD", last=100_000.0, bid=97_000.0, ask=103_000.0)])
    quote = book.compute(BTC, T0).quotes[0]
    assert quote.source == "last" and quote.price_usd == 100_000.0


def test_quote_currency_is_converted_through_its_usd_rate():
    book = make_book()
    book.ingest([tick("binance", "BTCUSDT", last=100_000.0)])
    live = book.compute(BTC, T0)
    assert live.price == pytest.approx(99_900.0)  # USDT at 0.999


def test_stablecoin_rate_updates_from_its_own_live_pair_within_parity():
    book = make_book()
    book.ingest([tick("binance", "BTCUSDT", last=100_000.0)])
    book.take_dirty()
    book.ingest([tick("coinbase", "USDT-USD", bid=0.9980, ask=0.9984)])
    assert book.quote_rates[USDT] == pytest.approx(0.9982)
    assert BTC in book.take_dirty()
    assert book.compute(BTC, T0).price == pytest.approx(99_820.0)

    # A "rate" 20% off parity is a broken market, not a depeg worth pricing through.
    book.ingest([tick("coinbase", "USDT-USD", bid=0.79, ask=0.80)])
    assert book.quote_rates[USDT] == pytest.approx(0.9982)


def test_extreme_outlier_is_excluded_with_a_reason_when_a_majority_exists():
    book = make_book()
    book.ingest(
        [
            tick("binance", "BTCUSDT", last=100_100.0),
            tick("okx", "BTC-USDT", last=100_105.0),
            tick("coinbase", "BTC-USD", last=100_000.0),
            tick("kraken", "BTC-USD", last=125_000.0),
        ]
    )
    live = book.compute(BTC, T0)
    assert live.venues_used == 3 and live.venues_seen == 4
    outlier = next(q for q in live.quotes if q.venue == "kraken")
    assert not outlier.included and outlier.reason == "extreme_deviation"
    assert 99_900 < live.price < 100_100


def test_two_venues_cannot_outvote_each_other_so_nothing_is_rejected():
    book = make_book()
    book.ingest(
        [tick("binance", "BTCUSDT", last=100_000.0), tick("okx", "BTC-USDT", last=130_000.0)]
    )
    live = book.compute(BTC, T0)
    assert live.venues_used == 2  # no majority to define "the market"; both stay, dispersion shows
    assert live.dispersion_bps > 100


def test_one_candidate_per_venue_the_most_liquid_market_wins():
    book = make_book()
    book.ingest(
        [
            tick("coinbase", "BTC-USD", last=100_000.0, base_volume_24h=100.0),
            tick("coinbase", "BTC-USDT", last=100_300.0, base_volume_24h=900.0),
        ]
    )
    live = book.compute(BTC, T0)
    assert live.venues_used == 1 and live.method == "single_venue"
    assert live.quotes[0].symbol == "BTC-USDT"


def test_volume_is_converted_to_usd_from_whichever_unit_the_venue_states():
    book = make_book()
    book.ingest(
        [
            tick("binance", "BTCUSDT", last=100_000.0, quote_volume_24h=1_000_000.0),
            tick("coinbase", "BTC-USD", last=100_000.0, base_volume_24h=10.0),
        ]
    )
    volumes = {q.venue: q.volume_usd for q in book.compute(BTC, T0).quotes}
    assert volumes["binance"] == pytest.approx(999_000.0)
    assert volumes["coinbase"] == pytest.approx(1_000_000.0)


def test_quotes_expire_and_a_stale_only_asset_has_no_price():
    book = make_book()
    book.ingest([tick("binance", "BTCUSDT", last=100_000.0)])
    assert book.compute(BTC, T0 + 299_000) is not None
    assert book.compute(BTC, T0 + 301_000) is None  # never serve a stale number as live


def test_stale_quote_is_listed_with_its_reason_when_other_venues_remain():
    book = make_book()
    book.ingest([tick("binance", "BTCUSDT", last=100_000.0, at=T0 - 400_000)])
    book.ingest([tick("okx", "BTC-USDT", last=100_000.0)])
    live = book.compute(BTC, T0)
    stale = next(q for q in live.quotes if q.venue == "binance")
    assert stale.reason == "stale" and not stale.included
    assert live.venues_used == 1


def test_disconnected_feed_keeps_counting_only_during_the_grace_period():
    book = make_book()
    book.ingest([tick("binance", "BTCUSDT", last=100_000.0)])
    book.set_feed_connected("binance", "spot", False, T0)
    assert book.compute(BTC, T0 + 10_000) is not None
    assert book.compute(BTC, T0 + 25_000) is None
    book.set_feed_connected("binance", "spot", True, T0 + 30_000)
    book.ingest([tick("binance", "BTCUSDT", last=100_100.0, at=T0 + 30_000)])
    assert book.compute(BTC, T0 + 31_000) is not None


def test_a_feed_that_never_connected_contributes_nothing():
    book = make_book()
    book.feeds.pop(("binance", "spot"))
    book.ingest([tick("binance", "BTCUSDT", last=100_000.0)])
    assert book.compute(BTC, T0) is None


def test_unfollowed_instruments_are_counted_and_ignored():
    book = make_book()
    book.ingest([tick("binance", "DOGEUSDT", last=0.2)])
    assert book.ticks_ignored == 1 and book.ticks_seen == 1
    assert book.compute(BTC, T0) is None


def test_perpetual_context_scales_funding_to_eight_hours_and_reports_basis():
    book = make_book()
    book.ingest([tick("binance", "BTCUSDT", last=100_000.0)])  # reference 99_900 (USDT 0.999)
    book.ingest(
        [
            tick(
                "bybit",
                "BTCUSDT",
                cls="linear-perp",
                last=100_100.0,
                mark=100_200.0,
                index=100_000.0,
                funding_rate=0.0001,
                funding_interval_hours=4.0,
                open_interest_usd=4.7e9,
            )
        ]
    )
    live = book.compute(BTC, T0)
    (perp,) = live.perps
    assert perp.funding_8h == pytest.approx(0.0002)
    assert perp.open_interest_usd == 4.7e9
    # mark 100200 USDT = 100099.8 USD against a 99900 reference.
    assert perp.basis_bps == pytest.approx((100_200 * 0.999 - live.price) / live.price * 1e4)
    # A perpetual never moves the spot reference price.
    assert live.price == pytest.approx(99_900.0)


def test_deltas_merge_into_state_instead_of_replacing_it():
    book = make_book()
    book.ingest(
        [
            tick(
                "bybit",
                "BTCUSDT",
                cls="linear-perp",
                last=100_000.0,
                mark=100_050.0,
                funding_rate=0.0001,
                funding_interval_hours=8.0,
                open_interest_usd=1e9,
            )
        ]
    )
    book.ingest([tick("bybit", "BTCUSDT", cls="linear-perp", last=100_010.0)])
    state = book.states[("bybit", "linear-perp", "BTCUSDT")]
    assert state.last == 100_010.0 and state.mark == 100_050.0 and state.open_interest_usd == 1e9


def test_dirty_tracking_and_universe_refresh_keeps_state():
    book = make_book()
    book.take_dirty()
    book.ingest([tick("binance", "BTCUSDT", last=100_000.0)])
    assert book.take_dirty() == [BTC] and book.take_dirty() == []
    book.set_universe(make_universe())  # a refresh with the same markets
    assert book.states[("binance", "spot", "BTCUSDT")].last == 100_000.0
    book.sweep()
    assert book.take_dirty() == [BTC]


def test_snapshot_lists_only_assets_with_a_live_price_in_rank_order():
    universe = make_universe()
    eth = "asset:native:ethereum:ETH"
    universe.assets[eth] = AssetInfo(eth, "ETH", "Ether", 2_500.0, 1e10, 2)
    universe.instruments[("binance", "spot", "ETHUSDT")] = InstrumentRef(eth, USDT)
    book = make_book(universe)
    book.ingest([tick("binance", "BTCUSDT", last=100_000.0)])
    assert [a.symbol for a in book.snapshot(T0)] == ["BTC"]
    book.ingest([tick("binance", "ETHUSDT", last=2_500.0)])
    assert [a.symbol for a in book.snapshot(T0)] == ["BTC", "ETH"]


def test_detail_serialises_to_strict_json():
    import json

    book = make_book()
    book.ingest([tick("binance", "BTCUSDT", last=100_000.0, quote_volume_24h=1e9)])
    detail = book.compute(BTC, T0).detail()
    json.dumps(detail, allow_nan=False)
    assert detail["symbol"] == "BTC" and detail["quotes"][0]["venue"] == "binance"


def test_public_detail_explains_venues_without_republishing_their_raw_quotes():
    book = make_book()
    book.ingest(
        [
            tick("binance", "BTCUSDT", last=100_000.0, quote_volume_24h=1e9),
            tick(
                "bybit",
                "BTCUSDT",
                cls="linear-perp",
                last=100_100.0,
                mark=100_200.0,
                index=100_000.0,
                funding_rate=0.0001,
                funding_interval_hours=8.0,
                open_interest_usd=1e9,
            ),
        ]
    )
    detail = book.compute(BTC, T0).detail()
    (quote,) = detail["quotes"]
    assert set(quote) == {
        "venue",
        "symbol",
        "source",
        "age_ms",
        "weight",
        "deviation_bps",
        "included",
        "reason",
    }
    (perp,) = detail["perpetuals"]
    assert set(perp) == {"venue", "symbol", "funding_8h", "open_interest_usd", "basis_bps"}
