"""Fixture wiring for every adapter.

One place that knows which recorded payload answers which request, for each venue.
Contract tests use it so that the universal invariants in
``test_all_adapters.py`` run against every adapter without sixteen bespoke setups.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tests.conftest import RouteRecorder, load_fixture

#: An empty response in each venue's own envelope shape, for the product types a
#: fixture set does not cover. The adapter's loop must still complete.
_EMPTY: dict[str, Any] = {}


def _okx(r: RouteRecorder) -> None:
    empty = {"code": "0", "msg": "", "data": []}
    for inst_type, instruments, tickers in (
        ("SPOT", "instruments_spot", "tickers_spot"),
        ("SWAP", "instruments_swap", "tickers_swap"),
    ):
        r.on_path_param(
            "/api/v5/public/instruments", "instType", inst_type, load_fixture("okx", instruments)
        )
        r.on_path_param(
            "/api/v5/market/tickers", "instType", inst_type, load_fixture("okx", tickers)
        )
    r.on_path_param("/api/v5/public/instruments", "instType", "FUTURES", empty)
    for inst_type in ("FUTURES", "OPTION"):
        r.on_path_param("/api/v5/market/tickers", "instType", inst_type, empty)
    # Options require an instFamily, which comes from the underlying endpoint.
    r.on_path("/api/v5/public/underlying", {"code": "0", "msg": "", "data": [["BTC-USD"]]})
    r.on_path("/api/v5/public/instruments", empty)
    r.on_path("/api/v5/public/funding-rate", load_fixture("okx", "funding"))
    r.on_path("/api/v5/public/open-interest", load_fixture("okx", "open_interest"))
    r.on_path("/api/v5/public/mark-price", load_fixture("okx", "mark_price"))
    r.on_path("/api/v5/market/books", load_fixture("okx", "books"))


def _binance(r: RouteRecorder) -> None:
    r.on_path("/api/v3/exchangeInfo", load_fixture("binance", "exchange_info"))
    r.on_path("/api/v3/ticker/24hr", load_fixture("binance", "ticker_24hr"))
    r.on_path("/api/v3/depth", load_fixture("binance", "depth"))
    r.on_path("/api/v3/time", {"serverTime": 1790530158844})


def _coinbase(r: RouteRecorder) -> None:
    r.on_path("/products", load_fixture("coinbase", "products"))
    r.on_path("/products/stats", load_fixture("coinbase", "stats"))
    r.on_path("/products/BTC-USD/book", load_fixture("coinbase", "book"))
    r.on_path("/time", {"iso": "2026-09-27T17:30:00Z", "epoch": 1790530200.0})


def _kraken(r: RouteRecorder) -> None:
    r.on_path("/0/public/AssetPairs", load_fixture("kraken", "asset_pairs"))
    r.on_path("/0/public/Ticker", load_fixture("kraken", "ticker"))
    r.on_path("/0/public/Depth", load_fixture("kraken", "depth"))
    r.on_path("/0/public/Time", {"error": [], "result": {"unixtime": 1790530200, "rfc1123": "x"}})


def _krakenfutures(r: RouteRecorder) -> None:
    r.on_path("/derivatives/api/v3/instruments", load_fixture("krakenfutures", "instruments"))
    r.on_path("/derivatives/api/v3/tickers", load_fixture("krakenfutures", "tickers"))


def _bitget(r: RouteRecorder) -> None:
    empty = {"code": "00000", "msg": "success", "data": []}
    r.on_path("/api/v2/spot/public/symbols", load_fixture("bitget", "spot_symbols"))
    r.on_path("/api/v2/spot/market/tickers", load_fixture("bitget", "spot_tickers"))
    r.on_path_param(
        "/api/v2/mix/market/contracts",
        "productType",
        "USDT-FUTURES",
        load_fixture("bitget", "mix_contracts_usdt"),
    )
    r.on_path_param(
        "/api/v2/mix/market/tickers",
        "productType",
        "USDT-FUTURES",
        load_fixture("bitget", "mix_tickers_usdt"),
    )
    for product in ("USDC-FUTURES", "COIN-FUTURES"):
        r.on_path_param("/api/v2/mix/market/contracts", "productType", product, empty)
        r.on_path_param("/api/v2/mix/market/tickers", "productType", product, empty)
    r.on_path("/api/v2/public/time", {"code": "00000", "data": {"serverTime": "1790530200000"}})


def _gateio(r: RouteRecorder) -> None:
    r.on_path("/api/v4/spot/currency_pairs", load_fixture("gateio", "currency_pairs"))
    r.on_path("/api/v4/spot/tickers", load_fixture("gateio", "spot_tickers"))
    r.on_path("/api/v4/futures/usdt/contracts", load_fixture("gateio", "futures_contracts"))
    r.on_path("/api/v4/futures/usdt/tickers", load_fixture("gateio", "futures_tickers"))
    r.on_path("/api/v4/futures/btc/contracts", [])
    r.on_path("/api/v4/futures/btc/tickers", [])
    r.on_path("/api/v4/spot/time", {"server_time": 1790530200000})


def _kucoin(r: RouteRecorder) -> None:
    r.on_path("/api/v2/symbols", load_fixture("kucoin", "symbols"))
    r.on_path("/api/v1/market/allTickers", load_fixture("kucoin", "all_tickers"))
    r.on_path("/api/v1/timestamp", {"code": "200000", "data": 1790530200000})


def _mexc(r: RouteRecorder) -> None:
    r.on_path("/api/v3/exchangeInfo", load_fixture("mexc", "exchange_info"))
    r.on_path("/api/v3/ticker/24hr", load_fixture("mexc", "ticker_24hr"))
    r.on_path("/api/v3/time", {"serverTime": 1790530200000})


def _htx(r: RouteRecorder) -> None:
    r.on_path("/v2/settings/common/symbols", load_fixture("htx", "symbols"))
    r.on_path("/market/tickers", load_fixture("htx", "tickers"))
    r.on_path("/v1/common/timestamp", {"status": "ok", "data": 1790530200000})


def _cryptocom(r: RouteRecorder) -> None:
    r.on_path("/exchange/v1/public/get-instruments", load_fixture("cryptocom", "instruments"))
    r.on_path("/exchange/v1/public/get-tickers", load_fixture("cryptocom", "tickers"))


def _deribit(r: RouteRecorder) -> None:
    empty = {"jsonrpc": "2.0", "result": []}
    # Discovery uses currency=any and returns the whole catalogue in one request. The
    # fixture is the BTC slice of it, which is enough to exercise every instrument kind.
    r.on_path_param(
        "/api/v2/public/get_instruments",
        "currency",
        "any",
        load_fixture("deribit", "instruments_btc"),
    )
    r.on_path_param(
        "/api/v2/public/get_book_summary_by_currency",
        "currency",
        "BTC",
        load_fixture("deribit", "book_summary_btc"),
    )
    # Currencies discovery found beyond BTC (settlement currencies such as USDC)
    # answer empty, so the per-currency quote loop still completes.
    r.on_path("/api/v2/public/get_book_summary_by_currency", empty)
    r.on_path("/api/v2/public/ticker", load_fixture("deribit", "ticker_perpetual"))
    r.on_path("/api/v2/public/get_time", {"jsonrpc": "2.0", "result": 1790530200000})


def _hyperliquid(r: RouteRecorder) -> None:
    r.on_path("/info", load_fixture("hyperliquid", "meta_and_asset_ctxs"))


def _dydx(r: RouteRecorder) -> None:
    r.on_path("/v4/perpetualMarkets", load_fixture("dydx", "perpetual_markets"))


def _bitstamp(r: RouteRecorder) -> None:
    r.on_path("/api/v2/trading-pairs-info/", load_fixture("bitstamp", "trading_pairs_info"))
    r.on_path("/api/v2/ticker/", load_fixture("bitstamp", "tickers"))


def _bitfinex(r: RouteRecorder) -> None:
    r.on_path(
        "/v2/conf/pub:map:currency:label,pub:map:currency:sym",
        load_fixture("bitfinex", "conf_currency_maps"),
    )
    r.on_path("/v2/conf/pub:list:pair:exchange", load_fixture("bitfinex", "conf_pairs"))
    r.on_path("/v2/tickers", load_fixture("bitfinex", "tickers"))


#: Venue slug to the function that wires its fixtures.
WIRING: dict[str, Callable[[RouteRecorder], None]] = {
    "binance": _binance,
    "bitfinex": _bitfinex,
    "bitget": _bitget,
    "bitstamp": _bitstamp,
    "coinbase": _coinbase,
    "cryptocom": _cryptocom,
    "deribit": _deribit,
    "dydx": _dydx,
    "gateio": _gateio,
    "htx": _htx,
    "hyperliquid": _hyperliquid,
    "kraken": _kraken,
    "krakenfutures": _krakenfutures,
    "kucoin": _kucoin,
    "mexc": _mexc,
    "okx": _okx,
}

#: A symbol known to be present in each venue's fixture set, for order-book tests.
BOOK_SYMBOLS: dict[str, str] = {
    "binance": "BTCUSDT",
    "coinbase": "BTC-USD",
    "kraken": "XBTUSD",
    "okx": "BTC-USDT",
}
