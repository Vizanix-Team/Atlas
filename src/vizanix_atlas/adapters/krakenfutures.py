"""Kraken Futures adapter.

Official documentation: https://docs.kraken.com/api/docs/futures-api/trading/get-tickers

Funding semantics, verified rather than assumed
-----------------------------------------------
Kraken Futures publishes ``fundingRate`` as an **absolute** rate, which the
documentation describes only as "the current absolute funding rate" without giving a
conversion to a relative rate. A ``relativeFundingRate`` field is documented as
present "when available", and it was absent from all 300 records in the live bulk
ticker response captured on 2026-09-27.

Rather than guess, the conversion was derived from the venue's own published data.
``/derivatives/api/v4/historicalfundingrates`` returns both the absolute and the
relative rate for the same hour, which pins the relationship exactly:

``PF_XBTUSD`` (linear, contract is 1 BTC, funding denominated in USD):

    absolute 0.18037855027822186, mark 84420.34806550229
    absolute / mark = 2.13668e-06
    venue's published relativeFundingRate = 2.136845833333e-06

``PI_XBTUSD`` (inverse, contract is 1 USD, funding denominated in BTC):

    absolute 2.40324183e-10, mark 84436.41626261781
    absolute * mark = 2.029211e-05
    venue's published relativeFundingRate = 2.0286595833333e-05

So the relative rate is ``absolute / mark`` for linear contracts and
``absolute * mark`` for inverse ones. Both agree with the venue's own figure to
within the difference expected from the reference price being sampled a moment
apart. The derivation is tagged in provenance as ``derived_from_absolute`` and the
arithmetic is restated in ``docs/METHODOLOGY.md``.

The same historical endpoint fixes the interval: consecutive records were at
15:00, 16:00 and 17:00 UTC, so funding is hourly.

Perpetual versus dated
----------------------
The instruments endpoint carries no ``tag`` field (that appears only on tickers), so
the two product families inside one ``type`` are told apart by which fields the venue
populates: a perpetual carries ``fundingRateCoefficient`` and no ``lastTradingTime``,
while a dated contract carries ``lastTradingTime`` and no funding coefficient. Both
``futures_inverse`` (4 perpetual, 8 dated when captured) and ``flexible_futures``
(278 perpetual, 10 dated) contain a mix, which is why the type alone is not enough.

Open interest
-------------
The documentation does not state what unit ``openInterest`` is in. The values are
consistent with a contract count (``PI_XBTUSD`` at 2,278,207 against a 1 USD
contract size, and ``PF_XBTUSD`` at 2,139.7 against a 1 BTC contract size), so Atlas
records it as contracts and applies the published ``contractSize``. The unit is
declared as such in the instrument record, so a consumer can see the basis.
"""

from __future__ import annotations

from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.atlas_time import parse_iso, to_epoch_ms
from vizanix_atlas.core.errors import ExchangeApplicationError, SchemaMismatch
from vizanix_atlas.core.numeric import parse_float, parse_non_negative, parse_positive
from vizanix_atlas.models.enums import OpenInterestUnit
from vizanix_atlas.models.observations import (
    RawDerivativeObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)

#: Kraken Futures product type to contract denomination. Each family contains both
#: perpetual and dated contracts, so the type fixes only linear versus inverse.
_CONTRACT_TYPE_BY_PRODUCT: Final = {
    "flexible_futures": "linear",
    "futures_inverse": "inverse",
    "futures_vanilla": "linear",
}

#: Funding is paid hourly, confirmed from consecutive hourly historical records.
_FUNDING_INTERVAL_HOURS: Final = 1.0


