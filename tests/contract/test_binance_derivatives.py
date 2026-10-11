"""Binance derivatives adapter contract tests (USD-M and COIN-M).

Driven by payloads captured from ``fapi.binance.com`` and ``dapi.binance.com`` on 2026-10-11
and trimmed to representative symbols (``tests/fixtures/exchanges/binance_derivatives/``).
The assertions encode the unit semantics checked against those captures.
"""

from __future__ import annotations

import pytest

from tests.conftest import RouteRecorder, make_client, venue_for
from tests.contract.wiring import WIRING
from vizanix_atlas.adapters.binance_derivatives import BinanceDerivativesAdapter
from vizanix_atlas.models.enums import OpenInterestUnit


def _wire(recorder: RouteRecorder) -> BinanceDerivativesAdapter:
    WIRING["binance_derivatives"](recorder)
    client = make_client(
        recorder, venue_slug="binance_derivatives", base_url=BinanceDerivativesAdapter.base_url
    )
    return BinanceDerivativesAdapter(venue_for("binance_derivatives"), client)


async def test_usd_m_instruments_are_linear_and_use_published_funding_intervals(
    routes: RouteRecorder,
) -> None:
    adapter = _wire(routes)
    by_symbol = {i.symbol_native: i for i in await adapter.discover_instruments()}

    btc = by_symbol["BTCUSDT"]
    assert btc.instrument_class == "linear-perp" and btc.instrument_type == "perpetual"
    assert btc.contract_type == "linear"
    assert btc.settlement_symbol_native == "USDT"
    assert btc.contract_multiplier == pytest.approx(1.0)
    assert btc.contract_value_symbol_native == "BTC"
    assert btc.tick_size == pytest.approx(0.10)
    assert btc.quantity_step == pytest.approx(0.001)
    assert btc.minimum_notional == pytest.approx(50.0)
    assert btc.open_interest_unit == OpenInterestUnit.BASE_ASSET.value
    # fundingInfo publishes the interval per symbol: 8h, 4h and 1h all occur on the venue.
    assert btc.funding_interval_hours == pytest.approx(8.0)
    assert by_symbol["STRKUSDT"].funding_interval_hours == pytest.approx(4.0)
    assert by_symbol["KAIAUSDT"].funding_interval_hours == pytest.approx(1.0)

    assert by_symbol["BTCUSDC"].settlement_symbol_native == "USDC"


