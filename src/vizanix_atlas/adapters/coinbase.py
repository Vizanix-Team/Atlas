"""Coinbase Exchange adapter.

Official documentation: https://docs.cdp.coinbase.com/exchange/docs/welcome

Verified 2026-09-27. Two capability limits shape this adapter and are declared
rather than worked around:

No top of book in bulk
    ``/products/stats`` returns ``open``, ``high``, ``low``, ``last`` and base
    ``volume`` only. There is no bid, no ask and no timestamp, so spread on Coinbase
    is a tier B measurement requiring an order-book request.

No quote volume
    Only base volume is published. Atlas leaves ``quote_volume_24h`` null rather than
    multiplying base volume by the last price, which would invent a figure Coinbase
    never reported and would silently differ from how every other venue computes it.

Coinbase is a USD-native venue, which makes it one of the few places Atlas can
observe stablecoin-to-USD rates directly instead of deriving them.
"""

from __future__ import annotations

from typing import Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.atlas_time import parse_iso, to_epoch_ms
from vizanix_atlas.core.errors import SchemaMismatch
from vizanix_atlas.core.numeric import parse_non_negative, parse_positive
from vizanix_atlas.models.enums import PriceSource
from vizanix_atlas.models.observations import (
    RawFxObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)

#: Directly observable stablecoin-to-USD markets. These are the strongest edges in
#: the conversion graph because no intermediary is involved.
_FX_PAIRS: Final = (
    ("USDT-USD", "USDT", "USD"),
    ("USDC-USD", "USDC", "USD"),
    ("DAI-USD", "DAI", "USD"),
    ("PYUSD-USD", "PYUSD", "USD"),
)


class CoinbaseAdapter(ExchangeAdapter):
    """Collects spot markets from Coinbase Exchange."""

    slug = "coinbase"
    base_url = "https://api.exchange.coinbase.com"

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate every online product from ``/products``."""
        self.refresh_receive_time()
        payload = await self.client.get_json("/products", operation="products")
        rows = self.require_list(payload, path="/products")

        instruments: list[RawInstrument] = []
        for row in rows:
            if not isinstance(row, dict):
                raise SchemaMismatch("product row was not an object", venue=self.slug)
            product_id = row.get("id")
            base = row.get("base_currency")
            quote = row.get("quote_currency")
            if not (
                isinstance(product_id, str) and isinstance(base, str) and isinstance(quote, str)
            ):
                raise SchemaMismatch(
                    "product row lacked id, base_currency or quote_currency", venue=self.slug
                )

            status = str(row.get("status", ""))
            if status != "online" or row.get("trading_disabled"):
                continue
            # A product in cancel-only or post-only mode cannot be traded normally,
            # so its quote does not describe a market Atlas should aggregate.
            if row.get("cancel_only") or row.get("post_only"):
                continue

            instruments.append(
                RawInstrument(
                    venue_slug=self.slug,
                    symbol_native=product_id,
                    instrument_class="spot",
                    instrument_type="spot",
                    base_symbol_native=base,
                    quote_symbol_native=quote,
                    tick_size=parse_positive(row.get("quote_increment")),
                    quantity_step=parse_positive(row.get("base_increment")),
                    minimum_notional=parse_non_negative(row.get("min_market_funds")),
                    active=True,
                )
            )
        return instruments

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch 24-hour statistics for every product in one request.

        The response is keyed by product ID, and covers only products with recent
        activity, so it is normally smaller than the product catalogue.
        """
        self.refresh_receive_time()
        payload = await self.client.get_json("/products/stats", operation="stats")
        body = self.require_mapping(payload, path="/products/stats")

        tickers: list[RawTicker] = []
        for product_id, entry in sorted(body.items()):
            if not isinstance(entry, dict):
                continue
            stats = entry.get("stats_24hour")
            if not isinstance(stats, dict):
                continue
            tickers.append(
                RawTicker(
                    venue_slug=self.slug,
                    symbol_native=product_id,
                    # No venue timestamp exists on this endpoint, so freshness rests
                    # on the collector's receive time. Declared, not disguised.
                    timing=self.timing(),
                    last_price=parse_positive(stats.get("last")),
                    open_24h=parse_positive(stats.get("open")),
                    high_24h=parse_positive(stats.get("high")),
                    low_24h=parse_positive(stats.get("low")),
                    base_volume_24h=parse_non_negative(stats.get("volume")),
                    # Coinbase publishes no quote volume; deriving one would invent it.
                    quote_volume_24h=None,
                )
            )
        return tickers

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch a level-2 order-book snapshot.

        Level 2 is the aggregated book. Coinbase also offers level 3, which is
        per-order and far larger; Atlas does not need order identity for depth.
        """
        self.refresh_receive_time()
        payload = await self.client.get_json(
            f"/products/{symbol_native}/book",
            params={"level": 2},
            operation="book",
        )
        body = self.require_mapping(payload, path="/products/{id}/book")
        bids, bids_truncated = self.build_levels(
            self.require_list(body, path="book", key="bids"), depth=depth, descending=True
        )
        asks, asks_truncated = self.build_levels(
            self.require_list(body, path="book", key="asks"), depth=depth, descending=False
        )
        event_time = None
        raw_time = body.get("time")
        if isinstance(raw_time, str):
            try:
                event_time = to_epoch_ms(parse_iso(raw_time))
            except ValueError:
                # A timestamp Atlas cannot parse is recorded as absent rather than
                # guessed at, so freshness stays honest.
                self.note_unknown_enum("book.time", raw_time[:32])
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            timing=self.timing(exchange_event_time=event_time),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated,
        )

    async def fetch_fx_observations(self) -> list[RawFxObservation]:
        """Observe stablecoin-to-USD rates directly from Coinbase's USD markets."""
        self.refresh_receive_time()
        payload = await self.client.get_json("/products/stats", operation="stats:fx")
        body = self.require_mapping(payload, path="/products/stats")

        observations: list[RawFxObservation] = []
        for product_id, from_code, to_code in _FX_PAIRS:
            entry = body.get(product_id)
            if not isinstance(entry, dict):
                continue
            stats = entry.get("stats_24hour")
            if not isinstance(stats, dict):
                continue
            rate = parse_positive(stats.get("last"))
            if rate is None:
                continue
            observations.append(
                RawFxObservation(
                    venue_slug=self.slug,
                    symbol_native=product_id,
                    timing=self.timing(),
                    from_symbol_native=from_code,
                    to_symbol_native=to_code,
                    rate=rate,
                    # The bulk stats endpoint carries no bid or ask, so this is a last
                    # price. Recorded as such rather than labelled a mid.
                    source_price=PriceSource.LAST,
                )
            )
        return observations

    async def server_time_ms(self) -> int | None:
        """Fetch Coinbase's server clock for skew measurement."""
        payload = await self.client.get_json("/time", operation="time")
        body = self.require_mapping(payload, path="/time")
        epoch = parse_positive(body.get("epoch"))
        return int(epoch * 1000) if epoch is not None else None
