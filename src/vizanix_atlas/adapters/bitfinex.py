"""Bitfinex adapter.

Official documentation: https://docs.bitfinex.com/docs/rest-public

Verified 2026-09-27. Bitfinex uses venue-specific three-letter contractions that
collide with other venues' tickers, and the most dangerous of them is ``UST``, which
on Bitfinex means Tether and elsewhere has meant TerraUSD. Merging on the ticker
would combine two unrelated assets.

Atlas does not hand-write that mapping. Bitfinex publishes its own currency maps, and
the adapter reads them:

``pub:map:currency:sym``
    Venue code to the widely used symbol: ``ALG`` to ``ALGO``, ``ATO`` to ``ATOM``,
    ``DSH`` to ``DASH``.
``pub:map:currency:label``
    Venue code to the project name: ``UST`` to ``Tether USDt``.

Both are carried into every instrument as resolution evidence, so identity rests on
what the venue states rather than on a guess. ``config/asset_overrides.yaml``
additionally lists ``UST`` as never-merge-on-symbol.

Tickers are positional arrays. Only the eleven documented elements are read; a
twelfth undocumented element was present in the captured response and is ignored.
"""

from __future__ import annotations

from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.errors import SchemaMismatch
from vizanix_atlas.core.numeric import parse_non_negative, parse_positive
from vizanix_atlas.models.observations import (
    RawFxObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)

#: Documented positions in the trading-pair ticker array.
_SYMBOL: Final = 0
_BID: Final = 1
_BID_SIZE: Final = 2
_ASK: Final = 3
_ASK_SIZE: Final = 4
_LAST_PRICE: Final = 7
_VOLUME: Final = 8
_HIGH: Final = 9
_LOW: Final = 10
_MIN_TICKER_FIELDS: Final = 11

_FX_PAIRS: Final = (
    ("tUSTUSD", "UST", "USD"),
    ("tUDCUSD", "UDC", "USD"),
)


