"""Bybit adapter (V5 API).

Official documentation: https://bybit-exchange.github.io/docs/v5/intro

Verified 2026-10-11 against live responses captured from a European network (the hosts
refuse some jurisdictions, see ``docs/DATA_SOURCES.md``). Every field name and unit below
was checked against those payloads, not recalled.

One bulk request per ``category`` returns the whole catalogue (``instruments-info``) and
the whole quote board (``tickers``), so the venue costs about a dozen requests: ``spot``,
``linear`` (USDT/USDC margined) and ``inverse`` (coin margined).

Units, as observed:

``spot``
    ``volume24h`` is base volume and ``turnover24h`` is quote volume (BTCUSDT: 1.72 BTC
    against 143,047 USD).
``linear``
    ``volume24h`` is base volume, ``turnover24h`` quote volume, ``openInterest`` is in
    **base asset units** and ``openInterestValue`` is the same figure in the quote
    currency. ``fundingRate`` is a relative rate per ``fundingIntervalHour``.
``inverse``
    ``volume24h`` is in **contracts** (each worth 1 USD) and ``turnover24h`` is in the
    base coin (BTCUSD: 130,342,124 contracts against 1,566.7 BTC, which at 83,582 USD is
    the same notional). ``openInterest`` is in contracts and ``openInterestValue`` is the
    base-coin equivalent, which Atlas stores as the venue-published base figure.

Bybit also lists tokenised equities, ETFs, commodities and FX pairs as USDT contracts
(``symbolType`` ``stock``, ``ETF``, ``commodity``, ``forex``) and tokenised stocks on spot
(``xstocks``). Atlas is a crypto market-state layer, so those are skipped.
"""

from __future__ import annotations

from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.atlas_time import plausible_contract_time_ms, plausible_epoch_ms
from vizanix_atlas.core.errors import ExchangeApplicationError, SchemaMismatch
from vizanix_atlas.core.http import ExchangeHttpClient
from vizanix_atlas.core.numeric import parse_float, parse_non_negative, parse_positive
from vizanix_atlas.models.enums import OpenInterestUnit
from vizanix_atlas.models.observations import (
    RawDerivativeObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)
from vizanix_atlas.models.venue import Venue

#: ``symbolType`` values that describe non-crypto underlyings. Skipped by design.
_NON_CRYPTO_SYMBOL_TYPES: Final = frozenset({"stock", "ETF", "commodity", "forex", "xstocks"})

#: ``symbolType`` values seen on crypto markets. Anything else is counted as unknown.
_CRYPTO_SYMBOL_TYPES: Final = frozenset({"", "innovation", "adventure"})

#: Derivative categories in collection order.
_DERIVATIVE_CATEGORIES: Final = ("linear", "inverse")

#: Orderbook depth limits per category (the venue rejects anything larger).
_MAX_BOOK_LIMIT: Final = {"spot": 200, "linear": 500, "inverse": 500}

#: Page size for ``instruments-info``; 1000 is the documented maximum.
_PAGE_LIMIT: Final = 1000

#: Safety bound on pagination, so a venue that stops terminating cursors cannot loop.
_MAX_PAGES: Final = 20

_CONTRACT_KINDS: Final = {
    "LinearPerpetual": ("linear", "perpetual"),
    "LinearFutures": ("linear", "future"),
    "InversePerpetual": ("inverse", "perpetual"),
    "InverseFutures": ("inverse", "future"),
}


def _mapping(value: Any) -> dict[str, Any]:
    """Return ``value`` when it is an object, otherwise an empty mapping.

    Used for the nested ``priceFilter`` and ``lotSizeFilter`` blocks: a block that goes
    missing must leave those fields empty, not crash the whole catalogue.
    """
    return value if isinstance(value, dict) else {}


