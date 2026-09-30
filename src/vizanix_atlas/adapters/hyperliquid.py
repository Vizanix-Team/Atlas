"""Hyperliquid adapter.

Official documentation: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api

Verified 2026-09-27.

Funding is hourly
    The venue's funding documentation states that "the funding rate formula applies to
    8 hour funding rate. However, funding is paid every hour at one eighth of the
    computed rate for each hour." The observed baseline value of ``0.0000125`` per
    hour is exactly ``0.0001`` (0.01%) per eight hours, which corroborates it. The
    interval is therefore recorded as 1 hour and the 8-hour equivalent is the hourly
    rate times eight.

Parallel arrays
    ``metaAndAssetCtxs`` returns a two-element array: the universe metadata and the
    per-asset contexts, aligned by index. The adapter zips them and fails loudly if
    the lengths disagree, because a silent misalignment would attribute one asset's
    funding to another.

POST, not GET
    Hyperliquid's public info endpoint takes a JSON body. It is the only supported
    venue that does.

No timestamp
    No response timestamp exists, so freshness rests on the collector's receive time.
"""

from __future__ import annotations

from typing import Any, Final

from vizanix_atlas.adapters.base import DEFAULT_BOOK_DEPTH, ExchangeAdapter
from vizanix_atlas.core.errors import SchemaMismatch
from vizanix_atlas.core.numeric import parse_float, parse_non_negative, parse_positive
from vizanix_atlas.models.enums import OpenInterestUnit
from vizanix_atlas.models.observations import (
    RawDerivativeObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)

#: Every Hyperliquid perpetual is quoted and settled in USDC.
_QUOTE_SYMBOL: Final = "USDC"

#: Funding is paid hourly; see the module docstring.
_FUNDING_INTERVAL_HOURS: Final = 1.0

_INFO_PATH: Final = "/info"


