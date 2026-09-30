"""Crypto.com Exchange adapter.

Official documentation: https://exchange-docs.crypto.com/exchange/v1/rest-ws/index.html

Verified 2026-09-27. Three shape notes:

- ``/public/get-instruments`` covers spot (``CCY_PAIR``), perpetuals
  (``PERPETUAL_SWAP``) and dated futures (``FUTURE``) in one request.
- ``/public/get-tickers`` uses single-letter field names: ``i`` instrument, ``a``
  last, ``b`` bid, ``k`` ask, ``h`` high, ``l`` low, ``v`` base volume, ``vv`` the
  venue's own 24h traded value in USD, ``oi`` open interest, ``t`` timestamp,
  ``c`` change.
- ``vv`` is **not** quote-currency volume, whatever the pair's quote currency is.
  Verified arithmetically: for ``ETH_BTC`` (quoted in BTC), ``v * last`` gives
  about 7.95 BTC of turnover, while ``vv`` was 672,529 - matching
  ``7.95 * (BTC/USD reference price)``, not the BTC-denominated turnover. For a
  USD- or USDT-quoted pair the two are numerically close, which is what made this
  easy to miss: ``v * last`` and ``vv`` agree whenever the quote currency is
  already worth about one dollar, and diverge by orders of magnitude otherwise.
  Atlas's schema requires quote_volume_24h to be reported in the *instrument's own
  quote currency*, both because that is what the field means everywhere else and
  because Atlas performs its own documented USD conversion on it, with its own
  provenance. Passing the venue's already-USD figure through that conversion a
  second time using the pair's true quote currency was the fault this comment
  replaced; it inflated the observation by the quote asset's own price. ``vv`` is
  therefore not used at all, and quote_volume_24h stays null for this venue, the
  same conservative choice already made for Coinbase, Kraken, Bitstamp and
  Bitfinex.

The ticker carries open interest but no funding rate, so funding stays null for this
venue rather than being sourced from somewhere it does not belong.
"""

from __future__ import annotations

from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.atlas_time import plausible_contract_time_ms, plausible_epoch_ms
from vizanix_atlas.core.errors import ExchangeApplicationError, SchemaMismatch
from vizanix_atlas.core.numeric import parse_float, parse_non_negative, parse_positive
from vizanix_atlas.models.enums import OpenInterestUnit
from vizanix_atlas.models.observations import (
    RawDerivativeObservation,
    RawFxObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)

#: Crypto.com instrument type to Atlas instrument type and class.
_TYPE_MAP: Final = {
    "CCY_PAIR": ("spot", "spot"),
    "PERPETUAL_SWAP": ("perpetual", "linear-perp"),
    "FUTURE": ("future", "linear-future"),
}

_FX_PAIRS: Final = (
    ("USDT_USD", "USDT", "USD"),
    ("USDC_USD", "USDC", "USD"),
    ("USDC_USDT", "USDC", "USDT"),
)


