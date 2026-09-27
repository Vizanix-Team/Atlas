"""Kraken spot adapter.

Official documentation: https://docs.kraken.com/api/docs/rest-api/get-ticker-information

Verified 2026-09-27. Kraken's asset vocabulary is the most idiosyncratic of any
supported venue, and this adapter handles it explicitly rather than by pattern
matching:

Legacy asset codes
    Kraken prefixes some assets with ``X`` and fiat with ``Z``: bitcoin against the
    dollar is the pair ``XXBTZUSD`` with ``base`` ``XXBT`` and ``quote`` ``ZUSD``.
    The ``wsname`` field carries the unambiguous form (``XBT/USD``), so it is the
    primary source and the prefixed codes are the fallback.

XBT rather than BTC
    Kraken follows the ISO 4217 convention for a non-national currency. Nothing in
    the catalogue states that XBT is bitcoin, so the mapping is asserted in
    ``config/asset_overrides.yaml`` with a citation rather than guessed here.

No timestamp
    ``/0/public/Ticker`` carries no time field at all, so freshness rests on the
    collector's receive time. Declared in the registry, not disguised.

No quote volume
    The response gives 24-hour base volume and a 24-hour VWAP but no quote volume.
    Atlas records both and leaves quote volume null rather than multiplying them,
    which would produce a figure Kraken never published.
"""

from __future__ import annotations

from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.errors import ExchangeApplicationError, SchemaMismatch
from vizanix_atlas.core.numeric import parse_int, parse_non_negative, parse_positive
from vizanix_atlas.models.enums import PriceSource
from vizanix_atlas.models.observations import (
    RawFxObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)

#: Directly observable stablecoin-to-USD markets on Kraken.
_FX_PAIRS: Final = (
    ("USDTZUSD", "USDT", "USD"),
    ("USDCUSD", "USDC", "USD"),
    ("DAIUSD", "DAI", "USD"),
)

#: Ticker array positions, from the official field documentation. Named so the
#: parsing below does not read as a sequence of magic indexes.
_PRICE: Final = 0
_WHOLE_LOT_VOLUME: Final = 1
_LAST_24H: Final = 1


