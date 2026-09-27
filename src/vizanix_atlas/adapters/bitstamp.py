"""Bitstamp adapter.

Official documentation: https://www.bitstamp.net/api/

Verified 2026-09-27. Bitstamp is a USD-native venue, which makes it one of the few
places Atlas can observe a stablecoin-to-USD rate directly rather than deriving it
through an intermediary.

``/api/v2/ticker/`` returns every pair in one response, with a per-pair timestamp, a
24-hour VWAP and top of book. Only base ``volume`` is published, so quote volume
stays null rather than being computed from the VWAP.
"""

from __future__ import annotations

from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.atlas_time import plausible_epoch_ms
from vizanix_atlas.core.errors import SchemaMismatch
from vizanix_atlas.core.numeric import parse_float, parse_non_negative, parse_positive
from vizanix_atlas.models.observations import (
    RawFxObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)

#: Directly observable stablecoin-to-USD markets.
_FX_PAIRS: Final = (
    ("USDT/USD", "USDT", "USD"),
    ("USDC/USD", "USDC", "USD"),
    ("DAI/USD", "DAI", "USD"),
)


class BitstampAdapter(ExchangeAdapter):
    """Collects spot markets from Bitstamp."""

    slug = "bitstamp"
    base_url = "https://www.bitstamp.net"

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate every enabled trading pair."""
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/api/v2/trading-pairs-info/", operation="trading-pairs-info"
        )
        rows = self.require_list(payload, path="/api/v2/trading-pairs-info/")

        instruments: list[RawInstrument] = []
        for row in rows:
            if not isinstance(row, dict):
                raise SchemaMismatch("trading pair row was not an object", venue=self.slug)
            name = row.get("name")
            url_symbol = row.get("url_symbol")
            if not (isinstance(name, str) and "/" in name and isinstance(url_symbol, str)):
                raise SchemaMismatch(
                    "trading pair row lacked name or url_symbol", venue=self.slug
                )
            if str(row.get("trading", "")) != "Enabled":
                continue

            base, _, quote = name.partition("/")
            instruments.append(
                RawInstrument(
                    venue_slug=self.slug,
                    # url_symbol is the key the API's own per-pair endpoints take, so
                    # it is the native symbol; `name` is the display form.
                    symbol_native=url_symbol,
                    instrument_class="spot",
                    instrument_type="spot",
                    base_symbol_native=base,
                    quote_symbol_native=quote,
                    # `description` is the project name, useful resolution evidence.
                    base_name=str(row.get("description") or "") or None,
                    tick_size=_places_to_step(row.get("counter_decimals")),
                    quantity_step=_places_to_step(row.get("base_decimals")),
                    active=True,
                )
            )
        return instruments

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch every ticker in one request."""
        tickers: list[RawTicker] = []
        for row, symbol, event_time in await self._ticker_rows():
            tickers.append(
                RawTicker(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    timing=self.timing(exchange_event_time=event_time),
                    last_price=parse_positive(row.get("last")),
                    bid_price=parse_positive(row.get("bid")),
                    ask_price=parse_positive(row.get("ask")),
                    open_24h=parse_positive(row.get("open_24")),
                    high_24h=parse_positive(row.get("high")),
                    low_24h=parse_positive(row.get("low")),
                    base_volume_24h=parse_non_negative(row.get("volume")),
                    # Bitstamp publishes a VWAP but no quote volume; multiplying them
                    # would fabricate a figure the venue never reported.
                    quote_volume_24h=None,
                )
            )
        return tickers

    async def _ticker_rows(self) -> list[tuple[dict[str, Any], str, int | None]]:
        """Fetch the bulk ticker array, normalising pair names to url symbols."""
        self.refresh_receive_time()
        payload = await self.client.get_json("/api/v2/ticker/", operation="ticker")
        rows = self.require_list(payload, path="/api/v2/ticker/")
        out: list[tuple[dict[str, Any], str, int | None]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            pair = row.get("pair")
            if not isinstance(pair, str):
                continue
            if str(row.get("market_type", "SPOT")) != "SPOT":
                self.note_unknown_enum("market_type", str(row.get("market_type")))
                continue
            # The ticker reports "BTC/USD"; discovery reports "btcusd". Normalising to
            # the url symbol keeps the two joinable.
            symbol = pair.replace("/", "").lower()
            timestamp = parse_float(row.get("timestamp"))
            out.append(
                (row, symbol, plausible_epoch_ms(timestamp * 1000) if timestamp else None)
            )
        return out

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one order-book snapshot."""
        self.refresh_receive_time()
        payload = await self.client.get_json(
            f"/api/v2/order_book/{symbol_native}/", operation="order-book"
        )
        body = self.require_mapping(payload, path="/api/v2/order_book/")
        bids, bids_truncated = self.build_levels(
            self.require_list(body, path="order_book", key="bids"), depth=depth, descending=True
        )
        asks, asks_truncated = self.build_levels(
            self.require_list(body, path="order_book", key="asks"), depth=depth, descending=False
        )
        timestamp = parse_float(body.get("timestamp"))
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            timing=self.timing(
                exchange_event_time=plausible_epoch_ms(timestamp * 1000) if timestamp else None
            ),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated,
        )

    async def fetch_fx_observations(self) -> list[RawFxObservation]:
        """Observe stablecoin-to-USD rates directly from Bitstamp's USD markets."""
        rows = {
            str(row.get("pair")): (row, symbol, event_time)
            for row, symbol, event_time in await self._ticker_rows()
        }
        observations: list[RawFxObservation] = []
        for pair, from_code, to_code in _FX_PAIRS:
            entry = rows.get(pair)
            if entry is None:
                continue
            row, symbol, event_time = entry
            ticker = RawTicker(
                venue_slug=self.slug,
                symbol_native=symbol,
                timing=self.timing(exchange_event_time=event_time),
                last_price=parse_positive(row.get("last")),
                bid_price=parse_positive(row.get("bid")),
                ask_price=parse_positive(row.get("ask")),
            )
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


def _places_to_step(value: Any) -> float | None:
    """Convert a decimal-places count into a step size."""
    places = parse_float(value)
    if places is None or places < 0 or places > 18 or not float(places).is_integer():
        return None
    return 10.0 ** -int(places)