class CryptocomAdapter(ExchangeAdapter):
    """Collects spot, perpetual and dated futures markets from Crypto.com Exchange."""

    slug = "cryptocom"
    base_url = "https://api.crypto.com"

    def _unwrap(self, payload: Any, *, path: str) -> list[Any]:
        """Return the ``result.data`` array, raising if the venue reported an error.

        Raises:
            ExchangeApplicationError: If ``code`` is non-zero.

        """
        body = self.require_mapping(payload, path=path)
        code = body.get("code")
        if code not in (0, "0", None):
            raise ExchangeApplicationError(
                "Crypto.com reported an application error",
                venue_code=str(code)[:40],
                venue=self.slug,
                path=path,
                message=str(body.get("message", ""))[:200],
            )
        result = self.require_mapping(body, path=path, key="result")
        return self.require_list(result, path=path, key="data")

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate every tradable instrument in one request."""
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/exchange/v1/public/get-instruments", operation="instruments"
        )
        rows = self._unwrap(payload, path="/exchange/v1/public/get-instruments")

        instruments: list[RawInstrument] = []
        for row in rows:
            if not isinstance(row, dict):
                raise SchemaMismatch("instrument row was not an object", venue=self.slug)
            parsed = self._parse_instrument(row)
            if parsed is not None:
                instruments.append(parsed)
        return instruments

    def _parse_instrument(self, row: dict[str, Any]) -> RawInstrument | None:
        """Turn one instrument row into a :class:`RawInstrument`."""
        symbol = row.get("symbol")
        base = row.get("base_ccy")
        quote = row.get("quote_ccy")
        if not (isinstance(symbol, str) and isinstance(base, str) and isinstance(quote, str)):
            raise SchemaMismatch(
                "instrument row lacked symbol, base_ccy or quote_ccy", venue=self.slug
            )
        if not row.get("tradable", False):
            return None

        inst_type = str(row.get("inst_type", ""))
        mapped = _TYPE_MAP.get(inst_type)
        if mapped is None:
            self.note_unknown_enum("inst_type", inst_type)
            return None
        instrument_type, instrument_class = mapped

        # expiry_timestamp_ms is 0 for perpetuals and spot.
        expiry = plausible_contract_time_ms(parse_float(row.get("expiry_timestamp_ms")))
        if instrument_type == "future" and expiry is None:
            return None
        if instrument_type != "future":
            expiry = None

        is_spot = instrument_type == "spot"
        return RawInstrument(
            venue_slug=self.slug,
            symbol_native=symbol,
            instrument_class=instrument_class,
            instrument_type=instrument_type,
            base_symbol_native=base,
            quote_symbol_native=quote,
            settlement_symbol_native=None if is_spot else quote,
            contract_type=None if is_spot else "linear",
            contract_multiplier=None if is_spot else parse_positive(row.get("contract_size")),
            contract_value_symbol_native=None if is_spot else base,
            tick_size=parse_positive(row.get("price_tick_size")),
            quantity_step=parse_positive(row.get("qty_tick_size")),
            expiry=expiry,
            # The venue publishes no funding rate on its public ticker, so Atlas
            # declares no funding semantics rather than implying it has them.
            funding_interval_hours=None,
            funding_semantics=None,
            open_interest_unit=None if is_spot else OpenInterestUnit.CONTRACTS.value,
            active=True,
        )

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch every ticker in one request."""
        tickers: list[RawTicker] = []
        for row, symbol, event_time in await self._ticker_rows():
            tickers.append(
                RawTicker(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    timing=self.timing(exchange_event_time=event_time),
                    last_price=parse_positive(row.get("a")),
                    bid_price=parse_positive(row.get("b")),
                    ask_price=parse_positive(row.get("k")),
                    high_24h=parse_positive(row.get("h")),
                    low_24h=parse_positive(row.get("l")),
                    base_volume_24h=parse_non_negative(row.get("v")),
                    # vv is the venue's own USD-denominated turnover, not quote-currency
                    # volume; see the module docstring. Using it here would be converted
                    # to USD a second time by Atlas's own quote-conversion step.
                    quote_volume_24h=None,
                )
            )
        return tickers

    async def fetch_derivatives(self) -> list[RawDerivativeObservation]:
        """Fetch open interest from the ticker response.

        The public ticker carries ``oi`` but no funding rate and no mark price, so
        those stay null. Recording an index or last price as a mark price would break
        the guarantee that basis is always computed from mark prices.
        """
        observations: list[RawDerivativeObservation] = []
        for row, symbol, event_time in await self._ticker_rows():
            open_interest = parse_non_negative(row.get("oi"))
            if open_interest is None:
                continue
            observations.append(
                RawDerivativeObservation(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    timing=self.timing(exchange_event_time=event_time),
                    open_interest_raw=open_interest,
                    open_interest_unit=OpenInterestUnit.CONTRACTS,
                )
            )
        return observations

    async def _ticker_rows(self) -> list[tuple[dict[str, Any], str, int | None]]:
        """Fetch and normalise the bulk ticker array."""
        self.refresh_receive_time()
        payload = await self.client.get_json("/exchange/v1/public/get-tickers", operation="tickers")
        rows = self._unwrap(payload, path="/exchange/v1/public/get-tickers")
        out: list[tuple[dict[str, Any], str, int | None]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("i")
            if not isinstance(symbol, str):
                continue
            out.append((row, symbol, plausible_epoch_ms(parse_float(row.get("t")))))
        return out

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one order-book snapshot."""
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/exchange/v1/public/get-book",
            params={"instrument_name": symbol_native, "depth": min(depth, 150)},
            operation="book",
        )
        rows = self._unwrap(payload, path="/exchange/v1/public/get-book")
        if not rows:
            raise SchemaMismatch(
                "book response contained no data", venue=self.slug, symbol=symbol_native
            )
        book = self.require_mapping(rows[0], path="get-book")
        bids, bids_truncated = self.build_levels(
            self.require_list(book, path="get-book", key="bids"), depth=depth, descending=True
        )
        asks, asks_truncated = self.build_levels(
            self.require_list(book, path="get-book", key="asks"), depth=depth, descending=False
        )
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            timing=self.timing(exchange_event_time=plausible_epoch_ms(parse_float(book.get("t")))),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated,
        )

    async def fetch_fx_observations(self) -> list[RawFxObservation]:
        """Observe stablecoin-to-USD rates from Crypto.com's USD markets."""
        rows = {symbol: (row, t) for row, symbol, t in await self._ticker_rows()}
        observations: list[RawFxObservation] = []
        for symbol, from_code, to_code in _FX_PAIRS:
            entry = rows.get(symbol)
            if entry is None:
                continue
            row, event_time = entry
            ticker = RawTicker(
                venue_slug=self.slug,
                symbol_native=symbol,
                timing=self.timing(exchange_event_time=event_time),
                last_price=parse_positive(row.get("a")),
                bid_price=parse_positive(row.get("b")),
                ask_price=parse_positive(row.get("k")),
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
