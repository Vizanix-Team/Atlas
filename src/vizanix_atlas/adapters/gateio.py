"""Gate.io adapter.

Official documentation: https://www.gate.com/docs/developers/apiv4/

Verified 2026-09-27. Notable facts:

- ``/futures/usdt/contracts`` already carries ``mark_price``, ``index_price``,
  ``funding_rate`` and ``position_size`` (open interest in contracts), so one
  request covers both discovery and derivative observations for the whole
  settlement currency.
- ``funding_interval`` is published in **seconds** (``28800`` for eight hours).
- ``quanto_multiplier`` is the contract size.
- Neither spot tickers nor futures contracts carry a timestamp.
"""

from __future__ import annotations

from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.atlas_time import plausible_contract_time_ms, plausible_epoch_ms
from vizanix_atlas.core.errors import SchemaMismatch
from vizanix_atlas.core.numeric import parse_float, parse_int, parse_non_negative, parse_positive
from vizanix_atlas.models.enums import OpenInterestUnit
from vizanix_atlas.models.observations import (
    RawDerivativeObservation,
    RawFxObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)

#: Futures settlement currencies Gate.io exposes. Each is a separate endpoint.
_SETTLEMENTS: Final = ("usdt", "btc")

_SECONDS_PER_HOUR: Final = 3600.0

_FX_PAIRS: Final = (
    ("USDC_USDT", "USDC", "USDT"),
    ("DAI_USDT", "DAI", "USDT"),
    ("BTC_USD", "BTC", "USD"),
)