async def test_usd_m_dated_contracts_carry_an_expiry_and_no_funding(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    by_symbol = {i.symbol_native: i for i in await adapter.discover_instruments()}

    quarter = by_symbol["BTCUSDT_261225"]
    assert quarter.instrument_class == "linear-future" and quarter.instrument_type == "future"
    assert quarter.expiry == 1798185600000
    assert quarter.funding_interval_hours is None
    assert by_symbol["ETHUSDT_270326"].expiry == 1806048000000


async def test_coin_m_instruments_are_inverse_with_a_contract_size(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    by_symbol = {i.symbol_native: i for i in await adapter.discover_instruments()}

    btc = by_symbol["BTCUSD_PERP"]
    assert btc.instrument_class == "inverse-perp"
    assert btc.contract_type == "inverse"
    assert btc.settlement_symbol_native == "BTC"
    # contractSize: 100 USD per BTC contract, 10 USD for the others.
    assert btc.contract_multiplier == pytest.approx(100.0)
    assert btc.contract_value_symbol_native == "USD"
    assert btc.open_interest_unit == OpenInterestUnit.CONTRACTS.value
    # dapi fundingInfo is empty because every coin-margined contract funds every 8 hours.
    assert btc.funding_interval_hours == pytest.approx(8.0)
    assert by_symbol["ETHUSD_PERP"].contract_multiplier == pytest.approx(10.0)

    future = by_symbol["BTCUSD_261225"]
    assert future.instrument_class == "inverse-future" and future.expiry == 1798185600000


async def test_non_crypto_and_non_trading_contracts_are_skipped(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    symbols = {i.symbol_native for i in await adapter.discover_instruments()}

    assert "XAUUSDT" not in symbols, "TRADIFI_PERPETUAL commodity contract"
    assert "AAPLUSDT" not in symbols, "TRADIFI_PERPETUAL equity contract"
    assert "OMGUSDT" not in symbols, "status SETTLING is not a live market"
    # An index perpetual on a crypto index is still a crypto market.
    assert "BTCDOMUSDT" in symbols


async def test_usd_m_ticker_units_and_top_of_book(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    tickers = {(t.instrument_class, t.symbol_native): t for t in await adapter.fetch_tickers()}

    btc = tickers[("linear-perp", "BTCUSDT")]
    assert btc.base_volume_24h == pytest.approx(59472.154)
    assert btc.quote_volume_24h == pytest.approx(4959870998.67)
    assert btc.contract_volume_24h is None
    # Best bid and ask come from the bulk bookTicker endpoint.
    assert btc.bid_price == pytest.approx(83656.60) and btc.ask_price == pytest.approx(83656.70)
    assert btc.bid_size == pytest.approx(4.413)


async def test_coin_m_ticker_units(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    tickers = {(t.instrument_class, t.symbol_native): t for t in await adapter.fetch_tickers()}

    btc = tickers[("inverse-perp", "BTCUSD_PERP")]
    # `volume` is contracts of 100 USD, `baseVolume` is BTC.
    assert btc.contract_volume_24h == pytest.approx(3575480.0)
    assert btc.base_volume_24h == pytest.approx(4291.4)
    assert btc.quote_volume_24h is None
    # Cross-check the capture: contracts x 100 USD / price is the BTC volume.
    assert btc.contract_volume_24h * 100 / btc.last_price == pytest.approx(
        btc.base_volume_24h, rel=0.01
    )
    assert tickers[("inverse-future", "BTCUSD_261225")].instrument_class == "inverse-future"


async def test_derivative_observations_for_usd_m(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    await adapter.discover_instruments()
    observed = {(o.instrument_class, o.symbol_native): o for o in await adapter.fetch_derivatives()}

    btc = observed[("linear-perp", "BTCUSDT")]
    assert btc.mark_price == pytest.approx(83657.10995652)
    assert btc.index_price == pytest.approx(83693.61586957)
    assert btc.funding_rate_raw == pytest.approx(0.00001986)
    assert btc.funding_interval_hours == pytest.approx(8.0)
    assert btc.next_funding_time == 1791763200000
    # The sampled open interest is in base units for USD-M.
    assert btc.open_interest_raw == pytest.approx(92642.305)
    assert btc.open_interest_unit is OpenInterestUnit.BASE_ASSET
    assert btc.open_interest_base == pytest.approx(92642.305)

    dated = observed[("linear-future", "BTCUSDT_261225")]
    assert dated.funding_rate_raw is None and dated.next_funding_time is None


async def test_derivative_observations_for_coin_m(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    await adapter.discover_instruments()
    observed = {(o.instrument_class, o.symbol_native): o for o in await adapter.fetch_derivatives()}

    btc = observed[("inverse-perp", "BTCUSD_PERP")]
    assert btc.funding_interval_hours == pytest.approx(8.0)
    # COIN-M open interest is contracts; Atlas converts with the contract size.
    assert btc.open_interest_raw == pytest.approx(13016199.0)
    assert btc.open_interest_unit is OpenInterestUnit.CONTRACTS
    assert btc.open_interest_base is None


async def test_open_interest_is_sampled_not_requested_for_every_contract(
    routes: RouteRecorder,
) -> None:
    """Binance has no bulk open-interest endpoint: the per-symbol requests must stay bounded."""
    adapter = _wire(routes)
    await adapter.discover_instruments()
    await adapter.fetch_tickers()
    before = routes.request_count
    await adapter.fetch_derivatives()

    open_interest_requests = [
        r for r in routes.requests[before:] if r.url.path.endswith("/openInterest")
    ]
    # The fixture holds fewer contracts than the sample size, so every one is requested,
    # but never more than the documented sample.
    assert 0 < len(open_interest_requests) <= 25


async def test_standalone_derivative_collection_ranks_without_a_ticker_pass(
    routes: RouteRecorder,
) -> None:
    adapter = _wire(routes)
    observed = await adapter.fetch_derivatives()
    assert any(o.open_interest_raw is not None for o in observed)


async def test_order_books_use_the_matching_host_and_class(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    await adapter.discover_instruments()

    usd_m = await adapter.fetch_order_book("BTCUSDT", depth=10)
    assert usd_m.instrument_class == "linear-perp"
    assert routes.requests[-1].url.host == "fapi.binance.com"

    coin_m = await adapter.fetch_order_book("BTCUSD_PERP", depth=10)
    assert coin_m.instrument_class == "inverse-perp"
    assert routes.requests[-1].url.host == "dapi.binance.com"
    assert not usd_m.is_crossed and not coin_m.is_crossed


async def test_server_time(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    assert await adapter.server_time_ms() == 1791751171305


async def test_quotes_for_skipped_contracts_are_not_emitted_after_discovery(
    routes: RouteRecorder,
) -> None:
    """TradFi and non-trading contracts must not leak into tickers or derivatives."""
    adapter = _wire(routes)
    await adapter.discover_instruments()
    tickers = {t.symbol_native for t in await adapter.fetch_tickers()}
    derivatives = {o.symbol_native for o in await adapter.fetch_derivatives()}
    for skipped in ("XAUUSDT", "AAPLUSDT", "OMGUSDT"):
        assert skipped not in tickers
        assert skipped not in derivatives
    assert {"BTCUSDT", "BTCUSD_PERP"} <= tickers
