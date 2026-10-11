"""Binance derivatives adapter (USD-M and COIN-M futures).

Official documentation: https://developers.binance.com/docs/derivatives

Verified 2026-10-11 against live responses from a European network. ``fapi`` and ``dapi``
refuse United States addresses (HTTP 451), which is why this venue is collected from a
non-US egress, see ``docs/DATA_SOURCES.md``.

Two products share one slug, on two hosts:

``USD-M`` (``fapi.binance.com``)
    Linear contracts margined and quoted in USDT or USDC. Quantities are in **base asset
    units**, ``volume`` is base volume and ``quoteVolume`` is quote volume. Perpetuals
    publish their funding interval in ``/fapi/v1/fundingInfo`` (8, 4 or 1 hours, observed
    for every trading perpetual).
``COIN-M`` (``dapi.binance.com``)
    Inverse contracts margined in the base coin. One contract is worth ``contractSize``
    USD (100 for BTC, 10 for most others). ``volume`` is in **contracts** and
    ``baseVolume`` is in the base coin (BTCUSD_PERP: 3,575,480 contracts of 100 USD is
    4,291 BTC at 83,317). ``openInterest`` is in contracts. ``/dapi/v1/fundingInfo``
    returns an empty list because every coin-margined contract uses the default 8-hour
    interval, which the funding history confirms (consecutive fundings exactly 8 hours
    apart for BTC, ETH and XRP).

Open interest is the one thing Binance does not publish in bulk: ``openInterest`` takes a
single symbol. Atlas therefore samples the highest-volume contracts of each product
(see ``_OPEN_INTEREST_SAMPLE``) instead of making hundreds of requests, and every other
contract carries mark, index and funding with open interest left empty rather than zero.
That is a documented coverage limit, not a silent one.

Binance also lists tokenised equities, commodities and FX as ``TRADIFI_PERPETUAL``
contracts. Atlas is a crypto market-state layer, so those are skipped.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.atlas_time import plausible_contract_time_ms, plausible_epoch_ms
from vizanix_atlas.core.errors import AtlasError, SchemaMismatch
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

_FAPI: Final = "https://fapi.binance.com"
_DAPI: Final = "https://dapi.binance.com"

#: Contract types Atlas collects. ``TRADIFI_PERPETUAL`` (equities, commodities, FX) and
#: anything unrecognised is skipped, the latter counted as an unknown enum.
_CONTRACT_TYPES: Final = {
    "PERPETUAL": "perpetual",
    "CURRENT_QUARTER": "future",
    "NEXT_QUARTER": "future",
}
_SKIPPED_CONTRACT_TYPES: Final = frozenset({"TRADIFI_PERPETUAL"})

#: Highest-volume contracts per product whose open interest is requested one by one.
#: Sized so the whole venue stays inside the tier A request ceiling in
#: ``config/collection.yaml``.
_OPEN_INTEREST_SAMPLE: Final = {"usd_m": 15, "coin_m": 10}

#: Funding interval of every coin-margined contract (verified, see module docstring).
_COIN_M_FUNDING_HOURS: Final = 8.0

#: Depth values the ``/depth`` endpoint accepts.
_ALLOWED_DEPTHS: Final = (5, 10, 20, 50, 100, 500, 1000)


class BinanceDerivativesAdapter(ExchangeAdapter):
    """Collects USD-M and COIN-M futures from Binance."""

    slug = "binance_derivatives"
    base_url = _FAPI

    def __init__(self, venue: Venue, client: ExchangeHttpClient) -> None:
        super().__init__(venue, client)
        #: Native symbol to the product (``usd_m``/``coin_m``) and instrument class.
        self._targets: dict[str, tuple[str, str]] = {}
        #: Native symbol to the funding interval published by ``fundingInfo``.
        self._funding_hours: dict[str, float] = {}
        #: Native symbol to its 24h quote (USD-M) or base (COIN-M) turnover, for ranking.
        self._turnover: dict[str, float] = {}
        #: Products whose turnover has been loaded, so ranking never repeats a request.
        self._turnover_loaded: set[str] = set()

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _host(product: str) -> str:
        return _FAPI if product == "usd_m" else _DAPI

    @staticmethod
    def _api(product: str) -> str:
        return "/fapi/v1" if product == "usd_m" else "/dapi/v1"

    def _url(self, product: str, endpoint: str) -> str:
        """Build an absolute URL: one HTTP client serves both hosts."""
        return f"{self._host(product)}{self._api(product)}/{endpoint}"

    def _is_known(self, symbol: str) -> bool:
        """Return whether quotes for this contract should be emitted.

        Before discovery has run (a standalone call) everything is allowed; after it, only
        contracts the catalogue kept, so skipped TradFi and non-trading contracts never
        reach analytics.
        """
        return not self._targets or symbol in self._targets

    @staticmethod
    def _is_dated(symbol: str) -> bool:
        """Return whether a symbol names a dated contract (``BTCUSDT_261225``)."""
        return "_" in symbol and not symbol.endswith("_PERP")

    def _class_for(self, product: str, symbol: str) -> str:
        kind = "linear" if product == "usd_m" else "inverse"
        return f"{kind}-{'future' if self._is_dated(symbol) else 'perp'}"

    @staticmethod
    def _filter(filters: Any, filter_type: str, field: str) -> float | None:
        """Read one value out of Binance's symbol ``filters`` array."""
        if not isinstance(filters, list):
            return None
        for entry in filters:
            if isinstance(entry, dict) and entry.get("filterType") == filter_type:
                return parse_positive(entry.get(field))
        return None

    # ----------------------------------------------------------- discovery

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate trading USD-M and COIN-M contracts and USD-M funding intervals."""
        await self._load_funding_intervals()
        instruments: list[RawInstrument] = []
        for product in ("usd_m", "coin_m"):
            self.refresh_receive_time()
            path = self._url(product, "exchangeInfo")
            payload = await self.client.get_json(path, operation=f"exchange-info:{product}")
            body = self.require_mapping(payload, path=path)
            for row in self.require_list(body, path=path, key="symbols"):
                parsed = self._parse_instrument(row, product)
                if parsed is not None:
                    instruments.append(parsed)
                    self._targets[parsed.symbol_native] = (product, parsed.instrument_class)
        return instruments

    async def _load_funding_intervals(self) -> None:
        """Read per-symbol funding intervals for USD-M perpetuals.

        A failure here must not cost the whole catalogue, so it is recorded and the
        affected instruments simply carry no interval.
        """
        path = self._url("usd_m", "fundingInfo")
        self.refresh_receive_time()
        try:
            payload = await self.client.get_json(path, operation="funding-info")
            rows = self.require_list(payload, path=path)
        except (AtlasError, SchemaMismatch):
            self.note_unknown_enum("fundingInfo", "unavailable")
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            hours = parse_positive(row.get("fundingIntervalHours"))
            if isinstance(row.get("symbol"), str) and hours is not None:
                self._funding_hours[row["symbol"]] = hours

    def _parse_instrument(self, row: Any, product: str) -> RawInstrument | None:
        """Turn one ``exchangeInfo`` row into a :class:`RawInstrument`, or skip it."""
        if not isinstance(row, dict):
            raise SchemaMismatch("symbol row was not an object", venue=self.slug)
        symbol, base, quote = row.get("symbol"), row.get("baseAsset"), row.get("quoteAsset")
        if not (isinstance(symbol, str) and isinstance(base, str) and isinstance(quote, str)):
            raise SchemaMismatch(
                "symbol row lacked symbol, baseAsset or quoteAsset", venue=self.slug
            )

        # USD-M calls the field `status`; COIN-M calls it `contractStatus`.
        status = str(row.get("status") or row.get("contractStatus") or "")
        if status != "TRADING":
            # SETTLING, PENDING_TRADING and CLOSE are real states, none of them a live market.
            return None

        contract_kind = str(row.get("contractType", ""))
        if contract_kind in _SKIPPED_CONTRACT_TYPES:
            return None
        instrument_type = _CONTRACT_TYPES.get(contract_kind)
        if instrument_type is None:
            self.note_unknown_enum("contractType", contract_kind)
            return None

        expiry = None
        if instrument_type == "future":
            expiry = plausible_contract_time_ms(parse_float(row.get("deliveryDate")))
            if expiry is None:
                return None

        inverse = product == "coin_m"
        filters = row.get("filters")
        margin = row.get("marginAsset")
        contract_size = parse_positive(row.get("contractSize")) if inverse else 1.0
        if contract_size is None:
            # An inverse contract without a size cannot be converted, so it is not emitted.
            return None

        funding_hours = None
        if instrument_type == "perpetual":
            funding_hours = _COIN_M_FUNDING_HOURS if inverse else self._funding_hours.get(symbol)

        return RawInstrument(
            venue_slug=self.slug,
            symbol_native=symbol,
            instrument_class=self._class_for(product, symbol),
            instrument_type=instrument_type,
            base_symbol_native=base,
            quote_symbol_native=quote,
            settlement_symbol_native=margin if isinstance(margin, str) and margin else None,
            contract_type="inverse" if inverse else "linear",
            contract_multiplier=contract_size,
            # An inverse contract is worth `contractSize` units of the quote (USD).
            contract_value_symbol_native=quote if inverse else base,
            tick_size=self._filter(filters, "PRICE_FILTER", "tickSize"),
            quantity_step=self._filter(filters, "LOT_SIZE", "stepSize"),
            minimum_quantity=self._filter(filters, "LOT_SIZE", "minQty"),
            minimum_notional=self._filter(filters, "MIN_NOTIONAL", "notional"),
            expiry=expiry,
            funding_interval_hours=funding_hours,
            funding_semantics="relative_per_interval" if instrument_type == "perpetual" else None,
            open_interest_unit=(
                OpenInterestUnit.CONTRACTS.value if inverse else OpenInterestUnit.BASE_ASSET.value
            ),
            active=True,
            listing_time=plausible_contract_time_ms(parse_float(row.get("onboardDate"))),
        )

    # -------------------------------------------------------------- quotes

    async def _bulk(self, product: str, endpoint: str, operation: str) -> list[Any]:
        """Fetch one bulk endpoint of one product and return its row list."""
        self.refresh_receive_time()
        path = self._url(product, endpoint)
        payload = await self.client.get_json(path, operation=f"{operation}:{product}")
        return self.require_list(payload, path=path)

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch 24-hour statistics and best bid/ask for every contract: two bulk requests each."""
        tickers: list[RawTicker] = []
        for product in ("usd_m", "coin_m"):
            stats = await self._bulk(product, "ticker/24hr", "ticker-24hr")
            books = await self._bulk(product, "ticker/bookTicker", "book-ticker")
            by_symbol = {
                row["symbol"]: row
                for row in books
                if isinstance(row, dict) and isinstance(row.get("symbol"), str)
            }
            inverse = product == "coin_m"
            self._turnover_loaded.add(product)
            for row in stats:
                if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
                    continue
                symbol = row["symbol"]
                if not self._is_known(symbol):
                    continue
                book = by_symbol.get(symbol, {})
                volume = parse_non_negative(row.get("volume"))
                turnover = (
                    parse_non_negative(row.get("baseVolume"))
                    if inverse
                    else parse_non_negative(row.get("quoteVolume"))
                )
                self._turnover[symbol] = turnover or 0.0
                tickers.append(
                    RawTicker(
                        venue_slug=self.slug,
                        symbol_native=symbol,
                        instrument_class=self._class_for(product, symbol),
                        timing=self.timing(
                            exchange_event_time=plausible_epoch_ms(
                                parse_float(book.get("time") or row.get("closeTime"))
                            )
                        ),
                        last_price=parse_positive(row.get("lastPrice")),
                        bid_price=parse_positive(book.get("bidPrice")),
                        ask_price=parse_positive(book.get("askPrice")),
                        bid_size=parse_non_negative(book.get("bidQty")),
                        ask_size=parse_non_negative(book.get("askQty")),
                        open_24h=parse_positive(row.get("openPrice")),
                        high_24h=parse_positive(row.get("highPrice")),
                        low_24h=parse_positive(row.get("lowPrice")),
                        # COIN-M: `volume` is contracts and `baseVolume` is the base coin.
                        base_volume_24h=turnover if inverse else volume,
                        quote_volume_24h=None if inverse else turnover,
                        contract_volume_24h=volume if inverse else None,
                        trade_count_24h=(
                            int(count) if (count := parse_non_negative(row.get("count"))) else None
                        ),
                    )
                )
        return tickers

    async def fetch_derivatives(self) -> list[RawDerivativeObservation]:
        """Fetch mark, index and funding in bulk, plus sampled open interest."""
        observations: list[RawDerivativeObservation] = []
        for product in ("usd_m", "coin_m"):
            inverse = product == "coin_m"
            rows = await self._bulk(product, "premiumIndex", "premium-index")
            by_symbol: dict[str, RawDerivativeObservation] = {}
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
                    continue
                symbol = row["symbol"]
                if not self._is_known(symbol):
                    continue
                perpetual = not self._is_dated(symbol)
                interval = (
                    None
                    if not perpetual
                    else (_COIN_M_FUNDING_HOURS if inverse else self._funding_hours.get(symbol))
                )
                by_symbol[symbol] = RawDerivativeObservation(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    instrument_class=self._class_for(product, symbol),
                    timing=self.timing(
                        exchange_event_time=plausible_epoch_ms(parse_float(row.get("time")))
                    ),
                    mark_price=parse_positive(row.get("markPrice")),
                    index_price=parse_positive(row.get("indexPrice")),
                    funding_rate_raw=parse_float(row.get("lastFundingRate")) if perpetual else None,
                    funding_interval_hours=interval,
                    next_funding_time=(
                        plausible_contract_time_ms(parse_float(row.get("nextFundingTime")))
                        if perpetual
                        else None
                    ),
                )

            sampled = await self._sample_open_interest(product, by_symbol)
            for symbol, observation in by_symbol.items():
                value = sampled.get(symbol)
                if value is not None:
                    observation = observation.model_copy(
                        update={
                            "open_interest_raw": value,
                            "open_interest_unit": (
                                OpenInterestUnit.CONTRACTS
                                if inverse
                                else OpenInterestUnit.BASE_ASSET
                            ),
                            # USD-M reports base units directly. COIN-M reports contracts, which
                            # Atlas converts with the instrument's contract size.
                            "open_interest_base": None if inverse else value,
                        }
                    )
                observations.append(observation)
        return observations

    async def _sample_open_interest(
        self, product: str, observations: dict[str, RawDerivativeObservation]
    ) -> dict[str, float]:
        """Request open interest for the highest-turnover contracts of one product."""
        if product not in self._turnover_loaded:
            # Standalone call (no ticker pass before it): rank from one bulk request.
            await self._load_turnover(product)
        ranked = sorted(
            observations,
            key=lambda symbol: (-self._turnover.get(symbol, 0.0), symbol),
        )
        chosen = ranked[: _OPEN_INTEREST_SAMPLE[product]]

        async def one(symbol: str) -> tuple[str, float] | None:
            path = self._url(product, "openInterest")
            self.refresh_receive_time()
            try:
                payload = await self.client.get_json(
                    path, params={"symbol": symbol}, operation=f"open-interest:{product}"
                )
            except AtlasError:
                # One symbol failing costs that symbol's open interest only.
                return None
            body = self.require_mapping(payload, path=path)
            value = parse_non_negative(body.get("openInterest"))
            if value is None:
                return None
            return symbol, value

        results = await asyncio.gather(*(one(symbol) for symbol in chosen))
        return {item[0]: item[1] for item in results if item is not None}

    async def _load_turnover(self, product: str) -> None:
        """Populate turnover for ranking from the 24-hour bulk endpoint of one product."""
        self._turnover_loaded.add(product)
        inverse = product == "coin_m"
        for row in await self._bulk(product, "ticker/24hr", "ticker-24hr:rank"):
            if isinstance(row, dict) and isinstance(row.get("symbol"), str):
                key = "baseVolume" if inverse else "quoteVolume"
                self._turnover[row["symbol"]] = parse_non_negative(row.get(key)) or 0.0

    # --------------------------------------------------------- order books

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one order-book snapshot for a USD-M or COIN-M contract."""
        product, instrument_class = self._targets.get(
            symbol_native, ("coin_m" if "USD_" in symbol_native else "usd_m", None)
        )
        limit = next((value for value in _ALLOWED_DEPTHS if value >= depth), _ALLOWED_DEPTHS[-1])
        path = self._url(product, "depth")
        self.refresh_receive_time()
        payload = await self.client.get_json(
            path, params={"symbol": symbol_native, "limit": limit}, operation=f"depth:{product}"
        )
        book = self.require_mapping(payload, path=path)
        bids, bids_truncated = self.build_levels(
            self.require_list(book, path=path, key="bids"), depth=depth, descending=True
        )
        asks, asks_truncated = self.build_levels(
            self.require_list(book, path=path, key="asks"), depth=depth, descending=False
        )
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            instrument_class=instrument_class or self._class_for(product, symbol_native),
            timing=self.timing(
                exchange_event_time=plausible_epoch_ms(parse_float(book.get("T") or book.get("E")))
            ),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated or len(bids) >= limit,
        )

    async def server_time_ms(self) -> int | None:
        """Fetch Binance's USD-M server clock for skew measurement."""
        path = self._url("usd_m", "time")
        payload = await self.client.get_json(path, operation="time")
        body = self.require_mapping(payload, path=path)
        return plausible_epoch_ms(parse_float(body.get("serverTime")))
