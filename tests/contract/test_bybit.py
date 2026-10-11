"""Bybit adapter contract tests.

Driven by payloads captured from the live V5 API on 2026-10-11 and trimmed to a handful of
representative symbols (``tests/fixtures/exchanges/bybit/``). The assertions encode the unit
semantics checked against those captures, so a regression in the arithmetic fails here
rather than silently changing a published volume or open-interest figure.
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import RouteRecorder, load_fixture, make_client, venue_for
from tests.contract.wiring import WIRING
from vizanix_atlas.adapters.bybit import BybitAdapter
from vizanix_atlas.core.errors import ExchangeApplicationError
from vizanix_atlas.models.enums import OpenInterestUnit


def _wire(recorder: RouteRecorder) -> BybitAdapter:
    WIRING["bybit"](recorder)
    client = make_client(recorder, venue_slug="bybit", base_url=BybitAdapter.base_url)
    return BybitAdapter(venue_for("bybit"), client)


async def test_instrument_classes_and_contract_semantics(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    by_key = {
        (i.instrument_class, i.symbol_native): i for i in await adapter.discover_instruments()
    }

    spot = by_key[("spot", "BTCUSDT")]
    assert spot.instrument_type == "spot"
    assert spot.contract_type is None and spot.contract_multiplier is None

    # The same native symbol is listed as spot and as a linear perpetual: classes keep them apart.
    linear = by_key[("linear-perp", "BTCUSDT")]
    assert linear.instrument_type == "perpetual"
    assert linear.contract_type == "linear"
    assert linear.settlement_symbol_native == "USDT"
    assert linear.open_interest_unit == OpenInterestUnit.BASE_ASSET.value
    # fundingInterval is published in minutes: 480 minutes is 8 hours, 240 is 4.
    assert linear.funding_interval_hours == pytest.approx(8.0)
    assert by_key[("linear-perp", "0GUSDT")].funding_interval_hours == pytest.approx(4.0)

    dated = by_key[("linear-future", "BTCUSDT-16OCT26")]
    assert dated.instrument_type == "future"
    assert dated.expiry == 1792137600000
    assert dated.funding_interval_hours is None, "a dated contract has no funding"

    inverse = by_key[("inverse-perp", "BTCUSD")]
    assert inverse.contract_type == "inverse"
    assert inverse.settlement_symbol_native == "BTC"
    # An inverse contract is one USD of notional.
    assert inverse.contract_multiplier == pytest.approx(1.0)
    assert inverse.contract_value_symbol_native == "USD"
    assert inverse.open_interest_unit == OpenInterestUnit.CONTRACTS.value

    inverse_future = by_key[("inverse-future", "BTCUSDH27")]
    assert inverse_future.expiry == 1806048000000


async def test_non_crypto_markets_are_skipped(routes: RouteRecorder) -> None:
    """Tokenised equities, ETFs, commodities and FX are not crypto market state."""
    adapter = _wire(routes)
    symbols = {i.symbol_native for i in await adapter.discover_instruments()}

    for skipped in ("AAPLUSDT", "ARKKUSDT", "XAUUSDT", "EURUSDUSDT", "NVDAXUSDT"):
        assert skipped not in symbols
    # `innovation` and `adventure` are crypto and must survive.
    assert {"1000TAGUSDT", "MCRTUSDT"} <= symbols


async def test_discovery_uses_one_request_per_category(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    await adapter.discover_instruments()
    assert routes.paths() == ["/v5/market/instruments-info"] * 3


async def test_catalogue_follows_pagination_cursors(routes: RouteRecorder) -> None:
    """A venue that paginates must not lose its second page."""
    page_one = load_fixture("bybit", "instruments_linear")
    page_one["result"]["nextPageCursor"] = "page2"
    page_two = load_fixture("bybit", "instruments_linear")
    page_two["result"]["nextPageCursor"] = ""
    page_two["result"]["list"] = [
        dict(page_two["result"]["list"][0], symbol="ZZZUSDT", baseCoin="ZZZ")
    ]

    def linear(request: httpx.Request) -> bool:
        return (
            request.url.path == "/v5/market/instruments-info"
            and request.url.params.get("category") == "linear"
        )

    adapter = _wire(routes)
    # Routes registered later lose to earlier ones, so the two-page responder goes first.
    routes.routes.insert(
        0,
        (
            lambda r: linear(r) and r.url.params.get("cursor") == "page2",
            httpx.Response(200, json=page_two),
        ),
    )
    routes.routes.insert(
        1,
        (lambda r: linear(r) and "cursor" not in r.url.params, httpx.Response(200, json=page_one)),
    )

    symbols = {i.symbol_native for i in await adapter.discover_instruments()}
    assert "ZZZUSDT" in symbols, "the second page was dropped"
    assert "BTCUSDT" in symbols


async def test_linear_ticker_units(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    tickers = {(t.instrument_class, t.symbol_native): t for t in await adapter.fetch_tickers()}

    btc = tickers[("linear-perp", "BTCUSDT")]
    # volume24h is base volume and turnover24h is quote volume for linear markets.
    assert btc.base_volume_24h == pytest.approx(21470.744)
    assert btc.quote_volume_24h == pytest.approx(1791749323.3099)
    assert btc.contract_volume_24h is None
    assert btc.bid_price and btc.ask_price and btc.bid_price <= btc.ask_price


async def test_inverse_ticker_units(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    tickers = {(t.instrument_class, t.symbol_native): t for t in await adapter.fetch_tickers()}

    inverse = tickers[("inverse-perp", "BTCUSD")]
    # volume24h is contracts (1 USD each); turnover24h is the base coin.
    assert inverse.contract_volume_24h == pytest.approx(130342124.0)
    assert inverse.base_volume_24h == pytest.approx(1566.7181)
    assert inverse.quote_volume_24h is None, "quote volume is never derived"
    # Cross-check the captured numbers: contracts / price must equal the base-coin volume.
    assert inverse.contract_volume_24h / inverse.last_price == pytest.approx(
        inverse.base_volume_24h, rel=0.01
    )


async def test_ticker_class_separates_perpetuals_from_dated_contracts(
    routes: RouteRecorder,
) -> None:
    adapter = _wire(routes)
    classes = {
        t.symbol_native: t.instrument_class
        for t in await adapter.fetch_tickers()
        if t.symbol_native in {"BTCUSDT-16OCT26", "BTCUSDH27"}
    }
    assert classes == {"BTCUSDT-16OCT26": "linear-future", "BTCUSDH27": "inverse-future"}


async def test_derivative_units_linear(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    observed = {(o.instrument_class, o.symbol_native): o for o in await adapter.fetch_derivatives()}

    btc = observed[("linear-perp", "BTCUSDT")]
    assert btc.mark_price == pytest.approx(83662.10)
    assert btc.index_price == pytest.approx(83696.47)
    assert btc.funding_rate_raw == pytest.approx(0.00005509)
    assert btc.funding_interval_hours == pytest.approx(8.0)
    assert btc.next_funding_time == 1791763200000
    # openInterest is base units for linear contracts.
    assert btc.open_interest_raw == pytest.approx(55270.099)
    assert btc.open_interest_unit is OpenInterestUnit.BASE_ASSET
    assert btc.open_interest_base == pytest.approx(55270.099)
    # Self-consistency of the capture: base OI times mark is the published quote value.
    assert btc.open_interest_base * btc.mark_price == pytest.approx(4624012549.55, rel=0.001)

    future = observed[("linear-future", "BTCUSDT-16OCT26")]
    assert future.funding_rate_raw is None and future.next_funding_time is None


async def test_derivative_units_inverse(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    observed = {(o.instrument_class, o.symbol_native): o for o in await adapter.fetch_derivatives()}

    btc = observed[("inverse-perp", "BTCUSD")]
    # openInterest is contracts; openInterestValue is the base-coin equivalent.
    assert btc.open_interest_raw == pytest.approx(520588058.0)
    assert btc.open_interest_unit is OpenInterestUnit.CONTRACTS
    assert btc.open_interest_base == pytest.approx(6228.39)
    # Cross-check: contracts (USD) / mark price is the base-coin value.
    assert btc.open_interest_raw / btc.mark_price == pytest.approx(
        btc.open_interest_base, rel=0.001
    )


async def test_order_book_uses_the_discovered_category(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    await adapter.discover_instruments()

    book = await adapter.fetch_order_book("BTCUSDT", depth=10)
    # BTCUSDT is listed on both markets; spot wins, and the request says so.
    assert book.instrument_class == "spot"
    assert routes.requests[-1].url.params["category"] == "spot"
    assert len(book.bids) == len(book.asks) == 10
    assert not book.is_crossed


async def test_application_errors_are_not_swallowed(routes: RouteRecorder) -> None:
    routes.on_path("/v5/market/instruments-info", {"retCode": 10001, "retMsg": "bad", "result": {}})
    client = make_client(routes, venue_slug="bybit", base_url=BybitAdapter.base_url)
    adapter = BybitAdapter(venue_for("bybit"), client)
    with pytest.raises(ExchangeApplicationError):
        await adapter.discover_instruments()


async def test_server_time(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    assert await adapter.server_time_ms() == 1791751161716


async def test_quotes_for_skipped_markets_are_not_emitted_after_discovery(
    routes: RouteRecorder,
) -> None:
    """Skipped non-crypto markets must not leak into tickers or derivative observations."""
    adapter = _wire(routes)
    # Without a catalogue every row is allowed through, so the fixture does hold the rows.
    standalone = {t.symbol_native for t in await _wire(RouteRecorder()).fetch_tickers()}
    assert "AAPLUSDT" in standalone

    await adapter.discover_instruments()
    tickers = {t.symbol_native for t in await adapter.fetch_tickers()}
    derivatives = {o.symbol_native for o in await adapter.fetch_derivatives()}
    for skipped in ("AAPLUSDT", "ARKKUSDT", "XAUUSDT", "EURUSDUSDT", "NVDAXUSDT"):
        assert skipped not in tickers
        assert skipped not in derivatives
    assert "BTCUSDT" in tickers and "BTCUSDT" in derivatives