class BitfinexAdapter(ExchangeAdapter):
    """Collects spot markets from Bitfinex."""

    slug = "bitfinex"
    base_url = "https://api-pub.bitfinex.com"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: Venue code to widely used symbol, from the venue's own published map.
        self._symbol_map: dict[str, str] = {}
        #: Venue code to project name, from the venue's own published map.
        self._label_map: dict[str, str] = {}

    async def _load_currency_maps(self) -> None:
        """Load the venue's own currency symbol and label maps.

        This is what makes Bitfinex safe to resolve. The maps are the venue stating
        what its contractions mean, so Atlas is reading evidence rather than guessing.
        """
        if self._symbol_map or self._label_map:
            return
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/v2/conf/pub:map:currency:label,pub:map:currency:sym",
            operation="currency-maps",
        )
        outer = self.require_list(payload, path="/v2/conf")
        if len(outer) < 2:
            raise SchemaMismatch(
                "currency map response did not contain both maps",
                venue=self.slug,
                length=len(outer),
            )
        self._label_map = _pairs_to_dict(outer[0])
        self._symbol_map = _pairs_to_dict(outer[1])

    def _resolve_code(self, code: str) -> tuple[str, str | None]:
        """Return the venue code's widely used symbol and project name.

        Falls back to the code itself when the venue publishes no mapping for it,
        which leaves the resolver to treat it as weak evidence rather than silently
        assuming the contraction is a standard ticker.
        """
        return self._symbol_map.get(code, code), self._label_map.get(code)

    @staticmethod
    def _split_pair(raw: str) -> tuple[str, str] | None:
        """Split a Bitfinex pair code into base and quote parts.

        Bitfinex uses two forms: three-plus-three with no separator (``BTCUSD``), and
        colon-separated when either side is longer than three characters
        (``AAVE:USD``). Anything else is not a pair Atlas can split safely, so it is
        skipped rather than guessed at.
        """
        if ":" in raw:
            base, _, quote = raw.partition(":")
            return (base, quote) if base and quote else None
        if len(raw) == 6:
            return raw[:3], raw[3:]
        return None

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate every exchange-traded pair, carrying venue evidence through."""
        await self._load_currency_maps()
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/v2/conf/pub:list:pair:exchange", operation="pair-list"
        )
        outer = self.require_list(payload, path="/v2/conf/pub:list:pair:exchange")
        if not outer:
            raise SchemaMismatch("pair list response was empty", venue=self.slug)
        pairs = self.require_list(outer[0], path="pair-list")

        instruments: list[RawInstrument] = []
        for raw in pairs:
            if not isinstance(raw, str):
                continue
            split = self._split_pair(raw)
            if split is None:
                self.note_unknown_enum("pair_shape", raw[:16])
                continue
            base_code, quote_code = split
            base_symbol, base_name = self._resolve_code(base_code)
            quote_symbol, _ = self._resolve_code(quote_code)
            instruments.append(
                RawInstrument(
                    venue_slug=self.slug,
                    # The ticker endpoint prefixes trading pairs with "t".
                    symbol_native=f"t{raw}",
                    instrument_class="spot",
                    instrument_type="spot",
                    base_symbol_native=base_symbol,
                    quote_symbol_native=quote_symbol,
                    base_name=base_name,
                    active=True,
                )
            )
        return instruments

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch every ticker in one request."""
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/v2/tickers", params={"symbols": "ALL"}, operation="tickers"
        )
        rows = self.require_list(payload, path="/v2/tickers")

        tickers: list[RawTicker] = []
        for row in rows:
            parsed = self._parse_ticker(row)
            if parsed is not None:
                tickers.append(parsed)
        return tickers

    def _parse_ticker(self, row: Any) -> RawTicker | None:
        """Turn one positional ticker array into a :class:`RawTicker`.

        Only trading pairs (``t`` prefix) are collected; funding tickers (``f``
        prefix) describe Bitfinex's margin funding market, which is not a spot market.
        """
        if not isinstance(row, list | tuple) or len(row) < _MIN_TICKER_FIELDS:
            return None
        symbol = row[_SYMBOL]
        if not isinstance(symbol, str) or not symbol.startswith("t"):
            return None
        return RawTicker(
            venue_slug=self.slug,
            symbol_native=symbol,
            # Bitfinex publishes no timestamp on the bulk ticker endpoint.
            timing=self.timing(),
            last_price=parse_positive(row[_LAST_PRICE]),
            bid_price=parse_positive(row[_BID]),
            ask_price=parse_positive(row[_ASK]),
            bid_size=parse_non_negative(row[_BID_SIZE]),
            ask_size=parse_non_negative(row[_ASK_SIZE]),
            high_24h=parse_positive(row[_HIGH]),
            low_24h=parse_positive(row[_LOW]),
            base_volume_24h=parse_non_negative(row[_VOLUME]),
            # Bitfinex publishes only base volume.
            quote_volume_24h=None,
        )

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one order-book snapshot.

        The P0 precision book returns rows of ``[price, count, amount]`` where a
        positive amount is a bid and a negative one an ask, so the two sides are
        separated by sign and the magnitude is taken as the size.
        """
        self.refresh_receive_time()
        payload = await self.client.get_json(
            f"/v2/book/{symbol_native}/P0",
            params={"len": 100 if depth > 25 else 25},
            operation="book",
        )
        rows = self.require_list(payload, path="/v2/book")

        bid_rows: list[list[float]] = []
        ask_rows: list[list[float]] = []
        for row in rows:
            if not isinstance(row, list | tuple) or len(row) < 3:
                continue
            price = parse_positive(row[0])
            amount = row[2]
            if price is None or not isinstance(amount, int | float):
                continue
            if amount > 0:
                bid_rows.append([price, float(amount)])
            elif amount < 0:
                ask_rows.append([price, -float(amount)])

        bids, bids_truncated = self.build_levels(bid_rows, depth=depth, descending=True)
        asks, asks_truncated = self.build_levels(ask_rows, depth=depth, descending=False)
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            timing=self.timing(),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated,
        )

    async def fetch_fx_observations(self) -> list[RawFxObservation]:
        """Observe stablecoin-to-USD rates from Bitfinex's USD markets.

        Note that ``UST`` here is Tether, per the venue's own currency label map.
        """
        await self._load_currency_maps()
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
            resolved_from, _ = self._resolve_code(from_code)
            observations.append(
                RawFxObservation(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    timing=ticker.timing,
                    # The venue's own map turns UST into USDT and UDC into UDC's
                    # published symbol, so the conversion graph gets a real identity.
                    from_symbol_native=resolved_from,
                    to_symbol_native=to_code,
                    rate=rate,
                    source_price=source,
                )
            )
        return observations


def _pairs_to_dict(raw: Any) -> dict[str, str]:
    """Convert Bitfinex's ``[[key, value], ...]`` configuration array into a dict."""
    if not isinstance(raw, list):
        return {}
    return {
        entry[0]: entry[1]
        for entry in raw
        if isinstance(entry, list | tuple)
        and len(entry) >= 2
        and isinstance(entry[0], str)
        and isinstance(entry[1], str)
    }
