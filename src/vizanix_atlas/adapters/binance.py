"""Binance spot adapter.

Official documentation: https://developers.binance.com/docs/binance-spot-api-docs

Atlas uses ``data-api.binance.vision``, Binance's official market-data-only host.
The trading hosts (``api.binance.com`` and its regional aliases) returned HTTP 451
from the network used to build this release, while the market-data host did not.

Spot only. Binance USD-M and COIN-M futures live on ``fapi.binance.com`` and
``dapi.binance.com``, which returned 451 with no market-data mirror available, so no
fixture could be captured and no adapter was written. That gap is recorded in
``config/exchanges.yaml`` and ``docs/LIMITATIONS.md`` rather than papered over.

Verified 2026-09-27: ``/api/v3/ticker/24hr`` already carries best bid and ask, so
the separate ``bookTicker`` request most clients make is unnecessary.
"""

from __future__ import annotations

from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.atlas_time import plausible_epoch_ms
from vizanix_atlas.core.errors import SchemaMismatch
from vizanix_atlas.core.numeric import parse_float, parse_non_negative, parse_int, parse_positive
from vizanix_atlas.models.enums import PriceSource
from vizanix_atlas.models.observations import (
    RawFxObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)

#: Rates worth observing for the quote-conversion graph. Binance lists no fiat USD
#: markets, so USDT/USD is not directly observable here; USDC/USDT is.
_FX_PAIRS: Final = (
    ("USDCUSDT", "USDC", "USDT"),
    ("FDUSDUSDT", "FDUSD", "USDT"),
    ("TUSDUSDT", "TUSD", "USDT"),
    ("DAIUSDT", "DAI", "USDT"),
)

#: Depth levels the public endpoint accepts.
_ALLOWED_DEPTHS: Final = (5, 10, 20, 50, 100, 500, 1000, 5000)


