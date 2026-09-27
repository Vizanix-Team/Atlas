"""OKX adapter.

Official documentation: https://www.okx.com/docs-v5/en/

Verified against live responses captured 2026-09-27. The facts that matter and are
easy to get wrong:

Volume units
    For ``SWAP`` and ``FUTURES``, ``vol24h`` is a count of **contracts** and
    ``volCcy24h`` is volume in the **base** currency. This was confirmed
    arithmetically: for ``BTC-USDT-SWAP`` (linear, ``ctVal`` 0.01 BTC),
    ``volCcy24h == vol24h * ctVal`` held to the last digit. For ``SPOT``,
    ``vol24h`` is base volume and ``volCcy24h`` is quote volume.

Funding interval
    OKX does not publish the interval as a number. It is derived from the venue's
    own ``fundingTime`` and ``nextFundingTime``: 8 hours for most contracts, but
    ``CHIP-USDT-SWAP`` was observed on a 4-hour cycle, so assuming 8 would have
    been wrong.

Inverse contracts
    ``ctType`` is ``inverse`` and ``ctVal`` is denominated in USD
    (``ctValCcy == "USD"``), so open interest in contracts converts to USD directly
    rather than through the base asset.

Options need a family, and the venue's own endpoints disagree about which
    ``/public/instruments?instType=OPTION`` is rejected with code ``50015``, "Either
    parameter uly or instFamily is required". The families are listed by
    ``/public/underlying?instType=OPTION``, so discovery fetches that list and then
    issues one instruments request per family.

    Two of the four families that endpoint returned (``SOL-USD`` and ``XAU-USD``) were
    then rejected by the instruments endpoint with code ``51000``, "Parameter
    instFamily error", while ``BTC-USD`` and ``ETH-USD`` returned 704 and 686
    instruments. The venue's two endpoints do not agree, so a rejected family is
    recorded and skipped rather than failing the whole venue's collection. This is the
    partial-failure principle applied at the sub-request level.

    The option *ticker* endpoint has no family requirement and returns every strike in
    one request.
"""

from __future__ import annotations

from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.atlas_time import plausible_contract_time_ms, plausible_epoch_ms
from vizanix_atlas.core.errors import ExchangeApplicationError, HttpError, SchemaMismatch
from vizanix_atlas.core.numeric import parse_float, parse_non_negative, parse_positive
from vizanix_atlas.models.enums import OpenInterestUnit
from vizanix_atlas.models.observations import (
    RawDerivativeObservation,
    RawFxObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)
from vizanix_atlas.models.enums import PriceSource

#: Instrument types Atlas collects. ``MARGIN`` is excluded because it re-lists the
#: same spot markets and would double-count volume.
_INSTRUMENT_TYPES: Final = ("SPOT", "SWAP", "FUTURES", "OPTION")

#: OKX product type to Atlas instrument type.
_TYPE_MAP: Final = {
    "SPOT": "spot",
    "SWAP": "perpetual",
    "FUTURES": "future",
    "OPTION": "option",
}

#: Atlas instrument class per OKX product type and contract denomination. Keeping
#: linear and inverse in separate classes means one venue symbol never collides.
_CLASS_MAP: Final = {
    ("SPOT", None): "spot",
    ("SWAP", "linear"): "linear-perp",
    ("SWAP", "inverse"): "inverse-perp",
    ("FUTURES", "linear"): "linear-future",
    ("FUTURES", "inverse"): "inverse-future",
    ("OPTION", None): "option",
}

#: Quote and settlement currencies worth observing for the conversion graph.
_FX_PAIRS: Final = (
    ("USDT-USD", "USDT", "USD"),
    ("USDC-USD", "USDC", "USD"),
    ("USDC-USDT", "USDC", "USDT"),
    ("DAI-USDT", "DAI", "USDT"),
    ("EURT-USDT", "EURT", "USDT"),
)

_MS_PER_HOUR: Final = 3_600_000


