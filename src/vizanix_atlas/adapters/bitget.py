"""Bitget adapter.

Official documentation: https://www.bitget.com/api-doc/common/intro

Verified 2026-09-27. Bitget is unusually efficient to collect from: one request per
product type returns quotes, mark price, index price, funding rate and open interest
together, so the whole venue costs a handful of requests.

Units: ``baseVolume`` is base-asset volume and ``quoteVolume`` is quote-currency
volume for both spot and derivatives. ``holdingAmount`` is open interest in **base
asset units**, not contracts, which is why the instrument declares
``open_interest_unit`` as ``base_asset`` and no contract multiplier is applied.
``fundInterval`` is published per contract, in hours.
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

#: Derivative product types and the Atlas contract denomination each implies.
#: USDT-margined and USDC-margined contracts are linear; coin-margined are inverse.
_PRODUCT_TYPES: Final = (
    ("USDT-FUTURES", "linear"),
    ("USDC-FUTURES", "linear"),
    ("COIN-FUTURES", "inverse"),
)

_FX_PAIRS: Final = (
    ("USDCUSDT", "USDC", "USDT"),
    ("DAIUSDT", "DAI", "USDT"),
)


class BitgetAdapter(ExchangeAdapter):
    """Collects spot and derivative markets from Bitget."""

    slug = "bitget"
    base_url = "https://api.bitget.com"

    def _unwrap(self, payload: Any, *, path: str) -> list[Any]:
        """Return the ``data`` array, raising if Bitget reported an application error.

        Bitget answers ``200 OK`` with a non-``"00000"`` ``code`` when a request is
        rejected.

        Raises:
            ExchangeApplicationError: If ``code`` is not the success value.

        """
        body = self.require_mapping(payload, path=path)
        code = str(body.get("code", ""))
        if code and code != "00000":
            raise ExchangeApplicationError(
                "Bitget reported an application error",
                venue_code=code,
                venue=self.slug,
                path=path,
                message=str(body.get("msg", ""))[:200],
            )
        return self.require_list(body, path=path, key="data")

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate spot symbols and every derivative product type."""
        instruments: list[RawInstrument] = []

        self.refresh_receive_time()
        spot_payload = await self.client.get_json(
            "/api/v2/spot/public/symbols", operation="spot-symbols"
        )
        for row in self._unwrap(spot_payload, path="/api/v2/spot/public/symbols"):
            parsed = self._parse_spot_instrument(row)
            if parsed is not None:
                instruments.append(parsed)

        for product_type, contract_type in _PRODUCT_TYPES:
            self.refresh_receive_time()
            payload = await self.client.get_json(
                "/api/v2/mix/market/contracts",
                params={"productType": product_type},
                operation=f"mix-contracts:{product_type}",
            )
            for row in self._unwrap(payload, path="/api/v2/mix/market/contracts"):
                parsed = self._parse_mix_instrument(row, product_type, contract_type)
                if parsed is not None:
                    instruments.append(parsed)
        return instruments

    def _parse_spot_instrument(self, row: Any) -> RawInstrument | None:
        """Turn one spot symbol row into a :class:`RawInstrument`."""
        if not isinstance(row, dict):
            raise SchemaMismatch("spot symbol row was not an object", venue=self.slug)
        symbol = row.get("symbol")
        base = row.get("baseCoin")
        quote = row.get("quoteCoin")
        if not (isinstance(symbol, str) and isinstance(base, str) and isinstance(quote, str)):
            raise SchemaMismatch(
                "spot symbol row lacked symbol, baseCoin or quoteCoin", venue=self.slug
            )
        if str(row.get("status", "")) != "online":
            return None

        return RawInstrument(
            venue_slug=self.slug,
            symbol_native=symbol,
            instrument_class="spot",
            instrument_type="spot",
            # Bitget preserves the issuer's casing in baseCoin (for example
            # "rCVCO"), which is useful identity evidence, so it is not upper-cased.
            base_symbol_native=base,
            quote_symbol_native=quote,
            tick_size=_precision_to_step(row.get("pricePrecision")),
            quantity_step=_precision_to_step(row.get("quantityPrecision")),
            minimum_quantity=parse_non_negative(row.get("minTradeAmount")),
            minimum_notional=parse_non_negative(row.get("minTradeUSDT")),
            active=True,
            listing_time=plausible_contract_time_ms(parse_float(row.get("openTime"))),
        )

    def _parse_mix_instrument(
        self, row: Any, product_type: str, contract_type: str
    ) -> RawInstrument | None:
        """Turn one derivative contract row into a :class:`RawInstrument`."""
        if not isinstance(row, dict):
            raise SchemaMismatch("mix contract row was not an object", venue=self.slug)
        symbol = row.get("symbol")
        base = row.get("baseCoin")
        quote = row.get("quoteCoin")
        if not (isinstance(symbol, str) and isinstance(base, str) and isinstance(quote, str)):
            raise SchemaMismatch(
                "mix contract row lacked symbol, baseCoin or quoteCoin", venue=self.slug
            )
        if str(row.get("symbolStatus", "")) != "normal":
            return None

        symbol_type = str(row.get("symbolType", ""))
        if symbol_type == "perpetual":
            instrument_type, expiry = "perpetual", None
        elif symbol_type == "delivery":
            instrument_type = "future"
            expiry = plausible_contract_time_ms(parse_float(row.get("deliveryTime")))
            if expiry is None:
                return None
        else:
            self.note_unknown_enum("symbolType", symbol_type)
            return None

        settle_coins = row.get("supportMarginCoins")
        settlement = (
            settle_coins[0]
            if isinstance(settle_coins, list) and settle_coins and isinstance(settle_coins[0], str)
            else (base if contract_type == "inverse" else quote)
        )

        return RawInstrument(
            venue_slug=self.slug,
            symbol_native=symbol,
            instrument_class=f"{contract_type}-{'perp' if instrument_type == 'perpetual' else 'future'}",
            instrument_type=instrument_type,
            base_symbol_native=base,
            quote_symbol_native=quote,
            settlement_symbol_native=settlement,
            contract_type=contract_type,
            contract_multiplier=parse_positive(row.get("sizeMultiplier")),
            contract_value_symbol_native=base,
            tick_size=_precision_to_step(row.get("pricePlace")),
            quantity_step=_precision_to_step(row.get("volumePlace")),
            minimum_quantity=parse_non_negative(row.get("minTradeNum")),
            minimum_notional=parse_non_negative(row.get("minTradeUSDT")),
            expiry=expiry,
            funding_interval_hours=(
                parse_positive(row.get("fundInterval")) if instrument_type == "perpetual" else None
            ),
            funding_semantics=("relative_per_interval" if instrument_type == "perpetual" else None),
            # holdingAmount is reported in base-asset units, so no contract
            # multiplier is involved in normalising it.
            open_interest_unit=OpenInterestUnit.BASE_ASSET.value,
            active=True,
            # `launchTime` was empty on every captured record, so it is not read.
        )

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch spot and derivative tickers: one bulk request each."""
        tickers: list[RawTicker] = []

        self.refresh_receive_time()
        spot_payload = await self.client.get_json(
            "/api/v2/spot/market/tickers", operation="spot-tickers"
        )
        for row in self._unwrap(spot_payload, path="/api/v2/spot/market/tickers"):
            parsed = self._parse_ticker(row, instrument_class="spot")
            if parsed is not None:
                tickers.append(parsed)

        for product_type, _ in _PRODUCT_TYPES:
            self.refresh_receive_time()
            payload = await self.client.get_json(
                "/api/v2/mix/market/tickers",
                params={"productType": product_type},
                operation=f"mix-tickers:{product_type}",
            )
            for row in self._unwrap(payload, path="/api/v2/mix/market/tickers"):
                parsed = self._parse_ticker(row, instrument_class="derivative")
                if parsed is not None:
                    tickers.append(parsed)
        return tickers

    def _parse_ticker(self, row: Any, *, instrument_class: str) -> RawTicker | None:
        """Turn one ticker row into a :class:`RawTicker`.

        Spot and derivative tickers share the same field names on Bitget, and Bitget
        reuses a symbol between its spot pair and its USDT-margined mix contract (both
        "BTCUSDT"), so the caller states which side of the venue this row came from.
        """
        if not isinstance(row, dict):
            raise SchemaMismatch("ticker row was not an object", venue=self.slug)
        symbol = row.get("symbol")
        if not isinstance(symbol, str):
            return None
        return RawTicker(
            venue_slug=self.slug,
            symbol_native=symbol,
            instrument_class=instrument_class,
            timing=self.timing(exchange_event_time=plausible_epoch_ms(parse_float(row.get("ts")))),
            last_price=parse_positive(row.get("lastPr")),
            bid_price=parse_positive(row.get("bidPr")),
            ask_price=parse_positive(row.get("askPr")),
            bid_size=parse_non_negative(row.get("bidSz")),
            ask_size=parse_non_negative(row.get("askSz")),
            open_24h=parse_positive(row.get("open24h") or row.get("open")),
            high_24h=parse_positive(row.get("high24h")),
            low_24h=parse_positive(row.get("low24h")),
            base_volume_24h=parse_non_negative(row.get("baseVolume")),
            quote_volume_24h=parse_non_negative(row.get("quoteVolume")),
        )

    async def fetch_derivatives(self) -> list[RawDerivativeObservation]:
        """Fetch mark, index, funding and open interest from the bulk mix tickers."""
        observations: list[RawDerivativeObservation] = []
        for product_type, _ in _PRODUCT_TYPES:
            self.refresh_receive_time()
            payload = await self.client.get_json(
                "/api/v2/mix/market/tickers",
                params={"productType": product_type},
                operation=f"mix-derivatives:{product_type}",
            )
            for row in self._unwrap(payload, path="/api/v2/mix/market/tickers"):
                if not isinstance(row, dict):
                    continue
                symbol = row.get("symbol")
                if not isinstance(symbol, str):
                    continue
                holding = parse_non_negative(row.get("holdingAmount"))
                observations.append(
                    RawDerivativeObservation(
                        venue_slug=self.slug,
                        symbol_native=symbol,
                        instrument_class="derivative",
                        timing=self.timing(
                            exchange_event_time=plausible_epoch_ms(parse_float(row.get("ts")))
                        ),
                        mark_price=parse_positive(row.get("markPrice")),
                        index_price=parse_positive(row.get("indexPrice")),
                        funding_rate_raw=parse_float(row.get("fundingRate")),
                        open_interest_raw=holding,
                        open_interest_unit=(
                            OpenInterestUnit.BASE_ASSET if holding is not None else None
                        ),
                        # holdingAmount is already in base units, so it doubles as the
                        # base-equivalent open interest with no conversion needed.
                        open_interest_base=holding,
                    )
                )
        return observations

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one spot order-book snapshot."""
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/api/v2/spot/market/orderbook",
            params={"symbol": symbol_native, "type": "step0", "limit": min(depth, 150)},
            operation="orderbook",
        )
        rows: object = self._unwrap(payload, path="/api/v2/spot/market/orderbook")
        book = rows if isinstance(rows, dict) else None
        if book is None:
            # Bitget returns an object under `data` for this endpoint; `_unwrap`
            # requires a list, so the object form is read directly.
            body = self.require_mapping(payload, path="/api/v2/spot/market/orderbook")
            book = self.require_mapping(body, path="orderbook", key="data")
        bids, bids_truncated = self.build_levels(
            self.require_list(book, path="orderbook", key="bids"), depth=depth, descending=True
        )
        asks, asks_truncated = self.build_levels(
            self.require_list(book, path="orderbook", key="asks"), depth=depth, descending=False
        )
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            # This endpoint only ever returns a spot book; declared explicitly so a
            # symbol shared with a mix contract still resolves to the spot instrument.
            instrument_class="spot",
            timing=self.timing(exchange_event_time=plausible_epoch_ms(parse_float(book.get("ts")))),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated,
        )

    async def fetch_fx_observations(self) -> list[RawFxObservation]:
        """Observe stablecoin rates from Bitget's own spot markets."""
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/api/v2/spot/market/tickers", operation="spot-tickers:fx"
        )
        rows = {
            row["symbol"]: row
            for row in self._unwrap(payload, path="/api/v2/spot/market/tickers")
            if isinstance(row, dict) and isinstance(row.get("symbol"), str)
        }
        observations: list[RawFxObservation] = []
        for symbol, from_code, to_code in _FX_PAIRS:
            row = rows.get(symbol)
            if row is None:
                continue
            ticker = self._parse_ticker(row, instrument_class="spot")
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
        """Fetch Bitget's server clock for skew measurement."""
        payload = await self.client.get_json("/api/v2/public/time", operation="time")
        body = self.require_mapping(payload, path="/api/v2/public/time")
        data = body.get("data")
        if isinstance(data, dict):
            return plausible_epoch_ms(parse_float(data.get("serverTime")))
        return None


def _precision_to_step(value: Any) -> float | None:
    """Convert a decimal-places count into a step size.

    Bitget expresses tick and step sizes as a number of decimal places rather than
    as a value, so ``2`` means a step of ``0.01``. Returns ``None`` for anything not
    a plausible precision, rather than a step of 1.
    """
    places = parse_float(value)
    if places is None or places < 0 or places > 18 or not float(places).is_integer():
        return None
    return 10.0 ** -int(places)