class BinanceSpotAdapter(ExchangeAdapter):
    """Collects spot markets from Binance's public market-data host."""

    slug = "binance"
    base_url = "https://data-api.binance.vision"

    def _filter_value(self, filters: Any, filter_type: str, field: str) -> float | None:
        """Read one value out of Binance's symbol ``filters`` array.

        Binance expresses tick size, step size and minimum notional as entries in a
        list of filter objects rather than as fields, so they have to be looked up
        by type.
        """
        if not isinstance(filters, list):
            return None
        for entry in filters:
            if isinstance(entry, dict) and entry.get("filterType") == filter_type:
                return parse_positive(entry.get(field))
        return None

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate every trading spot symbol from ``/api/v3/exchangeInfo``.

        The response is large (tens of megabytes), which is why discovery runs on its
        own less frequent schedule rather than on every snapshot.
        """
        self.refresh_receive_time()
        payload = await self.client.get_json("/api/v3/exchangeInfo", operation="exchange-info")
        body = self.require_mapping(payload, path="/api/v3/exchangeInfo")
        rows = self.require_list(body, path="/api/v3/exchangeInfo", key="symbols")

        instruments: list[RawInstrument] = []
        for row in rows:
            if not isinstance(row, dict):
                raise SchemaMismatch("symbol row was not an object", venue=self.slug)
            symbol = row.get("symbol")
            base = row.get("baseAsset")
            quote = row.get("quoteAsset")
            if not (isinstance(symbol, str) and isinstance(base, str) and isinstance(quote, str)):
                raise SchemaMismatch("symbol row lacked symbol, baseAsset or quoteAsset",
                                     venue=self.slug)

            status = str(row.get("status", ""))
            if status != "TRADING":
                # BREAK, HALT, PRE_TRADING and END_OF_DAY are all real states, and
                # none of them describe a market Atlas should quote.
                continue
            if not row.get("isSpotTradingAllowed", True):
                continue

            filters = row.get("filters")
            instruments.append(
                RawInstrument(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    instrument_class="spot",
                    instrument_type="spot",
                    base_symbol_native=base,
                    quote_symbol_native=quote,
                    tick_size=self._filter_value(filters, "PRICE_FILTER", "tickSize"),
                    quantity_step=self._filter_value(filters, "LOT_SIZE", "stepSize"),
                    minimum_quantity=self._filter_value(filters, "LOT_SIZE", "minQty"),
                    minimum_notional=self._filter_value(
                        filters, "NOTIONAL", "minNotional"
                    ),
                    active=True,
                )
            )
        return instruments

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch all 24-hour tickers in one request.

        The bulk response carries bid and ask, so top-of-book spread is available at
        tier A for every Binance spot market.
        """
        self.refresh_receive_time()
        payload = await self.client.get_json("/api/v3/ticker/24hr", operation="ticker-24hr")
        rows = self.require_list(payload, path="/api/v3/ticker/24hr")

        tickers: list[RawTicker] = []
        for row in rows:
            if not isinstance(row, dict):
                raise SchemaMismatch("ticker row was not an object", venue=self.slug)
            symbol = row.get("symbol")
            if not isinstance(symbol, str):
                continue
            tickers.append(
                RawTicker(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    # closeTime is the end of the rolling window the figures describe,
                    # which is the closest thing Binance publishes to an event time.
                    timing=self.timing(
                        exchange_event_time=plausible_epoch_ms(parse_float(row.get("closeTime")))
                    ),
                    last_price=parse_positive(row.get("lastPrice")),
                    bid_price=parse_positive(row.get("bidPrice")),
                    ask_price=parse_positive(row.get("askPrice")),
                    bid_size=parse_non_negative(row.get("bidQty")),
                    ask_size=parse_non_negative(row.get("askQty")),
                    open_24h=parse_positive(row.get("openPrice")),
                    high_24h=parse_positive(row.get("highPrice")),
                    low_24h=parse_positive(row.get("lowPrice")),
                    base_volume_24h=parse_non_negative(row.get("volume")),
                    quote_volume_24h=parse_non_negative(row.get("quoteVolume")),
                    trade_count_24h=parse_int(row.get("count")),
                )
            )
        return tickers

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one order-book snapshot.

        Binance only accepts a fixed set of depth values, so the request is rounded
        up to the next allowed one rather than rejected.
        """
        allowed = next((d for d in _ALLOWED_DEPTHS if d >= depth), _ALLOWED_DEPTHS[-1])
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/api/v3/depth",
            params={"symbol": symbol_native, "limit": allowed},
            operation="depth",
        )
        body = self.require_mapping(payload, path="/api/v3/depth")
        bids, bids_truncated = self.build_levels(
            self.require_list(body, path="/api/v3/depth", key="bids"), depth=depth, descending=True
        )
        asks, asks_truncated = self.build_levels(
            self.require_list(body, path="/api/v3/depth", key="asks"), depth=depth, descending=False
        )
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            # The depth response carries no timestamp, only lastUpdateId.
            timing=self.timing(),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated,
        )

    async def fetch_fx_observations(self) -> list[RawFxObservation]:
        """Observe stablecoin rates from Binance's own spot markets."""
        self.refresh_receive_time()
        payload = await self.client.get_json("/api/v3/ticker/24hr", operation="ticker-24hr:fx")
        rows = {
            row["symbol"]: row
            for row in self.require_list(payload, path="/api/v3/ticker/24hr")
            if isinstance(row, dict) and isinstance(row.get("symbol"), str)
        }
        observations: list[RawFxObservation] = []
        for symbol, from_code, to_code in _FX_PAIRS:
            row = rows.get(symbol)
            if row is None:
                continue
            bid = parse_positive(row.get("bidPrice"))
            ask = parse_positive(row.get("askPrice"))
            if bid is not None and ask is not None and bid <= ask:
                rate, source = (bid + ask) / 2.0, PriceSource.MID
            else:
                candidate = parse_positive(row.get("lastPrice"))
                if candidate is None:
                    continue
                rate, source = candidate, PriceSource.LAST
            observations.append(
                RawFxObservation(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    timing=self.timing(
                        exchange_event_time=plausible_epoch_ms(parse_float(row.get("closeTime")))
                    ),
                    from_symbol_native=from_code,
                    to_symbol_native=to_code,
                    rate=rate,
                    source_price=source,
                )
            )
        return observations

    async def server_time_ms(self) -> int | None:
        """Fetch Binance's server clock for skew measurement."""
        payload = await self.client.get_json("/api/v3/time", operation="time")
        body = self.require_mapping(payload, path="/api/v3/time")
        return plausible_epoch_ms(parse_float(body.get("serverTime")))