class GateioAdapter(ExchangeAdapter):
    """Collects spot and USDT/BTC-settled perpetual markets from Gate.io."""

    slug = "gateio"
    base_url = "https://api.gateio.ws"

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate tradable spot pairs and every futures contract."""
        instruments: list[RawInstrument] = []

        self.refresh_receive_time()
        pairs = await self.client.get_json(
            "/api/v4/spot/currency_pairs", operation="currency-pairs"
        )
        for row in self.require_list(pairs, path="/api/v4/spot/currency_pairs"):
            parsed = self._parse_spot_instrument(row)
            if parsed is not None:
                instruments.append(parsed)

        for settle in _SETTLEMENTS:
            self.refresh_receive_time()
            contracts = await self.client.get_json(
                f"/api/v4/futures/{settle}/contracts", operation=f"contracts:{settle}"
            )
            for row in self.require_list(contracts, path=f"/api/v4/futures/{settle}/contracts"):
                parsed = self._parse_futures_instrument(row, settle)
                if parsed is not None:
                    instruments.append(parsed)
        return instruments

    def _parse_spot_instrument(self, row: Any) -> RawInstrument | None:
        """Turn one spot currency pair into a :class:`RawInstrument`."""
        if not isinstance(row, dict):
            raise SchemaMismatch("currency pair row was not an object", venue=self.slug)
        pair_id = row.get("id")
        base = row.get("base")
        quote = row.get("quote")
        if not (isinstance(pair_id, str) and isinstance(base, str) and isinstance(quote, str)):
            raise SchemaMismatch("currency pair row lacked id, base or quote", venue=self.slug)
        if str(row.get("trade_status", "")) != "tradable":
            return None

        listing = parse_float(row.get("buy_start"))
        return RawInstrument(
            venue_slug=self.slug,
            symbol_native=pair_id,
            instrument_class="spot",
            instrument_type="spot",
            base_symbol_native=base,
            quote_symbol_native=quote,
            # base_name is the project name, which is useful identity evidence.
            base_name=str(row.get("base_name") or "") or None,
            tick_size=_places_to_step(row.get("precision")),
            quantity_step=_places_to_step(row.get("amount_precision")),
            minimum_quantity=parse_non_negative(row.get("min_base_amount")),
            minimum_notional=parse_non_negative(row.get("min_quote_amount")),
            active=True,
            # Gate.io publishes these as Unix seconds.
            listing_time=plausible_contract_time_ms(listing * 1000) if listing else None,
        )

    def _parse_futures_instrument(self, row: Any, settle: str) -> RawInstrument | None:
        """Turn one futures contract into a :class:`RawInstrument`.

        ``type`` is ``direct`` for linear contracts and ``inverse`` for inverse ones.
        """
        if not isinstance(row, dict):
            raise SchemaMismatch("futures contract row was not an object", venue=self.slug)
        name = row.get("name")
        if not isinstance(name, str) or "_" not in name:
            raise SchemaMismatch("futures contract row had no usable name", venue=self.slug)
        if str(row.get("status", "")) != "trading" or row.get("in_delisting"):
            return None

        base, _, quote = name.partition("_")
        contract_kind = str(row.get("type", ""))
        if contract_kind == "direct":
            contract_type = "linear"
        elif contract_kind == "inverse":
            contract_type = "inverse"
        else:
            self.note_unknown_enum("type", contract_kind)
            return None

        interval_seconds = parse_positive(row.get("funding_interval"))
        create = parse_float(row.get("create_time"))
        return RawInstrument(
            venue_slug=self.slug,
            symbol_native=name,
            instrument_class=f"{contract_type}-perp",
            instrument_type="perpetual",
            base_symbol_native=base,
            quote_symbol_native=quote,
            settlement_symbol_native=settle.upper(),
            contract_type=contract_type,
            contract_multiplier=parse_positive(row.get("quanto_multiplier")),
            contract_value_symbol_native=base,
            tick_size=parse_positive(row.get("order_price_round")),
            minimum_quantity=parse_non_negative(row.get("order_size_min")),
            # Published in seconds; Atlas stores hours.
            funding_interval_hours=(
                interval_seconds / _SECONDS_PER_HOUR if interval_seconds else None
            ),
            funding_semantics="relative_per_interval",
            open_interest_unit=OpenInterestUnit.CONTRACTS.value,
            active=True,
            listing_time=plausible_contract_time_ms(create * 1000) if create else None,
        )

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch spot and futures tickers: one bulk request per market family."""
        tickers: list[RawTicker] = []

        self.refresh_receive_time()
        spot = await self.client.get_json("/api/v4/spot/tickers", operation="spot-tickers")
        for row in self.require_list(spot, path="/api/v4/spot/tickers"):
            if not isinstance(row, dict):
                continue
            pair = row.get("currency_pair")
            if not isinstance(pair, str):
                continue
            tickers.append(
                RawTicker(
                    venue_slug=self.slug,
                    symbol_native=pair,
                    # Gate.io reuses a currency pair's symbol as a futures contract name
                    # (both "ETH_USDT"), so the observation states which product line it
                    # belongs to rather than leaving the matcher to guess.
                    instrument_class="spot",
                    # Gate.io spot tickers carry no timestamp.
                    timing=self.timing(),
                    last_price=parse_positive(row.get("last")),
                    bid_price=parse_positive(row.get("highest_bid")),
                    ask_price=parse_positive(row.get("lowest_ask")),
                    high_24h=parse_positive(row.get("high_24h")),
                    low_24h=parse_positive(row.get("low_24h")),
                    base_volume_24h=parse_non_negative(row.get("base_volume")),
                    quote_volume_24h=parse_non_negative(row.get("quote_volume")),
                )
            )

        for settle in _SETTLEMENTS:
            self.refresh_receive_time()
            futures = await self.client.get_json(
                f"/api/v4/futures/{settle}/tickers", operation=f"futures-tickers:{settle}"
            )
            for row in self.require_list(futures, path=f"/api/v4/futures/{settle}/tickers"):
                if not isinstance(row, dict):
                    continue
                contract = row.get("contract")
                if not isinstance(contract, str):
                    continue
                tickers.append(
                    RawTicker(
                        venue_slug=self.slug,
                        symbol_native=contract,
                        # Only "not spot" is known at this point; contract_type (linear
                        # versus inverse) is not in this response. See discover_instruments
                        # for why the contract's own type must not be guessed from settle.
                        instrument_class="derivative",
                        timing=self.timing(),
                        last_price=parse_positive(row.get("last")),
                        bid_price=parse_positive(row.get("highest_bid")),
                        ask_price=parse_positive(row.get("lowest_ask")),
                        bid_size=parse_non_negative(row.get("highest_size")),
                        ask_size=parse_non_negative(row.get("lowest_size")),
                        high_24h=parse_positive(row.get("high_24h")),
                        low_24h=parse_positive(row.get("low_24h")),
                        base_volume_24h=parse_non_negative(row.get("volume_24h_base")),
                        quote_volume_24h=parse_non_negative(row.get("volume_24h_quote")),
                        contract_volume_24h=parse_non_negative(row.get("volume_24h")),
                    )
                )
        return tickers

    async def fetch_derivatives(self) -> list[RawDerivativeObservation]:
        """Fetch mark, index, funding and open interest from the contracts endpoint.

        The contracts endpoint carries live values as well as static metadata, so
        derivative observations cost no extra request beyond discovery's.
        """
        observations: list[RawDerivativeObservation] = []
        for settle in _SETTLEMENTS:
            self.refresh_receive_time()
            contracts = await self.client.get_json(
                f"/api/v4/futures/{settle}/contracts", operation=f"derivatives:{settle}"
            )
            for row in self.require_list(contracts, path=f"/api/v4/futures/{settle}/contracts"):
                if not isinstance(row, dict):
                    continue
                name = row.get("name")
                if not isinstance(name, str) or str(row.get("status", "")) != "trading":
                    continue
                interval_seconds = parse_positive(row.get("funding_interval"))
                next_apply = parse_float(row.get("funding_next_apply"))
                position_size = parse_int(row.get("position_size"))
                observations.append(
                    RawDerivativeObservation(
                        venue_slug=self.slug,
                        symbol_native=name,
                        instrument_class="derivative",
                        timing=self.timing(),
                        mark_price=parse_positive(row.get("mark_price")),
                        index_price=parse_positive(row.get("index_price")),
                        funding_rate_raw=parse_float(row.get("funding_rate")),
                        funding_interval_hours=(
                            interval_seconds / _SECONDS_PER_HOUR if interval_seconds else None
                        ),
                        next_funding_time=(
                            plausible_epoch_ms(next_apply * 1000) if next_apply else None
                        ),
                        open_interest_raw=(
                            float(position_size) if position_size is not None and position_size >= 0
                            else None
                        ),
                        open_interest_unit=(
                            OpenInterestUnit.CONTRACTS if position_size is not None else None
                        ),
                    )
                )
        return observations

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one spot order-book snapshot."""
        self.refresh_receive_time()
        payload = await self.client.get_json(
            "/api/v4/spot/order_book",
            params={"currency_pair": symbol_native, "limit": min(depth, 100), "with_id": "false"},
            operation="order-book",
        )
        body = self.require_mapping(payload, path="/api/v4/spot/order_book")
        bids, bids_truncated = self.build_levels(
            self.require_list(body, path="order_book", key="bids"), depth=depth, descending=True
        )
        asks, asks_truncated = self.build_levels(
            self.require_list(body, path="order_book", key="asks"), depth=depth, descending=False
        )
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            # This endpoint only ever returns a spot book; declared explicitly so a
            # symbol shared with a futures contract still resolves to the spot instrument.
            instrument_class="spot",
            timing=self.timing(
                exchange_event_time=plausible_epoch_ms(parse_float(body.get("current")))
            ),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated,
        )

    async def fetch_fx_observations(self) -> list[RawFxObservation]:
        """Observe stablecoin rates from Gate.io's own spot markets."""
        self.refresh_receive_time()
        payload = await self.client.get_json("/api/v4/spot/tickers", operation="spot-tickers:fx")
        rows = {
            row["currency_pair"]: row
            for row in self.require_list(payload, path="/api/v4/spot/tickers")
            if isinstance(row, dict) and isinstance(row.get("currency_pair"), str)
        }
        observations: list[RawFxObservation] = []
        for pair, from_code, to_code in _FX_PAIRS:
            row = rows.get(pair)
            if row is None:
                continue
            ticker = RawTicker(
                venue_slug=self.slug,
                symbol_native=pair,
                instrument_class="spot",
                timing=self.timing(),
                last_price=parse_positive(row.get("last")),
                bid_price=parse_positive(row.get("highest_bid")),
                ask_price=parse_positive(row.get("lowest_ask")),
            )
            candidate = ticker.reference_candidate
            if candidate is None:
                continue
            rate, source = candidate
            observations.append(
                RawFxObservation(
                    venue_slug=self.slug,
                    symbol_native=pair,
                    timing=ticker.timing,
                    from_symbol_native=from_code,
                    to_symbol_native=to_code,
                    rate=rate,
                    source_price=source,
                )
            )
        return observations

    async def server_time_ms(self) -> int | None:
        """Fetch Gate.io's server clock for skew measurement."""
        payload = await self.client.get_json("/api/v4/spot/time", operation="time")
        body = self.require_mapping(payload, path="/api/v4/spot/time")
        return plausible_epoch_ms(parse_float(body.get("server_time")))


def _places_to_step(value: Any) -> float | None:
    """Convert a decimal-places count into a step size.

    Gate.io reports precision as a number of decimal places. An ``amount_precision``
    of ``0`` means whole units, which is a step of 1.
    """
    places = parse_float(value)
    if places is None or places < 0 or places > 18 or not float(places).is_integer():
        return None
    return 10.0 ** -int(places)
