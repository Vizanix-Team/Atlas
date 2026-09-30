"""dYdX v4 adapter.

Official documentation: https://docs.dydx.xyz/

Verified 2026-09-27 against the public indexer.

Funding is hourly
    ``/v4/historicalFunding/BTC-USD`` returned records at ``effectiveAt`` values of
    14:00, 15:00, 16:00 and 17:00 UTC, so ``nextFundingRate`` is a one-hour rate.
    The ``defaultFundingRate1H`` field name agrees.

Oracle price, not mark price
    dYdX publishes ``oraclePrice``. Atlas records it as the index price rather than
    the mark price, so that basis (which is defined against mark prices) is not
    computed from something else under the same name.

No top of book
    ``/v4/perpetualMarkets`` carries no bid or ask, so spread is tier B only.
"""

from __future__ import annotations

from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.errors import SchemaMismatch
from vizanix_atlas.core.numeric import parse_float, parse_int, parse_non_negative, parse_positive
from vizanix_atlas.models.enums import OpenInterestUnit
from vizanix_atlas.models.observations import (
    RawDerivativeObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)

#: Every dYdX v4 perpetual is quoted and settled in USDC.
_QUOTE_SYMBOL: Final = "USDC"

#: Funding is hourly; see the module docstring.
_FUNDING_INTERVAL_HOURS: Final = 1.0

#: Market statuses that describe a live market.
_ACTIVE_STATUSES: Final = frozenset({"ACTIVE"})


class DydxAdapter(ExchangeAdapter):
    """Collects perpetual markets from the dYdX v4 public indexer."""

    slug = "dydx"
    base_url = "https://indexer.dydx.trade"

    async def _markets(self) -> list[tuple[str, dict[str, Any]]]:
        """Fetch every perpetual market, keyed by ticker."""
        self.refresh_receive_time()
        payload = await self.client.get_json("/v4/perpetualMarkets", operation="perpetual-markets")
        body = self.require_mapping(payload, path="/v4/perpetualMarkets")
        markets = self.require_mapping(body, path="/v4/perpetualMarkets", key="markets")
        return [
            (ticker, row)
            for ticker, row in sorted(markets.items())
            if isinstance(row, dict) and isinstance(ticker, str)
        ]

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate every active perpetual market."""
        instruments: list[RawInstrument] = []
        for ticker, row in await self._markets():
            status = str(row.get("status", ""))
            if status not in _ACTIVE_STATUSES:
                if status not in (
                    "PAUSED",
                    "CANCEL_ONLY",
                    "POST_ONLY",
                    "INITIALIZING",
                    "FINAL_SETTLEMENT",
                ):
                    self.note_unknown_enum("status", status)
                continue
            base = row.get("ticker")
            if not isinstance(base, str) or "-" not in base:
                raise SchemaMismatch("market row had no usable ticker", venue=self.slug)
            base_symbol = base.split("-", 1)[0]

            instruments.append(
                RawInstrument(
                    venue_slug=self.slug,
                    symbol_native=ticker,
                    instrument_class="linear-perp",
                    instrument_type="perpetual",
                    base_symbol_native=base_symbol,
                    quote_symbol_native=_QUOTE_SYMBOL,
                    settlement_symbol_native=_QUOTE_SYMBOL,
                    contract_type="linear",
                    # Sizes are quoted in the base asset directly.
                    contract_multiplier=1.0,
                    contract_value_symbol_native=base_symbol,
                    tick_size=parse_positive(row.get("tickSize")),
                    quantity_step=parse_positive(row.get("stepSize")),
                    funding_interval_hours=_FUNDING_INTERVAL_HOURS,
                    funding_semantics="relative_per_interval",
                    open_interest_unit=OpenInterestUnit.BASE_ASSET.value,
                    active=True,
                )
            )
        return instruments

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch quotes for every market.

        The indexer publishes an oracle price and a 24-hour change but no last traded
        price and no book, so the oracle price is recorded as the last price with the
        understanding that ``fetch_derivatives`` also records it as the index price.
        """
        tickers: list[RawTicker] = []
        for ticker, row in await self._markets():
            if str(row.get("status", "")) not in _ACTIVE_STATUSES:
                continue
            oracle = parse_positive(row.get("oraclePrice"))
            change = parse_float(row.get("priceChange24H"))
            tickers.append(
                RawTicker(
                    venue_slug=self.slug,
                    symbol_native=ticker,
                    # The indexer publishes no timestamp on this endpoint.
                    timing=self.timing(),
                    last_price=oracle,
                    open_24h=(
                        oracle - change
                        if oracle is not None and change is not None and oracle - change > 0
                        else None
                    ),
                    # volume24H is USDC notional turnover.
                    quote_volume_24h=parse_non_negative(row.get("volume24H")),
                    trade_count_24h=parse_int(row.get("trades24H")),
                )
            )
        return tickers

    async def fetch_derivatives(self) -> list[RawDerivativeObservation]:
        """Fetch oracle price, funding and open interest."""
        observations: list[RawDerivativeObservation] = []
        for ticker, row in await self._markets():
            if str(row.get("status", "")) not in _ACTIVE_STATUSES:
                continue
            open_interest = parse_non_negative(row.get("openInterest"))
            observations.append(
                RawDerivativeObservation(
                    venue_slug=self.slug,
                    symbol_native=ticker,
                    timing=self.timing(),
                    # dYdX publishes no mark price, so mark_price stays null rather
                    # than being filled with the oracle price under a different name.
                    mark_price=None,
                    index_price=parse_positive(row.get("oraclePrice")),
                    funding_rate_raw=parse_float(row.get("nextFundingRate")),
                    funding_interval_hours=_FUNDING_INTERVAL_HOURS,
                    open_interest_raw=open_interest,
                    open_interest_unit=(
                        OpenInterestUnit.BASE_ASSET if open_interest is not None else None
                    ),
                    open_interest_base=open_interest,
                )
            )
        return observations

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one order-book snapshot.

        The indexer returns levels as objects with ``price`` and ``size`` keys.
        """
        self.refresh_receive_time()
        payload = await self.client.get_json(
            f"/v4/orderbooks/perpetualMarket/{symbol_native}", operation="orderbook"
        )
        body = self.require_mapping(payload, path="/v4/orderbooks/perpetualMarket")
        bids, bids_truncated = self.build_levels(
            _as_pairs(body.get("bids")), depth=depth, descending=True
        )
        asks, asks_truncated = self.build_levels(
            _as_pairs(body.get("asks")), depth=depth, descending=False
        )
        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            timing=self.timing(),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated,
        )


def _as_pairs(side: Any) -> list[list[float | str]]:
    """Convert dYdX's ``{"price": ..., "size": ...}`` levels into positional pairs."""
    if not isinstance(side, list):
        return []
    return [
        [level["price"], level["size"]]
        for level in side
        if isinstance(level, dict) and "price" in level and "size" in level
    ]