class BybitAdapter(ExchangeAdapter):
    """Collects spot, linear and inverse markets from Bybit."""

    slug = "bybit"
    base_url = "https://api.bybit.com"

    def __init__(self, venue: Venue, client: ExchangeHttpClient) -> None:
        super().__init__(venue, client)
        #: Native symbol to the ``(category, instrument_class)`` its order book belongs to,
        #: filled from the catalogue. Spot wins a shared symbol such as ``BTCUSDT``.
        self._book_targets: dict[str, tuple[str, str]] = {}
        #: ``(instrument_class, symbol)`` of every instrument discovery kept. Once discovery
        #: has run, quotes for anything else (skipped non-crypto markets) are not emitted.
        self._kept: set[tuple[str, str]] = set()

    def _unwrap(self, payload: Any, *, path: str) -> dict[str, Any]:
        """Return ``result`` after checking Bybit's application-level status.

        Bybit answers ``200 OK`` with a non-zero ``retCode`` when a request is rejected.

        Raises:
            ExchangeApplicationError: If ``retCode`` is not ``0``.

        """
        body = self.require_mapping(payload, path=path)
        code = body.get("retCode")
        if code != 0:
            raise ExchangeApplicationError(
                "Bybit reported an application error",
                venue_code=str(code),
                venue=self.slug,
                path=path,
                message=str(body.get("retMsg", ""))[:200],
            )
        return self.require_mapping(body, path=path, key="result")

    # ------------------------------------------------------------- discovery

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate spot, linear and inverse instruments, following pagination."""
        instruments: list[RawInstrument] = []
        for category in ("spot", *_DERIVATIVE_CATEGORIES):
            for row in await self._catalogue(category):
                parsed = (
                    self._parse_spot_instrument(row)
                    if category == "spot"
                    else self._parse_contract(row, category)
                )
                if parsed is not None:
                    instruments.append(parsed)
                    self._kept.add((parsed.instrument_class, parsed.symbol_native))
                    self._book_targets.setdefault(
                        parsed.symbol_native, (category, parsed.instrument_class)
                    )
        return instruments

    def _is_known(self, instrument_class: str, symbol: str) -> bool:
        """Return whether quotes for this market should be emitted.

        Before discovery has run (a standalone ticker call) everything is allowed; after it,
        only markets the catalogue kept, so skipped non-crypto markets never reach analytics.
        """
        return not self._kept or (instrument_class, symbol) in self._kept

    async def _catalogue(self, category: str) -> list[Any]:
        """Fetch every ``instruments-info`` page for one category."""
        path = "/v5/market/instruments-info"
        rows: list[Any] = []
        cursor = ""
        for _ in range(_MAX_PAGES):
            self.refresh_receive_time()
            params: dict[str, Any] = {"category": category, "limit": _PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            payload = await self.client.get_json(
                path, params=params, operation=f"instruments:{category}"
            )
            result = self._unwrap(payload, path=path)
            rows.extend(self.require_list(result, path=path, key="list"))
            cursor = str(result.get("nextPageCursor") or "")
            if not cursor:
                return rows
        self.note_unknown_enum("instruments-info.pagination", f"{category}:unterminated")
        return rows

    def _skip_symbol_type(self, row: dict[str, Any]) -> bool:
        """Return whether a catalogue row describes a non-crypto or unknown product."""
        symbol_type = str(row.get("symbolType") or "")
        if symbol_type in _NON_CRYPTO_SYMBOL_TYPES:
            return True
        if symbol_type not in _CRYPTO_SYMBOL_TYPES:
            self.note_unknown_enum("symbolType", symbol_type)
        return False

    def _parse_spot_instrument(self, row: Any) -> RawInstrument | None:
        """Turn one spot catalogue row into a :class:`RawInstrument`, or skip it."""
        if not isinstance(row, dict):
            raise SchemaMismatch("spot instrument row was not an object", venue=self.slug)
        symbol, base, quote = row.get("symbol"), row.get("baseCoin"), row.get("quoteCoin")
        if not (isinstance(symbol, str) and isinstance(base, str) and isinstance(quote, str)):
            raise SchemaMismatch(
                "spot instrument row lacked symbol, baseCoin or quoteCoin", venue=self.slug
            )
        if str(row.get("status", "")) != "Trading" or self._skip_symbol_type(row):
            return None
        lot = _mapping(row.get("lotSizeFilter"))
        price = _mapping(row.get("priceFilter"))
        return RawInstrument(
            venue_slug=self.slug,
            symbol_native=symbol,
            instrument_class="spot",
            instrument_type="spot",
            base_symbol_native=base,
            quote_symbol_native=quote,
            tick_size=parse_positive(price.get("tickSize")),
            quantity_step=parse_positive(lot.get("basePrecision")),
            minimum_quantity=parse_non_negative(lot.get("minOrderQty")),
            minimum_notional=parse_non_negative(lot.get("minOrderAmt")),
            active=True,
        )

    def _parse_contract(self, row: Any, category: str) -> RawInstrument | None:
        """Turn one linear or inverse contract row into a :class:`RawInstrument`."""
        if not isinstance(row, dict):
            raise SchemaMismatch("contract row was not an object", venue=self.slug)
        symbol, base, quote = row.get("symbol"), row.get("baseCoin"), row.get("quoteCoin")
        if not (isinstance(symbol, str) and isinstance(base, str) and isinstance(quote, str)):
            raise SchemaMismatch(
                "contract row lacked symbol, baseCoin or quoteCoin", venue=self.slug
            )
        if str(row.get("status", "")) != "Trading" or row.get("isPreListing") is True:
            return None
        if self._skip_symbol_type(row):
            return None

        contract_kind = str(row.get("contractType", ""))
        kind = _CONTRACT_KINDS.get(contract_kind)
        if kind is None:
            self.note_unknown_enum("contractType", contract_kind)
            return None
        contract_type, instrument_type = kind
        if contract_type != category:
            self.note_unknown_enum("contractType/category", f"{contract_kind}/{category}")
            return None

        expiry = None
        if instrument_type == "future":
            expiry = plausible_contract_time_ms(parse_float(row.get("deliveryTime")))
            if expiry is None:
                return None

        settle = row.get("settleCoin")
        lot = _mapping(row.get("lotSizeFilter"))
        price = _mapping(row.get("priceFilter"))
        funding_minutes = parse_positive(row.get("fundingInterval"))
        inverse = contract_type == "inverse"

        return RawInstrument(
            venue_slug=self.slug,
            symbol_native=symbol,
            instrument_class=f"{contract_type}-{'perp' if instrument_type == 'perpetual' else 'future'}",
            instrument_type=instrument_type,
            base_symbol_native=base,
            quote_symbol_native=quote,
            settlement_symbol_native=settle if isinstance(settle, str) and settle else None,
            contract_type=contract_type,
            # Linear quantities are in base units. An inverse contract is worth exactly
            # one unit of the quote currency (USD), which is what the multiplier says.
            contract_multiplier=1.0,
            contract_value_symbol_native=quote if inverse else base,
            tick_size=parse_positive(price.get("tickSize")),
            quantity_step=parse_positive(lot.get("qtyStep")),
            minimum_quantity=parse_non_negative(lot.get("minOrderQty")),
            minimum_notional=parse_non_negative(lot.get("minNotionalValue")),
            expiry=expiry,
            funding_interval_hours=(
                funding_minutes / 60.0
                if instrument_type == "perpetual" and funding_minutes is not None
                else None
            ),
            funding_semantics="relative_per_interval" if instrument_type == "perpetual" else None,
            open_interest_unit=(
                OpenInterestUnit.CONTRACTS.value if inverse else OpenInterestUnit.BASE_ASSET.value
            ),
            active=True,
            listing_time=plausible_contract_time_ms(parse_float(row.get("launchTime"))),
        )

    # ---------------------------------------------------------------- quotes

    async def _tickers(self, category: str) -> tuple[list[Any], int | None]:
        """Fetch one category's bulk ticker board and the venue's response time."""
        path = "/v5/market/tickers"
        self.refresh_receive_time()
        payload = await self.client.get_json(
            path, params={"category": category}, operation=f"tickers:{category}"
        )
        result = self._unwrap(payload, path=path)
        body = self.require_mapping(payload, path=path)
        rows = self.require_list(result, path=path, key="list")
        return rows, plausible_epoch_ms(parse_float(body.get("time")))

    @staticmethod
    def _instrument_class(row: dict[str, Any], category: str) -> str:
        """Return the exact Atlas instrument class a ticker row belongs to.

        A dated contract carries a non-zero ``deliveryTime`` and a perpetual carries
        ``"0"`` or an empty string, so the class is known precisely rather than guessed.
        """
        if category == "spot":
            return "spot"
        delivery = parse_float(row.get("deliveryTime"))
        return f"{category}-{'future' if delivery else 'perp'}"

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch spot, linear and inverse tickers: one bulk request per category."""
        tickers: list[RawTicker] = []
        for category in ("spot", *_DERIVATIVE_CATEGORIES):
            rows, server_time = await self._tickers(category)
            for row in rows:
                parsed = self._parse_ticker(row, category, server_time)
                if parsed is not None:
                    tickers.append(parsed)
        return tickers

    def _parse_ticker(self, row: Any, category: str, server_time: int | None) -> RawTicker | None:
        """Turn one ticker row into a :class:`RawTicker`."""
        if not isinstance(row, dict):
            raise SchemaMismatch("ticker row was not an object", venue=self.slug)
        symbol = row.get("symbol")
        if not isinstance(symbol, str):
            return None
        if not self._is_known(self._instrument_class(row, category), symbol):
            return None
        volume = parse_non_negative(row.get("volume24h"))
        turnover = parse_non_negative(row.get("turnover24h"))
        inverse = category == "inverse"
        return RawTicker(
            venue_slug=self.slug,
            symbol_native=symbol,
            instrument_class=self._instrument_class(row, category),
            timing=self.timing(exchange_server_time=server_time),
            last_price=parse_positive(row.get("lastPrice")),
            bid_price=parse_positive(row.get("bid1Price")),
            ask_price=parse_positive(row.get("ask1Price")),
            bid_size=parse_non_negative(row.get("bid1Size")),
            ask_size=parse_non_negative(row.get("ask1Size")),
            open_24h=parse_positive(row.get("prevPrice24h")),
            high_24h=parse_positive(row.get("highPrice24h")),
            low_24h=parse_positive(row.get("lowPrice24h")),
            # Inverse: volume is contracts and turnover is the base coin. Quote volume is
            # left empty rather than derived, in line with how every other inverse venue
            # is handled.
            base_volume_24h=turnover if inverse else volume,
            quote_volume_24h=None if inverse else turnover,
            contract_volume_24h=volume if inverse else None,
        )

    async def fetch_derivatives(self) -> list[RawDerivativeObservation]:
        """Fetch mark, index, funding and open interest from the bulk linear/inverse boards."""
        observations: list[RawDerivativeObservation] = []
        for category in _DERIVATIVE_CATEGORIES:
            rows, server_time = await self._tickers(category)
            inverse = category == "inverse"
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
                    continue
                if not self._is_known(self._instrument_class(row, category), row["symbol"]):
                    continue
                perpetual = not parse_float(row.get("deliveryTime"))
                open_interest = parse_non_negative(row.get("openInterest"))
                interval = parse_positive(row.get("fundingIntervalHour"))
                observations.append(
                    RawDerivativeObservation(
                        venue_slug=self.slug,
                        symbol_native=row["symbol"],
                        instrument_class=self._instrument_class(row, category),
                        timing=self.timing(exchange_server_time=server_time),
                        mark_price=parse_positive(row.get("markPrice")),
                        index_price=parse_positive(row.get("indexPrice")),
                        funding_rate_raw=parse_float(row.get("fundingRate")) if perpetual else None,
                        funding_interval_hours=interval if perpetual else None,
                        next_funding_time=(
                            plausible_contract_time_ms(parse_float(row.get("nextFundingTime")))
                            if perpetual
                            else None
                        ),
                        open_interest_raw=open_interest,
                        open_interest_unit=(
                            None
                            if open_interest is None
                            else OpenInterestUnit.CONTRACTS
                            if inverse
                            else OpenInterestUnit.BASE_ASSET
                        ),
                        # Linear: openInterest is already in base units. Inverse: the venue
                        # publishes the base-coin equivalent as openInterestValue.
                        open_interest_base=(
                            parse_non_negative(row.get("openInterestValue"))
                            if inverse
                            else open_interest
                        ),
                    )
                )
        return observations

    # ------------------------------------------------------------ order books

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one order-book snapshot.

        The category comes from the catalogue this adapter discovered (spot wins a symbol
        both markets list, such as ``BTCUSDT``). Bybit rejects a wrong category, so a
        mismatch fails loudly instead of returning a different market.
        """
        category, instrument_class = self._book_targets.get(
            symbol_native, self._guess_target(symbol_native)
        )
        limit = min(depth, _MAX_BOOK_LIMIT[category])
        path = "/v5/market/orderbook"
        self.refresh_receive_time()
        payload = await self.client.get_json(
            path,
            params={"category": category, "symbol": symbol_native, "limit": limit},
            operation="orderbook",
        )
        book = self._unwrap(payload, path=path)
        bids, bids_truncated = self.build_levels(
            self.require_list(book, path=path, key="b"), depth=depth, descending=True
        )
        asks, asks_truncated = self.build_levels(
            self.require_list(book, path=path, key="a"), depth=depth, descending=False
        )
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            instrument_class=instrument_class,
            timing=self.timing(exchange_event_time=plausible_epoch_ms(parse_float(book.get("ts")))),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated or len(bids) >= limit,
        )

    @staticmethod
    def _guess_target(symbol: str) -> tuple[str, str]:
        """Fall back to spot for a symbol the catalogue did not list.

        A symbol that was never discovered is not a market Atlas knows about, so the
        conservative answer is the spot endpoint, which rejects what it does not list.
        """
        return "spot", "spot"

    async def server_time_ms(self) -> int | None:
        """Fetch Bybit's server clock for skew measurement."""
        payload = await self.client.get_json("/v5/market/time", operation="time")
        body = self.require_mapping(payload, path="/v5/market/time")
        return plausible_epoch_ms(parse_float(body.get("time")))
