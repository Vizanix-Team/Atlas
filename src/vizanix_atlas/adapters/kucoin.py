"""KuCoin spot adapter.

Official documentation: https://www.kucoin.com/docs-new/

Verified 2026-09-27. ``/api/v1/market/allTickers`` returns every symbol with best bid
and ask in one request, under a single response-level ``time``. That one timestamp is
recorded as the exchange server time rather than as a per-record event time, because
it describes when the snapshot was generated, not when each market last traded.

Spot only in this release. KuCoin Futures is a separate host and product API; no
fixture was captured for it, so no adapter was written.
"""

from __future__ import annotations

from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.atlas_time import plausible_epoch_ms
from vizanix_atlas.core.errors import ExchangeApplicationError, SchemaMismatch
from vizanix_atlas.core.numeric import parse_float, parse_non_negative, parse_positive
from vizanix_atlas.models.observations import (
    RawFxObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)

_SUCCESS_CODE: Final = "200000"

_FX_PAIRS: Final = (
    ("USDC-USDT", "USDC", "USDT"),
    ("DAI-USDT", "DAI", "USDT"),
    ("USDT-USD", "USDT", "USD"),
)


class KucoinAdapter(ExchangeAdapter):
    """Collects spot markets from KuCoin."""

    slug = "kucoin"
    base_url = "https://api.kucoin.com"

    def _unwrap(self, payload: Any, *, path: str) -> Any:
        """Return the ``data`` value, raising if KuCoin reported an application error.

        Raises:
            ExchangeApplicationError: If ``code`` is not ``"200000"``.

        """
        body = self.require_mapping(payload, path=path)
        code = str(body.get("code", ""))
        if code and code != _SUCCESS_CODE:
            raise ExchangeApplicationError(
                "KuCoin reported an application error",
                venue_code=code,
                venue=self.slug,
                path=path,
                message=str(body.get("msg", ""))[:200],
            )
        if "data" not in body:
            raise SchemaMismatch("response had no data field", venue=self.slug, path=path)
        return body["data"]

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate every tradeable symbol from ``/api/v2/symbols``."""
        self.refresh_receive_time()
        payload = await self.client.get_json("/api/v2/symbols", operation="symbols")
        rows = self.require_list(self._unwrap(payload, path="/api/v2/symbols"), path="symbols")

        instruments: list[RawInstrument] = []
        for row in rows:
            if not isinstance(row, dict):
                raise SchemaMismatch("symbol row was not an object", venue=self.slug)
            symbol = row.get("symbol")
            base = row.get("baseCurrency")
            quote = row.get("quoteCurrency")
            if not (isinstance(symbol, str) and isinstance(base, str) and isinstance(quote, str)):
                raise SchemaMismatch(
                    "symbol row lacked symbol, baseCurrency or quoteCurrency", venue=self.slug
                )
            if not row.get("enableTrading", False):
                continue
            instruments.append(
                RawInstrument(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    instrument_class="spot",
                    instrument_type="spot",
                    base_symbol_native=base,
                    quote_symbol_native=quote,
                    tick_size=parse_positive(row.get("priceIncrement")),
                    quantity_step=parse_positive(row.get("baseIncrement")),
                    minimum_quantity=parse_non_negative(row.get("baseMinSize")),
                    minimum_notional=parse_non_negative(row.get("minFunds")),
                    active=True,
                )
            )
        return instruments

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch every ticker in one request."""
        self.refresh_receive_time()
        payload = await self.client.get_json("/api/v1/market/allTickers", operation="all-tickers")
        data = self.require_mapping(
            self._unwrap(payload, path="/api/v1/market/allTickers"), path="allTickers"
        )
        # One timestamp for the whole snapshot. Recorded as server time, not as a
        # per-market event time, because it is not one.
        server_time = plausible_epoch_ms(parse_float(data.get("time")))
        rows = self.require_list(data, path="allTickers", key="ticker")

        tickers: list[RawTicker] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            if not isinstance(symbol, str):
                continue
            tickers.append(
                RawTicker(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    timing=self.timing(exchange_server_time=server_time),
                    last_price=parse_positive(row.get("last")),
                    bid_price=parse_positive(row.get("buy")),
                    ask_price=parse_positive(row.get("sell")),
                    bid_size=parse_non_negative(row.get("bestBidSize")),
                    ask_size=parse_non_negative(row.get("bestAskSize")),
                    open_24h=parse_positive(row.get("open")),
                    high_24h=parse_positive(row.get("high")),
                    low_24h=parse_positive(row.get("low")),
                    base_volume_24h=parse_non_negative(row.get("vol")),
                    quote_volume_24h=parse_non_negative(row.get("volValue")),
                )
            )
        return tickers

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one order-book snapshot.

        KuCoin's unauthenticated book endpoints return a fixed 20 or 100 levels;
        deeper books require credentials, which the baseline system does not use.
        """
        levels = 20 if depth <= 20 else 100
        self.refresh_receive_time()
        payload = await self.client.get_json(
            f"/api/v1/market/orderbook/level2_{levels}",
            params={"symbol": symbol_native},
            operation="orderbook",
        )
        book = self.require_mapping(
            self._unwrap(payload, path="/api/v1/market/orderbook"), path="orderbook"
        )
        bids, bids_truncated = self.build_levels(
            self.require_list(book, path="orderbook", key="bids"), depth=depth, descending=True
        )
        asks, asks_truncated = self.build_levels(
            self.require_list(book, path="orderbook", key="asks"), depth=depth, descending=False
        )
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            timing=self.timing(
                exchange_event_time=plausible_epoch_ms(parse_float(book.get("time")))
            ),
            bids=bids,
            asks=asks,
            # The venue caps this response, so deeper liquidity certainly exists.
            truncated=bids_truncated or asks_truncated or depth > levels,
        )

    async def fetch_fx_observations(self) -> list[RawFxObservation]:
        """Observe stablecoin rates from KuCoin's own spot markets."""
        tickers = {t.symbol_native: t for t in await self.fetch_tickers()}
        observations: list[RawFxObservation] = []
        for symbol, from_code, to_code in _FX_PAIRS:
            ticker = tickers.get(symbol)
            if ticker is None:
                continue
            candidate = ticker.reference_candidate
            if candidate is None:
                continue
            rate, source = candidate
            observations.append(
                RawFxObservation(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    timing=ticker.timing,
                    from_symbol_native=from_code,
                    to_symbol_native=to_code,
                    rate=rate,
                    source_price=source,
                )
            )
        return observations

    async def server_time_ms(self) -> int | None:
        """Fetch KuCoin's server clock for skew measurement."""
        payload = await self.client.get_json("/api/v1/timestamp", operation="time")
        return plausible_epoch_ms(parse_float(self._unwrap(payload, path="/api/v1/timestamp")))