class HyperliquidAdapter(ExchangeAdapter):
    """Collects perpetual markets from Hyperliquid."""

    slug = "hyperliquid"
    base_url = "https://api.hyperliquid.xyz"

    async def _perp_context(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Fetch universe metadata paired with per-asset context.

        Raises:
            SchemaMismatch: If the response is not the documented two-element array,
                or if the two arrays have different lengths. A length mismatch would
                silently misattribute every field past the divergence.

        """
        self.refresh_receive_time()
        payload = await self.client.post_json(
            _INFO_PATH, json_body={"type": "metaAndAssetCtxs"}, operation="meta-and-asset-ctxs"
        )
        outer = self.require_list(payload, path=_INFO_PATH)
        if len(outer) != 2:
            raise SchemaMismatch(
                "metaAndAssetCtxs did not return a two-element array",
                venue=self.slug,
                length=len(outer),
            )
        meta = self.require_mapping(outer[0], path=f"{_INFO_PATH}[0]")
        universe = self.require_list(meta, path=f"{_INFO_PATH}[0]", key="universe")
        contexts = self.require_list(outer[1], path=f"{_INFO_PATH}[1]")
        if len(universe) != len(contexts):
            raise SchemaMismatch(
                "universe and asset context arrays have different lengths",
                venue=self.slug,
                universe=len(universe),
                contexts=len(contexts),
            )
        return [
            (entry, context)
            for entry, context in zip(universe, contexts, strict=True)
            if isinstance(entry, dict) and isinstance(context, dict)
        ]

    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate every perpetual in the venue's universe."""
        instruments: list[RawInstrument] = []
        for entry, _ in await self._perp_context():
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise SchemaMismatch("universe entry had no name", venue=self.slug)
            # A delisted asset stays in the universe with isDelisted set.
            if entry.get("isDelisted"):
                continue
            size_decimals = parse_float(entry.get("szDecimals"))
            instruments.append(
                RawInstrument(
                    venue_slug=self.slug,
                    symbol_native=name,
                    instrument_class="linear-perp",
                    instrument_type="perpetual",
                    base_symbol_native=name,
                    quote_symbol_native=_QUOTE_SYMBOL,
                    settlement_symbol_native=_QUOTE_SYMBOL,
                    contract_type="linear",
                    # Sizes are quoted directly in the base asset, so one unit of
                    # size is one unit of the base asset.
                    contract_multiplier=1.0,
                    contract_value_symbol_native=name,
                    quantity_step=(
                        10.0 ** -int(size_decimals)
                        if size_decimals is not None and 0 <= size_decimals <= 18
                        else None
                    ),
                    funding_interval_hours=_FUNDING_INTERVAL_HOURS,
                    funding_semantics="relative_per_interval",
                    open_interest_unit=OpenInterestUnit.BASE_ASSET.value,
                    active=True,
                )
            )
        return instruments

    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch quotes for every perpetual from the asset contexts.

        The context carries a mid price and ``impactPxs``, but no resting best bid or
        ask, so bid and ask stay null and spread is a tier B measurement here.
        """
        tickers: list[RawTicker] = []
        for entry, context in await self._perp_context():
            name = entry.get("name")
            if not isinstance(name, str) or entry.get("isDelisted"):
                continue
            tickers.append(
                RawTicker(
                    venue_slug=self.slug,
                    symbol_native=name,
                    timing=self.timing(),
                    last_price=parse_positive(context.get("midPx")),
                    open_24h=parse_positive(context.get("prevDayPx")),
                    base_volume_24h=parse_non_negative(context.get("dayBaseVlm")),
                    # dayNtlVlm is notional volume in the USDC quote currency.
                    quote_volume_24h=parse_non_negative(context.get("dayNtlVlm")),
                )
            )
        return tickers

    async def fetch_derivatives(self) -> list[RawDerivativeObservation]:
        """Fetch mark price, oracle price, funding and open interest."""
        observations: list[RawDerivativeObservation] = []
        for entry, context in await self._perp_context():
            name = entry.get("name")
            if not isinstance(name, str) or entry.get("isDelisted"):
                continue
            open_interest = parse_non_negative(context.get("openInterest"))
            observations.append(
                RawDerivativeObservation(
                    venue_slug=self.slug,
                    symbol_native=name,
                    timing=self.timing(),
                    mark_price=parse_positive(context.get("markPx")),
                    index_price=parse_positive(context.get("oraclePx")),
                    funding_rate_raw=parse_float(context.get("funding")),
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

        Hyperliquid returns ``levels`` as a two-element array of bid and ask arrays,
        each level an object with ``px`` and ``sz`` rather than a positional pair, so
        the levels are converted before the shared builder is used.
        """
        self.refresh_receive_time()
        payload = await self.client.post_json(
            _INFO_PATH,
            json_body={"type": "l2Book", "coin": symbol_native},
            operation="l2-book",
        )
        body = self.require_mapping(payload, path=_INFO_PATH)
        levels = self.require_list(body, path=_INFO_PATH, key="levels")
        if len(levels) != 2:
            raise SchemaMismatch(
                "l2Book levels was not a two-element array", venue=self.slug, length=len(levels)
            )
        bids, bids_truncated = self.build_levels(_as_pairs(levels[0]), depth=depth, descending=True)
        asks, asks_truncated = self.build_levels(
            _as_pairs(levels[1]), depth=depth, descending=False
        )
        from vizanix_atlas.core.atlas_time import plausible_epoch_ms

        return RawOrderBook(
            venue_slug=self.slug,
            symbol_native=symbol_native,
            timing=self.timing(
                exchange_server_time=plausible_epoch_ms(parse_float(body.get("time")))
            ),
            bids=bids,
            asks=asks,
            truncated=bids_truncated or asks_truncated,
        )


def _as_pairs(side: Any) -> list[list[float | str]]:
    """Convert Hyperliquid's ``{"px": ..., "sz": ...}`` levels into positional pairs."""
    if not isinstance(side, list):
        return []
    return [
        [level["px"], level["sz"]]
        for level in side
        if isinstance(level, dict) and "px" in level and "sz" in level
    ]