class KrakenAdapter(ExchangeAdapter):
    """Collects spot markets from Kraken."""

    slug = "kraken"
    base_url = "https://api.kraken.com"

    def _unwrap(self, payload: Any, *, path: str) -> dict[str, Any]:
        """Return the ``result`` object, raising if Kraken reported errors.

        Kraken answers ``200 OK`` with a populated ``error`` array when a request
        fails, so the array has to be checked rather than the status code.

        Raises:
            ExchangeApplicationError: If ``error`` is non-empty.
            SchemaMismatch: If the envelope is not the documented shape.
        """
        body = self.require_mapping(payload, path=path)
        errors = body.get("error")
        if isinstance(errors, list) and errors:
            raise ExchangeApplicationError(
                "Kraken reported an application error",
                venue_code=str(errors[0])[:80],
                venue=self.slug,
                path=path,
            )
        return self.require_mapping(body, path=path, key="result")

    @staticmethod
    def _split_wsname(wsname: Any) -> tuple[str, str] | None:
        """Split Kraken's ``wsname`` into base and quote codes.

        ``wsname`` is the venue's own unambiguous rendering (``XBT/USD``), which
        avoids having to unpick the ``X``/``Z`` prefixes on ``base`` and ``quote``.
        """
        if not isinstance(wsname, str) or "/" not in wsname:
            return None
        base, _, quote = wsname.partition("/")
        if not base or not quote:
            return None
        return base, quote

    @staticmethod
    def _strip_legacy_prefix(code: str) -> str:
        """Remove Kraken's legacy ``X``/``Z`` prefix from a 4-character asset code.

        ``XXBT`` becomes ``XBT`` and ``ZUSD`` becomes ``USD``. Applied only to
        4-character codes beginning with X or Z, which is the documented shape of
        the legacy namespace; a modern code such as ``XRP`` is left alone because it
        is three characters.
        """
        if len(code) == 4 and code[0] in ("X", "Z"):
            return code[1:]
        return code

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate every online asset pair from ``/0/public/AssetPairs``."""
        self.refresh_receive_time()
        payload = await self.client.get_json("/0/public/AssetPairs", operation="asset-pairs")
        result = self._unwrap(payload, path="/0/public/AssetPairs")

        instruments: list[RawInstrument] = []
        for pair_id, row in sorted(result.items()):
            if not isinstance(row, dict):
                raise SchemaMismatch("asset pair row was not an object", venue=self.slug)
            status = str(row.get("status", ""))
            if status != "online":
                # `cancel_only`, `post_only`, `limit_only` and `reduce_only` are all
                # real Kraken states that do not describe a normally tradeable market.
                continue

            split = self._split_wsname(row.get("wsname"))
            if split is not None:
                base, quote = split
            else:
                raw_base = row.get("base")
                raw_quote = row.get("quote")
                if not isinstance(raw_base, str) or not isinstance(raw_quote, str):
                    raise SchemaMismatch(
                        "asset pair row had neither wsname nor base/quote", venue=self.slug
                    )
                base = self._strip_legacy_prefix(raw_base)
                quote = self._strip_legacy_prefix(raw_quote)

            instruments.append(
                RawInstrument(
                    venue_slug=self.slug,
                    symbol_native=pair_id,
                    instrument_class="spot",
                    instrument_type="spot",
                    base_symbol_native=base,
                    quote_symbol_native=quote,
                    tick_size=parse_positive(row.get("tick_size")),
                    minimum_quantity=parse_non_negative(row.get("ordermin")),
                    minimum_notional=parse_non_negative(row.get("costmin")),
                    active=True,
                )
            )
        return instruments

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch every ticker in one request.

        Calling ``/0/public/Ticker`` with no ``pair`` argument returns all pairs,
        which is what keeps Kraken coverage to a single request.
        """
        self.refresh_receive_time()
        payload = await self.client.get_json("/0/public/Ticker", operation="ticker")
        result = self._unwrap(payload, path="/0/public/Ticker")

        tickers: list[RawTicker] = []
        for pair_id, row in sorted(result.items()):
            if not isinstance(row, dict):
                continue
            parsed = self._parse_ticker(pair_id, row)
            if parsed is not None:
                tickers.append(parsed)
        return tickers

    def _parse_ticker(self, pair_id: str, row: dict[str, Any]) -> RawTicker | None:
        """Turn one Kraken ticker entry into a :class:`RawTicker`.

        Kraken's ticker fields are short arrays: ``a`` is ask, ``b`` is bid, ``c`` is
        last trade, ``v`` is volume, ``p`` is VWAP, ``t`` is trade count, ``l`` is
        low and ``h`` is high. Each of ``v``, ``p``, ``t``, ``l`` and ``h`` has a
        today value at index 0 and a rolling 24-hour value at index 1; Atlas uses
        index 1 throughout so that every venue's 24-hour figure means the same thing.
        """

        def element(key: str, index: int) -> float | None:
            value = row.get(key)
            if isinstance(value, list | tuple) and len(value) > index:
                return parse_positive(value[index])
            return None

        def element_non_negative(key: str, index: int) -> float | None:
            value = row.get(key)
            if isinstance(value, list | tuple) and len(value) > index:
                return parse_non_negative(value[index])
            return None

        def element_int(key: str, index: int) -> int | None:
            value = row.get(key)
            if isinstance(value, list | tuple) and len(value) > index:
                return parse_int(value[index])
            return None

        last = element("c", _PRICE)
        bid = element("b", _PRICE)
        ask = element("a", _PRICE)
        if last is None and bid is None and ask is None:
            return None

        return RawTicker(
            venue_slug=self.slug,
            symbol_native=pair_id,
            # Kraken publishes no timestamp on this endpoint.
            timing=self.timing(),
            last_price=last,
            bid_price=bid,
            ask_price=ask,
            bid_size=element_non_negative("b", _WHOLE_LOT_VOLUME),
            ask_size=element_non_negative("a", _WHOLE_LOT_VOLUME),
            open_24h=parse_positive(row.get("o")),
            high_24h=element("h", _LAST_24H),
            low_24h=element("l", _LAST_24H),
            base_volume_24h=element_non_negative("v", _LAST_24H),
            # Kraken publishes a 24h VWAP but no quote volume. Multiplying the two
            # would fabricate a figure, so quote volume stays null.
            quote_volume_24h=None,
            trade_count_24h=element_int("t", _LAST_24H),
        )

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one order-book snapshot.

        Kraken caps ``count`` at 500 levels per side.
        """
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/0/public/Depth",
            params={"pair": symbol_native, "count": min(depth, 500)},
            operation="depth",
        )
        result = self._unwrap(payload, path="/0/public/Depth")
        if not result:
            raise SchemaMismatch(
                "depth response contained no pair", venue=self.slug, symbol=symbol_native
            )
        # Kraken keys the result by its own canonical pair name, which may differ
        # from the requested alias, so the single entry is taken rather than looked up.
        book = self.require_mapping(next(iter(result.values())), path="/0/public/Depth")
        bids, bids_truncated = self.build_levels(
            self.require_list(book, path="depth", key="bids"), depth=depth, descending=True
        )
        asks, asks_truncated = self.build_levels(
            self.require_list(book, path="depth", key="asks"), depth=depth, descending=False
        )
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            timing=self.timing(),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated,
        )

    async def fetch_fx_observations(self) -> list[RawFxObservation]:
        """Observe stablecoin-to-USD rates from Kraken's fiat markets."""
        self.refresh_receive_time()
        payload = await self.client.get_json("/0/public/Ticker", operation="ticker:fx")
        result = self._unwrap(payload, path="/0/public/Ticker")

        observations: list[RawFxObservation] = []
        for pair_id, from_code, to_code in _FX_PAIRS:
            row = result.get(pair_id)
            if not isinstance(row, dict):
                continue
            ticker = self._parse_ticker(pair_id, row)
            if ticker is None:
                continue
            candidate = ticker.reference_candidate
            if candidate is None:
                continue
            rate, source = candidate
            observations.append(
                RawFxObservation(
                    venue_slug=self.slug,
                    symbol_native=pair_id,
                    timing=self.timing(),
                    from_symbol_native=from_code,
                    to_symbol_native=to_code,
                    rate=rate,
                    source_price=source,
                )
            )
        return observations

    async def server_time_ms(self) -> int | None:
        """Fetch Kraken's server clock for skew measurement."""
        payload = await self.client.get_json("/0/public/Time", operation="time")
        result = self._unwrap(payload, path="/0/public/Time")
        unix = parse_positive(result.get("unixtime"))
        return int(unix * 1000) if unix is not None else None