class KrakenFuturesAdapter(ExchangeAdapter):
    """Collects perpetual and dated futures from Kraken Futures."""

    slug = "krakenfutures"
    base_url = "https://futures.kraken.com"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Contract denomination per symbol, learned at discovery and needed to turn
        # the venue's absolute funding rate into a relative one.
        self._contract_type: dict[str, str] = {}
        self._contract_size: dict[str, float] = {}

    def _unwrap(self, payload: Any, *, path: str) -> dict[str, Any]:
        """Return the response body, raising if Kraken Futures reported an error.

        The API answers ``200 OK`` with ``result`` set to ``"error"`` and an
        ``error`` field when a request fails.

        Raises:
            ExchangeApplicationError: If ``result`` is not ``"success"``.

        """
        body = self.require_mapping(payload, path=path)
        result = str(body.get("result", ""))
        if result and result != "success":
            raise ExchangeApplicationError(
                "Kraken Futures reported an application error",
                venue_code=str(body.get("error", result))[:80],
                venue=self.slug,
                path=path,
            )
        return body

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate every tradeable contract from ``/derivatives/api/v3/instruments``."""
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/derivatives/api/v3/instruments", operation="instruments"
        )
        body = self._unwrap(payload, path="/derivatives/api/v3/instruments")
        rows = self.require_list(body, path="/derivatives/api/v3/instruments", key="instruments")

        instruments: list[RawInstrument] = []
        for row in rows:
            if not isinstance(row, dict):
                raise SchemaMismatch("instrument row was not an object", venue=self.slug)
            parsed = self._parse_instrument(row)
            if parsed is not None:
                instruments.append(parsed)
        return instruments

    def _parse_instrument(self, row: dict[str, Any]) -> RawInstrument | None:
        """Turn one contract into a :class:`RawInstrument`, or skip it."""
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise SchemaMismatch("instrument row had no symbol", venue=self.slug)
        if not row.get("tradeable", False):
            return None

        product_type = str(row.get("type", ""))
        contract_type = _CONTRACT_TYPE_BY_PRODUCT.get(product_type)
        if contract_type is None:
            # Kraken Futures also lists index and spot-reference products, which are
            # not tradeable contracts. An unrecognised type is counted so that a new
            # product family shows up as schema drift rather than being absorbed.
            self.note_unknown_enum("type", product_type)
            return None

        base = row.get("base")
        quote = row.get("quote")
        if not isinstance(base, str) or not isinstance(quote, str):
            # Some products carry only `underlying` and `pair`. Without an explicit
            # base and quote, Atlas does not attempt to infer them from the symbol.
            return None

        # A dated contract publishes lastTradingTime; a perpetual publishes
        # fundingRateCoefficient instead. Both product families contain a mix, so this
        # is the discriminator rather than the type or the symbol prefix.
        raw_expiry = row.get("lastTradingTime")
        expiry: int | None = None
        if isinstance(raw_expiry, str):
            try:
                expiry = to_epoch_ms(parse_iso(raw_expiry))
            except ValueError:
                self.note_unknown_enum("lastTradingTime", raw_expiry[:32])
                return None
            instrument_type = "future"
        elif row.get("fundingRateCoefficient") is not None:
            instrument_type = "perpetual"
        else:
            # Neither marker present: not a contract Atlas can classify without
            # guessing, so it is skipped and counted.
            self.note_unknown_enum("classification", f"{product_type}:unclassified")
            return None

        contract_size = parse_positive(row.get("contractSize"))
        self._contract_type[symbol] = contract_type
        if contract_size is not None:
            self._contract_size[symbol] = contract_size

        listing_time = None
        opening = row.get("openingDate")
        if isinstance(opening, str):
            try:
                listing_time = to_epoch_ms(parse_iso(opening))
            except ValueError:
                self.note_unknown_enum("openingDate", opening[:32])

        instrument_class = (
            f"{contract_type}-{'perp' if instrument_type == 'perpetual' else 'future'}"
        )
        return RawInstrument(
            venue_slug=self.slug,
            symbol_native=symbol,
            instrument_class=instrument_class,
            instrument_type=instrument_type,
            base_symbol_native=base,
            quote_symbol_native=quote,
            # Inverse contracts settle in the base asset; linear ones in the quote.
            settlement_symbol_native=base if contract_type == "inverse" else quote,
            contract_type=contract_type,
            contract_multiplier=contract_size,
            contract_value_symbol_native=quote if contract_type == "inverse" else base,
            tick_size=parse_positive(row.get("tickSize")),
            expiry=expiry,
            funding_interval_hours=(
                _FUNDING_INTERVAL_HOURS if instrument_type == "perpetual" else None
            ),
            funding_semantics=("absolute_per_interval" if instrument_type == "perpetual" else None),
            open_interest_unit=OpenInterestUnit.CONTRACTS.value,
            active=True,
            listing_time=listing_time,
        )

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch every ticker in one request.

        The bulk ticker response carries quotes, mark, index, funding and open
        interest together, so the derivative fetch reuses it rather than repeating it.
        """
        rows = await self._fetch_ticker_rows()
        tickers: list[RawTicker] = []
        for row in rows:
            symbol = row.get("symbol")
            if not isinstance(symbol, str) or row.get("suspended"):
                continue
            tickers.append(
                RawTicker(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    timing=self.timing(exchange_event_time=self._event_time(row)),
                    last_price=parse_positive(row.get("last")),
                    bid_price=parse_positive(row.get("bid")),
                    ask_price=parse_positive(row.get("ask")),
                    bid_size=parse_non_negative(row.get("bidSize")),
                    ask_size=parse_non_negative(row.get("askSize")),
                    open_24h=parse_positive(row.get("open24h")),
                    high_24h=parse_positive(row.get("high24h")),
                    low_24h=parse_positive(row.get("low24h")),
                    # vol24h is a contract count; volumeQuote is the quote-currency
                    # turnover. For inverse contracts the two coincide because the
                    # contract is itself denominated in the quote currency.
                    contract_volume_24h=parse_non_negative(row.get("vol24h")),
                    quote_volume_24h=parse_non_negative(row.get("volumeQuote")),
                )
            )
        return tickers

    async def fetch_derivatives(self) -> list[RawDerivativeObservation]:
        """Fetch mark, index, funding and open interest from the bulk ticker response."""
        rows = await self._fetch_ticker_rows()
        observations: list[RawDerivativeObservation] = []
        for row in rows:
            symbol = row.get("symbol")
            if not isinstance(symbol, str) or row.get("suspended"):
                continue
            mark = parse_positive(row.get("markPrice"))
            absolute_funding = parse_float(row.get("fundingRate"))
            observations.append(
                RawDerivativeObservation(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    # Deliberately no exchange_event_time. The only per-record timestamp
                    # this response carries is `lastTime`, the last trade, which on a thin
                    # dated contract can be hours old. Mark price, funding and open
                    # interest in the same response are current values, so attributing the
                    # last trade's time to them would report fresh data as stale (observed
                    # at over three hours on a real run) and would wrongly exclude it.
                    # `lastTime` is still recorded on the ticker, where it belongs.
                    timing=self.timing(),
                    mark_price=mark,
                    index_price=parse_positive(row.get("indexPrice")),
                    # The raw absolute rate is preserved exactly as published. The
                    # relative conversion happens in normalisation, where the
                    # instrument's contract type is available and the conversion can
                    # be recorded in provenance.
                    funding_rate_raw=absolute_funding,
                    funding_interval_hours=(
                        _FUNDING_INTERVAL_HOURS if absolute_funding is not None else None
                    ),
                    open_interest_raw=parse_non_negative(row.get("openInterest")),
                    open_interest_unit=(
                        OpenInterestUnit.CONTRACTS if row.get("openInterest") is not None else None
                    ),
                )
            )
        return observations

    async def _fetch_ticker_rows(self) -> list[dict[str, Any]]:
        """Fetch and validate the bulk ticker array."""
        self.refresh_receive_time()
        payload = await self.client.get_json("/derivatives/api/v3/tickers", operation="tickers")
        body = self._unwrap(payload, path="/derivatives/api/v3/tickers")
        rows = self.require_list(body, path="/derivatives/api/v3/tickers", key="tickers")
        return [row for row in rows if isinstance(row, dict)]

    def _event_time(self, row: dict[str, Any]) -> int | None:
        """Read ``lastTime`` as the ticker's event timestamp.

        ``lastTime`` is the time of the last trade. That is the right event time for a
        ticker, whose last price it describes, and the wrong one for the funding and open
        interest carried in the same response, which are current. On a thin dated contract
        it can be hours old, so Atlas records it here and nowhere else.
        """
        raw = row.get("lastTime")
        if not isinstance(raw, str):
            return None
        try:
            return to_epoch_ms(parse_iso(raw))
        except ValueError:
            self.note_unknown_enum("lastTime", raw[:32])
            return None

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one order-book snapshot."""
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/derivatives/api/v3/orderbook",
            params={"symbol": symbol_native},
            operation="orderbook",
        )
        body = self._unwrap(payload, path="/derivatives/api/v3/orderbook")
        book = self.require_mapping(body, path="/derivatives/api/v3/orderbook", key="orderBook")
        bids, bids_truncated = self.build_levels(
            self.require_list(book, path="orderbook", key="bids"), depth=depth, descending=True
        )
        asks, asks_truncated = self.build_levels(
            self.require_list(book, path="orderbook", key="asks"), depth=depth, descending=False
        )
        server_time = None
        raw = body.get("serverTime")
        if isinstance(raw, str):
            try:
                server_time = to_epoch_ms(parse_iso(raw))
            except ValueError:
                self.note_unknown_enum("serverTime", raw[:32])
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            timing=self.timing(exchange_server_time=server_time),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated,
        )

    async def server_time_ms(self) -> int | None:
        """Fetch the venue clock, which the instruments response already carries."""
        payload = await self.client.get_json("/derivatives/api/v3/instruments", operation="time")
        body = self._unwrap(payload, path="/derivatives/api/v3/instruments")
        raw = body.get("serverTime")
        if not isinstance(raw, str):
            return None
        try:
            return to_epoch_ms(parse_iso(raw))
        except ValueError:
            return None
