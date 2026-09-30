"""HTX spot adapter.

Official documentation: https://huobiapi.github.io/docs/spot/v1/en/

Verified 2026-09-27. Two things make HTX different from every other supported venue:

Abbreviated catalogue field names
    ``/v2/settings/common/symbols`` uses short keys: ``bc`` is base currency, ``qc``
    quote currency, ``sc`` symbol code, ``dn`` display name, ``tpp`` price precision,
    ``tap`` amount precision, ``state`` the trading state. They are mapped here so
    nothing above this file has to know them.

Reversed volume field names
    In ``/market/tickers``, ``amount`` is **base** volume and ``vol`` is **quote**
    volume. That is the opposite of the convention most venues use, and getting it
    backwards would inflate reported volume by the asset's price.

The bulk ticker endpoint covers only actively traded symbols (609 of 2,165 catalogue
entries when captured), so a listed-but-quiet market appears in discovery with no
quote, which is the correct representation.
"""

from __future__ import annotations

from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.atlas_time import plausible_contract_time_ms, plausible_epoch_ms
from vizanix_atlas.core.errors import ExchangeApplicationError, SchemaMismatch
from vizanix_atlas.core.numeric import parse_float, parse_int, parse_non_negative, parse_positive
from vizanix_atlas.models.observations import (
    RawFxObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)

_FX_PAIRS: Final = (
    ("usdcusdt", "USDC", "USDT"),
    ("daiusdt", "DAI", "USDT"),
)


class HtxAdapter(ExchangeAdapter):
    """Collects spot markets from HTX."""

    slug = "htx"
    base_url = "https://api.huobi.pro"

    def _unwrap(self, payload: Any, *, path: str, key: str = "data") -> Any:
        """Return the payload body, raising if HTX reported an error.

        HTX answers ``200 OK`` with ``status`` set to ``"error"`` on failure.

        Raises:
            ExchangeApplicationError: If ``status`` is not ``"ok"``.

        """
        body = self.require_mapping(payload, path=path)
        status = str(body.get("status", ""))
        if status and status != "ok":
            raise ExchangeApplicationError(
                "HTX reported an application error",
                venue_code=str(body.get("err-code", status))[:80],
                venue=self.slug,
                path=path,
                message=str(body.get("err-msg", ""))[:200],
            )
        if key not in body:
            raise SchemaMismatch(f"response had no {key} field", venue=self.slug, path=path)
        return body[key]

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate every online symbol from the common settings endpoint."""
        self.refresh_receive_time()
        payload = await self.client.get_json("/v2/settings/common/symbols", operation="symbols")
        rows = self.require_list(
            self._unwrap(payload, path="/v2/settings/common/symbols"), path="symbols"
        )

        instruments: list[RawInstrument] = []
        for row in rows:
            if not isinstance(row, dict):
                raise SchemaMismatch("symbol row was not an object", venue=self.slug)
            # `sc` is the tradeable symbol code; `bc`/`qc` are the currency codes.
            symbol = row.get("sc")
            base = row.get("bc")
            quote = row.get("qc")
            if not (isinstance(symbol, str) and isinstance(base, str) and isinstance(quote, str)):
                raise SchemaMismatch("symbol row lacked sc, bc or qc", venue=self.slug)
            state = str(row.get("state", ""))
            if state != "online":
                # `offline`, `suspend`, `pre-online` and `transfer-board` are all real.
                continue

            instruments.append(
                RawInstrument(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    instrument_class="spot",
                    instrument_type="spot",
                    # `bcdn`/`qcdn` are the display forms of the same codes; the
                    # lowercase `bc`/`qc` are canonical on HTX.
                    base_symbol_native=str(row.get("bcdn") or base),
                    quote_symbol_native=str(row.get("qcdn") or quote),
                    tick_size=_places_to_step(row.get("tpp")),
                    quantity_step=_places_to_step(row.get("tap")),
                    minimum_quantity=parse_non_negative(row.get("minoa")),
                    minimum_notional=parse_non_negative(row.get("minov")),
                    active=True,
                    listing_time=plausible_contract_time_ms(parse_float(row.get("toa"))),
                )
            )
        return instruments

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch every actively traded ticker in one request."""
        self.refresh_receive_time()
        payload = await self.client.get_json("/market/tickers", operation="tickers")
        body = self.require_mapping(payload, path="/market/tickers")
        # Only a response-level timestamp exists, so it is the server time.
        server_time = plausible_epoch_ms(parse_float(body.get("ts")))
        rows = self.require_list(self._unwrap(payload, path="/market/tickers"), path="tickers")

        tickers: list[RawTicker] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            if not isinstance(symbol, str):
                continue
            tickers.append(
                RawTicker(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    timing=self.timing(exchange_server_time=server_time),
                    last_price=parse_positive(row.get("close")),
                    bid_price=parse_positive(row.get("bid")),
                    ask_price=parse_positive(row.get("ask")),
                    bid_size=parse_non_negative(row.get("bidSize")),
                    ask_size=parse_non_negative(row.get("askSize")),
                    open_24h=parse_positive(row.get("open")),
                    high_24h=parse_positive(row.get("high")),
                    low_24h=parse_positive(row.get("low")),
                    # HTX inverts the usual naming: `amount` is base, `vol` is quote.
                    base_volume_24h=parse_non_negative(row.get("amount")),
                    quote_volume_24h=parse_non_negative(row.get("vol")),
                    trade_count_24h=parse_int(row.get("count")),
                )
            )
        return tickers

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one order-book snapshot.

        ``step0`` is the unaggregated book. HTX caps ``depth`` at 20 for that step, so
        a deeper request is still capped and the result is marked truncated.
        """
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/market/depth",
            params={"symbol": symbol_native, "type": "step0", "depth": 20},
            operation="depth",
        )
        book = self.require_mapping(
            self._unwrap(payload, path="/market/depth", key="tick"), path="depth"
        )
        bids, bids_truncated = self.build_levels(
            self.require_list(book, path="depth", key="bids"), depth=depth, descending=True
        )
        asks, asks_truncated = self.build_levels(
            self.require_list(book, path="depth", key="asks"), depth=depth, descending=False
        )
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            timing=self.timing(exchange_event_time=plausible_epoch_ms(parse_float(book.get("ts")))),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated or depth > 20,
        )

    async def fetch_fx_observations(self) -> list[RawFxObservation]:
        """Observe stablecoin rates from HTX's own spot markets."""
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
        """Fetch HTX's server clock for skew measurement."""
        payload = await self.client.get_json("/v1/common/timestamp", operation="time")
        return plausible_epoch_ms(parse_float(self._unwrap(payload, path="/v1/common/timestamp")))


def _places_to_step(value: Any) -> float | None:
    """Convert a decimal-places count into a step size."""
    places = parse_float(value)
    if places is None or places < 0 or places > 18 or not float(places).is_integer():
        return None
    return 10.0 ** -int(places)