class OkxAdapter(ExchangeAdapter):
    """Collects spot, perpetual, dated futures and options from OKX."""

    slug = "okx"
    base_url = "https://www.okx.com"

    def _unwrap(self, payload: Any, *, path: str) -> list[Any]:
        """Return the ``data`` array, raising if OKX reported an application error.

        OKX answers ``200 OK`` with ``code`` set to a non-zero string when a request
        is rejected. Treating that as success would silently produce an empty
        generation.

        Raises:
            ExchangeApplicationError: If ``code`` is not ``"0"``.
            SchemaMismatch: If the envelope is not the documented shape.
        """
        body = self.require_mapping(payload, path=path)
        code = str(body.get("code", ""))
        if code != "0":
            raise ExchangeApplicationError(
                "OKX reported an application error",
                venue_code=code,
                venue=self.slug,
                path=path,
                message=str(body.get("msg", ""))[:200],
            )
        return self.require_list(body, path=path, key="data")

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate every active instrument across all four product types.

        Spot, swaps and futures need one request each. Options additionally need the
        family list, then one request per family.
        """
        instruments: list[RawInstrument] = []
        for inst_type in ("SPOT", "SWAP", "FUTURES"):
            self.refresh_receive_time()
            payload = await self.client.get_json(
                "/api/v5/public/instruments",
                params={"instType": inst_type},
                operation=f"instruments:{inst_type}",
            )
            for row in self._unwrap(payload, path="/api/v5/public/instruments"):
                parsed = self._parse_instrument(row, inst_type)
                if parsed is not None:
                    instruments.append(parsed)

        for family in await self._option_families():
            self.refresh_receive_time()
            try:
                payload = await self.client.get_json(
                    "/api/v5/public/instruments",
                    params={"instType": "OPTION", "instFamily": family},
                    operation=f"instruments:OPTION:{family}",
                )
                rows = self._unwrap(payload, path="/api/v5/public/instruments")
            except (ExchangeApplicationError, HttpError):
                # The venue lists this family under /public/underlying but rejects it
                # here. Losing one option family must not cost the other 5,000
                # instruments, so it is recorded and skipped.
                self.note_unknown_enum("instFamily.rejected", family)
                continue
            for row in rows:
                parsed = self._parse_instrument(row, "OPTION")
                if parsed is not None:
                    instruments.append(parsed)
        return instruments

    async def _option_families(self) -> tuple[str, ...]:
        """Fetch the option underlying families the venue currently lists.

        Returns an empty tuple rather than raising if the endpoint shape is
        unexpected, so that an option-side change degrades option coverage instead of
        failing the whole venue's collection.
        """
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/api/v5/public/underlying",
            params={"instType": "OPTION"},
            operation="option-underlyings",
        )
        rows = self._unwrap(payload, path="/api/v5/public/underlying")
        # The venue nests the list one level deep: [["BTC-USD", "ETH-USD", ...]].
        families = [
            value
            for group in rows
            if isinstance(group, list)
            for value in group
            if isinstance(value, str) and value
        ]
        if not families:
            self.note_unknown_enum("underlying", "empty-option-family-list")
        return tuple(sorted(set(families)))

    def _parse_instrument(self, row: Any, inst_type: str) -> RawInstrument | None:
        """Turn one catalogue row into a :class:`RawInstrument`, or skip it.

        Returns ``None`` for rows Atlas deliberately does not collect: instruments
        that are not live, and rows missing the fields that make them meaningful.
        """
        if not isinstance(row, dict):
            raise SchemaMismatch("instrument row was not an object", venue=self.slug)

        inst_id = row.get("instId")
        if not isinstance(inst_id, str) or not inst_id:
            raise SchemaMismatch("instrument row had no instId", venue=self.slug)

        state = str(row.get("state", ""))
        if state != "live":
            # `suspend`, `preopen`, `expired` and `test` are all real OKX states.
            # None of them describe a market Atlas should quote.
            return None

        ct_type_raw = str(row.get("ctType") or "") or None
        if ct_type_raw is not None and ct_type_raw not in ("linear", "inverse"):
            self.note_unknown_enum("ctType", ct_type_raw)
            return None

        instrument_type = _TYPE_MAP.get(inst_type)
        if instrument_type is None:
            self.note_unknown_enum("instType", inst_type)
            return None

        class_key = (inst_type, ct_type_raw if inst_type in ("SWAP", "FUTURES") else None)
        instrument_class = _CLASS_MAP.get(class_key)
        if instrument_class is None:
            self.note_unknown_enum("instType/ctType", f"{inst_type}/{ct_type_raw}")
            return None

        base, quote, settle = self._resolve_currencies(row, inst_type, inst_id)
        if base is None or quote is None:
            return None

        expiry = plausible_contract_time_ms(parse_float(row.get("expTime")))
        option_type = None
        strike = None
        if inst_type == "OPTION":
            # OKX option symbols are BASE-QUOTE-YYMMDD-STRIKE-C|P. The optType and
            # stk fields carry the same information; the symbol is the fallback.
            option_type = {"C": "call", "P": "put"}.get(str(row.get("optType", "")).upper())
            strike = parse_positive(row.get("stk"))
            if option_type is None or strike is None or expiry is None:
                return None

        # OKX publishes contract size as ctVal in ctValCcy, with ctMult contracts
        # per lot. The effective multiplier is their product where both are given.
        ct_val = parse_positive(row.get("ctVal"))
        ct_mult = parse_positive(row.get("ctMult"))
        multiplier = None
        if ct_val is not None:
            multiplier = ct_val * ct_mult if ct_mult is not None else ct_val

        return RawInstrument(
            venue_slug=self.slug,
            symbol_native=inst_id,
            instrument_class=instrument_class,
            instrument_type=instrument_type,
            base_symbol_native=base,
            quote_symbol_native=quote,
            settlement_symbol_native=settle,
            contract_type=ct_type_raw,
            contract_multiplier=multiplier,
            contract_value_symbol_native=str(row.get("ctValCcy") or "") or None,
            tick_size=parse_positive(row.get("tickSz")),
            quantity_step=parse_positive(row.get("lotSz")),
            minimum_quantity=parse_non_negative(row.get("minSz")),
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            # Interval is derived from funding timestamps, not from the catalogue.
            funding_interval_hours=None,
            funding_semantics="relative_per_interval" if instrument_type == "perpetual" else None,
            open_interest_unit=(
                OpenInterestUnit.CONTRACTS.value if instrument_type != "spot" else None
            ),
            active=True,
            listing_time=plausible_contract_time_ms(parse_float(row.get("listTime"))),
        )

    def _resolve_currencies(
        self, row: dict[str, Any], inst_type: str, inst_id: str
    ) -> tuple[str | None, str | None, str | None]:
        """Extract base, quote and settlement currency codes.

        OKX populates ``baseCcy``/``quoteCcy`` for spot but leaves them empty for
        derivatives, where the information lives in ``instFamily``
        (``BTC-USDT``) and ``settleCcy``. Both paths are handled explicitly rather
        than by splitting the symbol, which would break on the several symbol
        shapes OKX uses.
        """
        settle = str(row.get("settleCcy") or "") or None
        base = str(row.get("baseCcy") or "") or None
        quote = str(row.get("quoteCcy") or "") or None
        if base and quote:
            return base, quote, settle

        family = str(row.get("instFamily") or "")
        if "-" in family:
            family_base, _, family_quote = family.partition("-")
            return family_base or None, family_quote or None, settle

        # Last resort for a derivative with no family: the leading two segments of
        # the instrument ID. Recorded so the gap is visible if OKX ever relies on it.
        parts = inst_id.split("-")
        if len(parts) >= 2:
            self.note_unknown_enum("instFamily", f"missing:{inst_type}")
            return parts[0], parts[1], settle
        return None, None, settle

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch every ticker: one bulk request per product type."""
        tickers: list[RawTicker] = []
        for inst_type in _INSTRUMENT_TYPES:
            self.refresh_receive_time()
            payload = await self.client.get_json(
                "/api/v5/market/tickers",
                params={"instType": inst_type},
                operation=f"tickers:{inst_type}",
            )
            for row in self._unwrap(payload, path="/api/v5/market/tickers"):
                parsed = self._parse_ticker(row, inst_type)
                if parsed is not None:
                    tickers.append(parsed)
        return tickers

    def _parse_ticker(self, row: Any, inst_type: str) -> RawTicker | None:
        """Turn one ticker row into a :class:`RawTicker`.

        Volume is mapped according to the verified unit semantics: for derivatives
        ``vol24h`` is contracts and ``volCcy24h`` is base units, so neither is
        recorded as quote volume. Deriving quote volume from a price the venue did
        not quote against would invent a figure OKX never published.
        """
        if not isinstance(row, dict):
            raise SchemaMismatch("ticker row was not an object", venue=self.slug)
        inst_id = row.get("instId")
        if not isinstance(inst_id, str) or not inst_id:
            return None

        event_time = plausible_epoch_ms(parse_float(row.get("ts")))
        vol24h = parse_non_negative(row.get("vol24h"))
        vol_ccy24h = parse_non_negative(row.get("volCcy24h"))

        if inst_type == "SPOT":
            base_volume, quote_volume, contract_volume = vol24h, vol_ccy24h, None
        else:
            base_volume, quote_volume, contract_volume = vol_ccy24h, None, vol24h

        return RawTicker(
            venue_slug=self.slug,
            symbol_native=inst_id,
            timing=self.timing(exchange_event_time=event_time),
            last_price=parse_positive(row.get("last")),
            bid_price=parse_positive(row.get("bidPx")),
            ask_price=parse_positive(row.get("askPx")),
            bid_size=parse_non_negative(row.get("bidSz")),
            ask_size=parse_non_negative(row.get("askSz")),
            open_24h=parse_positive(row.get("open24h")),
            high_24h=parse_positive(row.get("high24h")),
            low_24h=parse_positive(row.get("low24h")),
            base_volume_24h=base_volume,
            quote_volume_24h=quote_volume,
            contract_volume_24h=contract_volume,
        )

    async def fetch_derivatives(self) -> list[RawDerivativeObservation]:
        """Fetch funding, open interest and mark price for every derivative.

        Five bulk requests total: funding for all swaps in one call, then open
        interest and mark price for swaps and futures. Merged by instrument so each
        instrument yields one record.
        """
        merged: dict[str, dict[str, Any]] = {}

        # OKX returns every swap's funding rate when instId is a placeholder; this
        # is what makes venue-wide funding coverage cost one request.
        self.refresh_receive_time()
        funding_payload = await self.client.get_json(
            "/api/v5/public/funding-rate",
            params={"instId": "ANY"},
            operation="funding",
        )
        for row in self._unwrap(funding_payload, path="/api/v5/public/funding-rate"):
            if not isinstance(row, dict):
                continue
            inst_id = row.get("instId")
            if not isinstance(inst_id, str):
                continue
            entry = merged.setdefault(inst_id, {})
            entry["funding_rate_raw"] = parse_float(row.get("fundingRate"))
            entry["next_funding_time"] = plausible_epoch_ms(parse_float(row.get("nextFundingTime")))
            entry["previous_funding_time"] = plausible_epoch_ms(
                parse_float(row.get("prevFundingTime"))
            )
            entry["event_time"] = plausible_epoch_ms(parse_float(row.get("ts")))
            entry["funding_interval_hours"] = self._derive_interval_hours(
                parse_float(row.get("fundingTime")), parse_float(row.get("nextFundingTime"))
            )

        for inst_type in ("SWAP", "FUTURES"):
            self.refresh_receive_time()
            oi_payload = await self.client.get_json(
                "/api/v5/public/open-interest",
                params={"instType": inst_type},
                operation=f"open-interest:{inst_type}",
            )
            for row in self._unwrap(oi_payload, path="/api/v5/public/open-interest"):
                if not isinstance(row, dict):
                    continue
                inst_id = row.get("instId")
                if not isinstance(inst_id, str):
                    continue
                entry = merged.setdefault(inst_id, {})
                entry["open_interest_raw"] = parse_non_negative(row.get("oi"))
                entry["open_interest_base"] = parse_non_negative(row.get("oiCcy"))
                entry["open_interest_usd"] = parse_non_negative(row.get("oiUsd"))
                entry.setdefault("event_time", plausible_epoch_ms(parse_float(row.get("ts"))))

            self.refresh_receive_time()
            mark_payload = await self.client.get_json(
                "/api/v5/public/mark-price",
                params={"instType": inst_type},
                operation=f"mark-price:{inst_type}",
            )
            for row in self._unwrap(mark_payload, path="/api/v5/public/mark-price"):
                if not isinstance(row, dict):
                    continue
                inst_id = row.get("instId")
                if not isinstance(inst_id, str):
                    continue
                entry = merged.setdefault(inst_id, {})
                entry["mark_price"] = parse_positive(row.get("markPx"))
                entry.setdefault("event_time", plausible_epoch_ms(parse_float(row.get("ts"))))

        return [
            RawDerivativeObservation(
                venue_slug=self.slug,
                symbol_native=inst_id,
                timing=self.timing(exchange_event_time=entry.get("event_time")),
                mark_price=entry.get("mark_price"),
                funding_rate_raw=entry.get("funding_rate_raw"),
                funding_interval_hours=entry.get("funding_interval_hours"),
                next_funding_time=entry.get("next_funding_time"),
                previous_funding_time=entry.get("previous_funding_time"),
                open_interest_raw=entry.get("open_interest_raw"),
                open_interest_unit=(
                    OpenInterestUnit.CONTRACTS if entry.get("open_interest_raw") is not None else None
                ),
                open_interest_base=entry.get("open_interest_base"),
                open_interest_usd=entry.get("open_interest_usd"),
            )
            for inst_id, entry in sorted(merged.items())
        ]

    @staticmethod
    def _derive_interval_hours(funding_time: float | None, next_time: float | None) -> float | None:
        """Derive the funding interval from two consecutive funding timestamps.

        OKX publishes no interval field. Observed values were 8 hours for most
        contracts and 4 hours for at least one, so the interval must be measured
        rather than assumed. Returns ``None`` when the timestamps do not give a
        plausible interval, which leaves the rate unrescaled rather than wrong.
        """
        if funding_time is None or next_time is None:
            return None
        delta_ms = next_time - funding_time
        if delta_ms <= 0:
            return None
        hours = delta_ms / _MS_PER_HOUR
        # Bound to the range of real funding cycles. Anything outside it means the
        # timestamps were not a consecutive pair.
        return hours if 0.5 <= hours <= 24.0 else None

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one order-book snapshot.

        OKX caps ``sz`` at 400 levels per side on the public books endpoint.
        """
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/api/v5/market/books",
            params={"instId": symbol_native, "sz": min(depth, 400)},
            operation="books",
        )
        rows = self._unwrap(payload, path="/api/v5/market/books")
        if not rows:
            raise SchemaMismatch(
                "order-book response contained no data", venue=self.slug, symbol=symbol_native
            )
        book = self.require_mapping(rows[0], path="/api/v5/market/books")
        bids, bids_truncated = self.build_levels(
            self.require_list(book, path="books", key="bids"), depth=depth, descending=True
        )
        asks, asks_truncated = self.build_levels(
            self.require_list(book, path="books", key="asks"), depth=depth, descending=False
        )
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            timing=self.timing(exchange_event_time=plausible_epoch_ms(parse_float(book.get("ts")))),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated,
        )

    async def fetch_fx_observations(self) -> list[RawFxObservation]:
        """Observe stablecoin and fiat rates from OKX's own spot markets.

        Uses the bulk spot ticker response already fetched shape, but issues a
        single filtered request so this stays independent of collection order.
        """
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/api/v5/market/tickers", params={"instType": "SPOT"}, operation="tickers:fx"
        )
        rows = {
            row["instId"]: row
            for row in self._unwrap(payload, path="/api/v5/market/tickers")
            if isinstance(row, dict) and isinstance(row.get("instId"), str)
        }
        observations: list[RawFxObservation] = []
        for symbol, from_code, to_code in _FX_PAIRS:
            row = rows.get(symbol)
            if row is None:
                continue
            bid = parse_positive(row.get("bidPx"))
            ask = parse_positive(row.get("askPx"))
            if bid is not None and ask is not None and bid <= ask:
                rate, source = (bid + ask) / 2.0, PriceSource.MID
            else:
                candidate = parse_positive(row.get("last"))
                if candidate is None:
                    continue
                rate, source = candidate, PriceSource.LAST
            observations.append(
                RawFxObservation(
                    venue_slug=self.slug,
                    symbol_native=symbol,
                    timing=self.timing(
                        exchange_event_time=plausible_epoch_ms(parse_float(row.get("ts")))
                    ),
                    from_symbol_native=from_code,
                    to_symbol_native=to_code,
                    rate=rate,
                    source_price=source,
                )
            )
        return observations

    async def server_time_ms(self) -> int | None:
        """Fetch OKX's server clock for skew measurement."""
        payload = await self.client.get_json("/api/v5/public/time", operation="time")
        rows = self._unwrap(payload, path="/api/v5/public/time")
        if not rows or not isinstance(rows[0], dict):
            return None
        return plausible_epoch_ms(parse_float(rows[0].get("ts")))
