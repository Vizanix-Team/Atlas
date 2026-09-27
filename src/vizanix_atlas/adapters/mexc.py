"""MEXC spot adapter.

Official documentation: https://mexcdevelop.github.io/apidocs/spot_v3_en/

Verified 2026-09-27. MEXC is the most useful venue in Atlas for identity resolution:
1,224 of 1,898 listed symbols carried a ``contractAddress``, either an EVM address or
a base58 Solana mint, and almost all carried a ``fullName``. A contract address is
the strongest evidence any supported venue publishes, and it is what lets Atlas
resolve a colliding ticker instead of refusing to.

Not every value in that field is an address: ``"xtokens"``, ``"BNB"`` and other
placeholders appear. The adapter passes the raw string through and the resolver
validates it, so a placeholder becomes "no evidence" rather than a bad identity.

Spot only. MEXC's contract API is a separate host and was not captured.
"""

from __future__ import annotations

from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.atlas_time import plausible_epoch_ms
from vizanix_atlas.core.errors import SchemaMismatch
from vizanix_atlas.core.numeric import parse_float, parse_int, parse_non_negative, parse_positive
from vizanix_atlas.models.observations import (
    RawFxObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)

#: MEXC reports symbol status as a string code; "1" is enabled.
_STATUS_ENABLED: Final = "1"

_FX_PAIRS: Final = (
    ("USDCUSDT", "USDC", "USDT"),
    ("DAIUSDT", "DAI", "USDT"),
)


class MexcAdapter(ExchangeAdapter):
    """Collects spot markets from MEXC."""

    slug = "mexc"
    base_url = "https://api.mexc.com"

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate every enabled spot symbol from ``/api/v3/exchangeInfo``.

        Carries the contract address and full name through as resolution evidence.
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
                raise SchemaMismatch(
                    "symbol row lacked symbol, baseAsset or quoteAsset", venue=self.slug
                )
            if str(row.get("status", "")) != _STATUS_ENABLED:
                continue
            if not row.get("isSpotTradingAllowed", True):
                continue

            instruments.append(
                RawInstrument(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    instrument_class="spot",
                    instrument_type="spot",
                    base_symbol_native=base,
                    quote_symbol_native=quote,
                    base_name=str(row.get("fullName") or "") or None,
                    # Passed through raw. The resolver decides whether it is an
                    # address; placeholders such as "xtokens" are rejected there.
                    base_contract_address=str(row.get("contractAddress") or "") or None,
                    tick_size=_places_to_step(row.get("quotePrecision")),
                    quantity_step=parse_positive(row.get("baseSizePrecision")),
                    minimum_notional=parse_non_negative(row.get("quoteAmountPrecision")),
                    active=True,
                )
            )
        return instruments

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch all 24-hour tickers in one request."""
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
        """Fetch one order-book snapshot."""
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/api/v3/depth",
            params={"symbol": symbol_native, "limit": min(depth, 5000)},
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
            timing=self.timing(
                exchange_server_time=plausible_epoch_ms(parse_float(body.get("timestamp")))
            ),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated,
        )

    async def fetch_fx_observations(self) -> list[RawFxObservation]:
        """Observe stablecoin rates from MEXC's own spot markets."""
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
        """Fetch MEXC's server clock for skew measurement."""
        payload = await self.client.get_json("/api/v3/time", operation="time")
        body = self.require_mapping(payload, path="/api/v3/time")
        return plausible_epoch_ms(parse_float(body.get("serverTime")))


def _places_to_step(value: Any) -> float | None:
    """Convert a decimal-places count into a step size."""
    places = parse_float(value)
    if places is None or places < 0 or places > 18 or not float(places).is_integer():
        return None
    return 10.0 ** -int(places)
