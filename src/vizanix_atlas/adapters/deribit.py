"""Deribit adapter.

Official documentation: https://docs.deribit.com/

Verified 2026-09-27. Deribit is Atlas's option venue and its most instructive one:

Nulls mean no trades
    ``get_book_summary_by_currency`` returns ``null`` for ``last``, ``high`` and
    ``low`` on an option with no trades in the window. Reading those as zero would
    turn "untraded" into "traded at zero", which is exactly the collapse Atlas
    refuses to make.

Combos are not outright instruments
    ``future_combo`` and ``option_combo`` are calendar and option spreads. They are
    excluded from collection because aggregating a spread's volume alongside the
    outright contracts it is built from double-counts activity.

Funding is already 8-hourly
    ``funding_8h`` is published directly, so Atlas stores it with an interval of 8
    hours and performs no rescaling. It appears in the bulk
    ``get_book_summary_by_currency`` response for perpetuals, so funding costs no
    extra requests at all.

Inverse open interest is in USD
    ``BTC-PERPETUAL`` reported ``open_interest`` of 863,495,490 against a contract
    size of 10 USD, so the figure is notional USD rather than a contract count.

Request budget
    ``/public/get_instruments?currency=any`` returns the entire catalogue (5,892
    instruments when captured) in one request, so discovery costs one call rather than
    one per currency. ``get_book_summary_by_currency`` rejects ``any``, so quotes are
    fetched per settlement currency instead, of which there were exactly three across
    the whole catalogue when captured (BTC, ETH and USDC). Base currency is the wrong
    key for that endpoint: there were over 140 of them and it rejects most.
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
    RawInstrument,
    RawOrderBook,
    RawTicker,
)

#: Deribit accepts this in place of a currency on the instruments endpoint, which
#: returns the whole catalogue in one request.
_ANY_CURRENCY: Final = "any"

#: Used only if discovery has not run, so that a quote fetch on its own still works.
_FALLBACK_CURRENCIES: Final = ("BTC", "ETH")

#: Instrument kinds Atlas collects. Combos are deliberately absent.
_COLLECTED_KINDS: Final = frozenset({"future", "option", "spot"})

#: Deribit funding is published as an 8-hour equivalent.
_FUNDING_INTERVAL_HOURS: Final = 8.0


class DeribitAdapter(ExchangeAdapter):
    """Collects perpetuals, dated futures, options and spot from Deribit."""

    slug = "deribit"
    base_url = "https://www.deribit.com"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: Currencies discovery actually found instruments on. Learned rather than
        #: assumed, so quote fetching does not iterate currencies with no markets.
        self._quote_currencies: tuple[str, ...] = ()

    def _unwrap(self, payload: Any, *, path: str) -> Any:
        """Return the JSON-RPC ``result``, raising if Deribit reported an error.

        Deribit speaks JSON-RPC over HTTP, so a failure arrives as an ``error``
        member with a ``200 OK`` status.

        Raises:
            ExchangeApplicationError: If the response carries an ``error``.

        """
        body = self.require_mapping(payload, path=path)
        error = body.get("error")
        if isinstance(error, dict):
            raise ExchangeApplicationError(
                "Deribit reported an application error",
                venue_code=str(error.get("code", ""))[:40],
                venue=self.slug,
                path=path,
                message=str(error.get("message", ""))[:200],
            )
        if "result" not in body:
            raise SchemaMismatch("response had no result member", venue=self.slug, path=path)
        return body["result"]

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate the entire active catalogue in one request.

        Also records which currencies carry instruments, so that the quote fetch loops
        over those rather than over every currency the venue supports.
        """
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/api/v2/public/get_instruments",
            params={"currency": _ANY_CURRENCY},
            operation="instruments",
        )
        rows = self._unwrap(payload, path="/api/v2/public/get_instruments")

        instruments: list[RawInstrument] = []
        currencies: set[str] = set()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            parsed = self._parse_instrument(row)
            if parsed is None:
                continue
            instruments.append(parsed)
            # get_book_summary_by_currency is keyed by settlement currency, not by base
            # currency. Only three settlement currencies exist across the whole
            # catalogue (BTC, ETH and USDC when captured), whereas there are over 140
            # base currencies, most of which that endpoint rejects outright.
            settlement = row.get("settlement_currency")
            if isinstance(settlement, str) and settlement:
                currencies.add(settlement)
        if currencies:
            self._quote_currencies = tuple(sorted(currencies))
        return instruments

    async def _currencies_to_quote(self) -> tuple[str, ...]:
        """Return the currencies to request book summaries for.

        Prefers what discovery observed. Falls back to BTC and ETH only when quotes are
        fetched without discovery having run, so a partial call still returns something.
        """
        if self._quote_currencies:
            return self._quote_currencies
        return _FALLBACK_CURRENCIES

    def _parse_instrument(self, row: dict[str, Any]) -> RawInstrument | None:
        """Turn one Deribit instrument into a :class:`RawInstrument`, or skip it."""
        name = row.get("instrument_name")
        if not isinstance(name, str) or not name:
            raise SchemaMismatch("instrument row had no instrument_name", venue=self.slug)
        if not row.get("is_active", False) or str(row.get("state", "")) != "open":
            return None

        kind = str(row.get("kind", ""))
        if kind not in _COLLECTED_KINDS:
            if kind not in ("future_combo", "option_combo"):
                # An unexpected kind is schema drift; the two combo kinds are a
                # deliberate exclusion, so they are not counted as drift.
                self.note_unknown_enum("kind", kind)
            return None

        base = row.get("base_currency")
        quote = row.get("counter_currency") or row.get("quote_currency")
        if not isinstance(base, str) or not isinstance(quote, str):
            raise SchemaMismatch("instrument row lacked currency fields", venue=self.slug)

        settlement = row.get("settlement_currency")
        # `reversed` is Deribit's term for an inverse contract.
        is_inverse = str(row.get("instrument_type", "")) == "reversed"
        expiry = plausible_contract_time_ms(parse_float(row.get("expiration_timestamp")))
        period = str(row.get("settlement_period", ""))

        if kind == "spot":
            instrument_type, instrument_class, expiry = "spot", "spot", None
        elif kind == "option":
            instrument_type = "option"
            instrument_class = "option"
            if expiry is None:
                return None
        elif period == "perpetual":
            instrument_type = "perpetual"
            instrument_class = f"{'inverse' if is_inverse else 'linear'}-perp"
            # Deribit gives perpetuals a sentinel expiry far in the future
            # (the year 2999); a perpetual must carry no expiry in Atlas.
            expiry = None
        else:
            instrument_type = "future"
            instrument_class = f"{'inverse' if is_inverse else 'linear'}-future"
            if expiry is None:
                return None

        option_type = None
        strike = None
        if kind == "option":
            option_type = str(row.get("option_type", "")).lower() or None
            if option_type not in ("call", "put"):
                self.note_unknown_enum("option_type", str(row.get("option_type", "")))
                return None
            strike = parse_positive(row.get("strike"))
            if strike is None:
                return None

        is_spot = instrument_type == "spot"
        return RawInstrument(
            venue_slug=self.slug,
            symbol_native=name,
            instrument_class=instrument_class,
            instrument_type=instrument_type,
            base_symbol_native=base,
            quote_symbol_native=quote,
            settlement_symbol_native=(
                settlement if isinstance(settlement, str) and not is_spot else None
            ),
            contract_type=None if is_spot else ("inverse" if is_inverse else "linear"),
            contract_multiplier=None if is_spot else parse_positive(row.get("contract_size")),
            contract_value_symbol_native=None if is_spot else quote,
            tick_size=parse_positive(row.get("tick_size")),
            minimum_quantity=parse_non_negative(row.get("min_trade_amount")),
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            funding_interval_hours=(
                _FUNDING_INTERVAL_HOURS if instrument_type == "perpetual" else None
            ),
            funding_semantics=("relative_per_interval" if instrument_type == "perpetual" else None),
            open_interest_unit=(
                None
                if is_spot
                # Inverse contracts report notional USD; linear ones report the
                # base asset.
                else (
                    OpenInterestUnit.USD.value if is_inverse else OpenInterestUnit.BASE_ASSET.value
                )
            ),
            active=True,
            listing_time=plausible_contract_time_ms(parse_float(row.get("creation_timestamp"))),
        )

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch quotes for every instrument, one request per currency.

        ``get_book_summary_by_currency`` covers every instrument on a currency,
        including all option strikes, in one response.
        """
        tickers: list[RawTicker] = []
        for row, name in await self._summary_rows():
            tickers.append(
                RawTicker(
                    venue_slug=self.slug,
                    symbol_native=name,
                    timing=self.timing(
                        exchange_server_time=plausible_epoch_ms(
                            parse_float(row.get("creation_timestamp"))
                        )
                    ),
                    # These are genuinely null for an untraded option. Passing them
                    # through as null is the whole point.
                    last_price=parse_positive(row.get("last")),
                    bid_price=parse_positive(row.get("bid_price")),
                    ask_price=parse_positive(row.get("ask_price")),
                    high_24h=parse_positive(row.get("high")),
                    low_24h=parse_positive(row.get("low")),
                    base_volume_24h=parse_non_negative(row.get("volume")),
                    # volume_usd is a USD figure rather than a quote-currency one, so
                    # it is not recorded as quote volume for an inverse contract whose
                    # quote currency is the base asset.
                    quote_volume_24h=None,
                )
            )
        return tickers

    async def fetch_derivatives(self) -> list[RawDerivativeObservation]:
        """Fetch mark price, funding, open interest and implied volatility.

        Everything comes from the same bulk book-summary response used for quotes, so
        derivative observations cost no additional requests.
        """
        observations: list[RawDerivativeObservation] = []
        for row, name in await self._summary_rows():
            open_interest = parse_non_negative(row.get("open_interest"))
            # funding_8h is present only on perpetuals, and is already the 8-hour
            # equivalent, so no rescaling is applied.
            funding_8h = parse_float(row.get("funding_8h"))
            observations.append(
                RawDerivativeObservation(
                    venue_slug=self.slug,
                    symbol_native=name,
                    timing=self.timing(
                        exchange_server_time=plausible_epoch_ms(
                            parse_float(row.get("creation_timestamp"))
                        )
                    ),
                    mark_price=parse_positive(row.get("mark_price")),
                    index_price=parse_positive(row.get("estimated_delivery_price")),
                    funding_rate_raw=funding_8h,
                    funding_interval_hours=(
                        _FUNDING_INTERVAL_HOURS if funding_8h is not None else None
                    ),
                    open_interest_raw=open_interest,
                    # The unit depends on the instrument's contract type, which
                    # normalisation resolves from the instrument record rather than
                    # being guessed per observation here.
                    open_interest_unit=None,
                    mark_iv=parse_non_negative(row.get("mark_iv")),
                    bid_iv=parse_non_negative(row.get("bid_iv")),
                    ask_iv=parse_non_negative(row.get("ask_iv")),
                    underlying_price=parse_positive(row.get("underlying_price")),
                )
            )
        return observations

    @staticmethod
    def _is_combo(instrument_name: str) -> bool:
        """Return whether a name identifies a spread rather than an outright instrument.

        Deribit names calendar and option spreads with an ``-FS-`` or ``-OS-`` infix.
        Atlas excludes combos from discovery, so their quotes have no instrument to attach
        to; skipping them here keeps thousands of deliberate exclusions out of the
        quarantine log, where they would drown the real data-quality events.
        """
        return "-FS-" in instrument_name or "-OS-" in instrument_name

    async def _summary_rows(self) -> list[tuple[dict[str, Any], str]]:
        """Fetch book summaries for every settlement currency, excluding combos."""
        rows: list[tuple[dict[str, Any], str]] = []
        seen: set[str] = set()
        for currency in await self._currencies_to_quote():
            self.refresh_receive_time()
            payload = await self.client.get_json(
                "/api/v2/public/get_book_summary_by_currency",
                params={"currency": currency},
                operation=f"book-summary:{currency}",
            )
            result = self._unwrap(payload, path="/api/v2/public/get_book_summary_by_currency")
            for row in result if isinstance(result, list) else []:
                if not isinstance(row, dict):
                    continue
                name = row.get("instrument_name")
                # A cross-settled instrument appears under more than one currency, so
                # the name is deduplicated to avoid double-counting its volume.
                if isinstance(name, str) and name not in seen and not self._is_combo(name):
                    seen.add(name)
                    rows.append((row, name))
        return rows

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one order-book snapshot."""
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/api/v2/public/get_order_book",
            params={"instrument_name": symbol_native, "depth": min(depth, 1000)},
            operation="order-book",
        )
        book = self.require_mapping(
            self._unwrap(payload, path="/api/v2/public/get_order_book"), path="get_order_book"
        )
        bids, bids_truncated = self.build_levels(
            self.require_list(book, path="get_order_book", key="bids"),
            depth=depth,
            descending=True,
        )
        asks, asks_truncated = self.build_levels(
            self.require_list(book, path="get_order_book", key="asks"),
            depth=depth,
            descending=False,
        )
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            timing=self.timing(
                exchange_event_time=plausible_epoch_ms(parse_float(book.get("timestamp")))
            ),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated,
        )

    async def server_time_ms(self) -> int | None:
        """Fetch Deribit's server clock for skew measurement."""
        payload = await self.client.get_json("/api/v2/public/get_time", operation="time")
        return plausible_epoch_ms(
            parse_float(self._unwrap(payload, path="/api/v2/public/get_time"))
        )
