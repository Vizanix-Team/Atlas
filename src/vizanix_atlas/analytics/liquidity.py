"""Liquidity measurement.

Atlas measures the **visible** order book at one instant and names its metrics
accordingly. ``observable_bid_depth_50bps_usd`` is resting bid liquidity within 50 basis
points of the reference price, as the venue showed it, at the moment it was sampled.

What these numbers are not
--------------------------
They are not predicted execution cost. The visible book excludes hidden and iceberg
liquidity, ignores fees, assumes no latency, assumes no other participant reacts, and
assumes nothing is cancelled as an order arrives. An impact estimate is therefore called
an *observable order-book impact estimate*, never slippage.

The liquidity surface
---------------------
Depth is measured per instrument, then aggregated per asset while keeping the per-venue
contributions. That is what makes the surface useful: it answers not only "how deep is
BTC" but "how much of that depth is one venue".
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from vizanix_atlas.analytics.robust import herfindahl, median
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.models.enums import InstrumentType
from vizanix_atlas.models.instrument import Instrument
from vizanix_atlas.models.observations import OrderBookLevel, RawOrderBook
from vizanix_atlas.models.state import (
    DepthBand,
    ImpactEstimate,
    LiquidityState,
    VenueLiquidity,
)

_log = get_logger(__name__)

#: Band at which venue shares, concentration and book imbalance are reported. Wide
#: enough to hold real resting size on a liquid market, narrow enough to still describe
#: the top of the book.
_SHARE_BAND_BPS: Final = 50


@dataclass(frozen=True, slots=True)
class BookSample:
    """One order-book snapshot with everything needed to value it in USD."""

    venue_slug: str
    instrument: Instrument
    book: RawOrderBook
    #: Rate from the instrument's quote currency to USD. ``None`` means no USD-valued
    #: depth can be published for this book.
    quote_to_usd: float | None
    observed_at: int | None

    @property
    def can_value(self) -> bool:
        """Whether this book's depth can be expressed in USD."""
        return self.quote_to_usd is not None and self.quote_to_usd > 0


def _level_notional_usd(
    level: OrderBookLevel, instrument: Instrument, quote_to_usd: float
) -> float | None:
    """Return one level's resting notional in USD.

    The arithmetic differs by contract denomination, and getting it wrong silently
    changes depth by orders of magnitude:

    - **Spot**: ``size`` is base units, so notional is ``price * size`` in the quote
      currency, then converted.
    - **Linear derivative**: ``size`` is contracts, each worth ``contract_multiplier``
      base units, so notional is ``price * size * multiplier``.
    - **Inverse derivative**: each contract is worth ``contract_multiplier`` units of the
      *quote* currency regardless of price, so notional is ``size * multiplier`` and
      does not involve the price at all.

    Returns ``None`` when a required multiplier is absent, which excludes the level
    rather than valuing it with a guessed multiplier.
    """
    if instrument.instrument_type is InstrumentType.SPOT:
        return level.price * level.size * quote_to_usd

    multiplier = instrument.contract_multiplier
    if multiplier is None:
        return None
    if instrument.is_inverse:
        # An inverse contract's value is fixed in the quote currency.
        return level.size * multiplier * quote_to_usd
    return level.price * level.size * multiplier * quote_to_usd


def depth_within(
    levels: Sequence[OrderBookLevel],
    *,
    reference_price: float,
    distance_bps: int,
    side: str,
    instrument: Instrument,
    quote_to_usd: float,
) -> float | None:
    """Sum resting notional within ``distance_bps`` of ``reference_price``.

    The band is measured from the reference price rather than from the venue's own mid, so
    that depth is comparable across venues: a venue quoting 30 bps away from the market
    has little liquidity *at the market*, and measuring from its own mid would hide that.

    Returns ``None`` when no level could be valued, which is different from ``0.0``
    meaning there was genuinely nothing inside the band.
    """
    if reference_price <= 0:
        return None
    edge = (
        reference_price * (1.0 - distance_bps / 10_000.0)
        if side == "bid"
        else reference_price * (1.0 + distance_bps / 10_000.0)
    )

    total = 0.0
    valued_any = False
    for level in levels:
        inside = level.price >= edge if side == "bid" else level.price <= edge
        if not inside:
            # Levels are ordered best first, so the first level outside the band ends it.
            break
        notional = _level_notional_usd(level, instrument, quote_to_usd)
        if notional is None:
            return None
        total += notional
        valued_any = True
    return total if valued_any or levels else None


def walk_book_impact(
    levels: Sequence[OrderBookLevel],
    *,
    reference_price: float,
    target_notional_usd: float,
    side: str,
    instrument: Instrument,
    quote_to_usd: float,
) -> tuple[float | None, bool]:
    """Estimate the cost of consuming ``target_notional_usd`` of visible liquidity.

    Walks outward from the best price, accumulating notional until the target is met, and
    returns the volume-weighted average price of what was consumed, expressed as a
    deviation from ``reference_price`` in basis points.

    Returns:
        The impact in basis points and whether the visible book held enough depth. When
        it did not, the impact is ``None`` rather than the cost of the partial fill,
        because a partial fill's average price understates the true cost.
    """
    if reference_price <= 0 or target_notional_usd <= 0:
        return None, False

    remaining = target_notional_usd
    weighted_price_sum = 0.0
    consumed = 0.0

    for level in levels:
        notional = _level_notional_usd(level, instrument, quote_to_usd)
        if notional is None or notional <= 0:
            continue
        take = min(notional, remaining)
        weighted_price_sum += level.price * take
        consumed += take
        remaining -= take
        if remaining <= 0:
            break

    if remaining > 0 or consumed <= 0:
        return None, False

    vwap = weighted_price_sum / consumed
    # A buy walks up the asks, so the impact is positive; a sell walks down the bids, so
    # the sign is flipped to keep both impacts positive and comparable.
    deviation = (vwap - reference_price) / reference_price * 10_000.0
    impact = deviation if side == "buy" else -deviation
    # Clamp at zero: a tiny negative value here reflects the reference price sitting a
    # fraction inside the book, not a negative cost.
    return max(0.0, impact), True


def measure_instrument(
    sample: BookSample,
    *,
    reference_price: float,
    bands_bps: Sequence[int],
) -> VenueLiquidity | None:
    """Measure one order-book snapshot.

    Returns ``None`` when the book cannot be valued in USD, which keeps an unvaluable
    book out of the surface rather than contributing zero depth to it.
    """
    if not sample.can_value or sample.quote_to_usd is None:
        return None

    bands: list[DepthBand] = []
    for distance in sorted(bands_bps):
        bid = depth_within(
            sample.book.bids,
            reference_price=reference_price,
            distance_bps=distance,
            side="bid",
            instrument=sample.instrument,
            quote_to_usd=sample.quote_to_usd,
        )
        ask = depth_within(
            sample.book.asks,
            reference_price=reference_price,
            distance_bps=distance,
            side="ask",
            instrument=sample.instrument,
            quote_to_usd=sample.quote_to_usd,
        )
        bands.append(DepthBand(distance_bps=distance, bid_depth_usd=bid, ask_depth_usd=ask))

    best_bid = sample.book.best_bid
    best_ask = sample.book.best_ask
    spread_bps = None
    if best_bid is not None and best_ask is not None and best_ask > best_bid > 0:
        mid = (best_bid + best_ask) / 2.0
        spread_bps = (best_ask - best_bid) / mid * 10_000.0

    return VenueLiquidity(
        venue_slug=sample.venue_slug,
        instrument_id=sample.instrument.instrument_id,
        instrument_type=sample.instrument.instrument_type,
        spread_bps=spread_bps,
        bands=tuple(bands),
        book_truncated=sample.book.truncated,
        observed_at=sample.observed_at,
    )


def aggregate_liquidity(
    samples: Sequence[BookSample],
    *,
    reference_price: float | None,
    bands_bps: Sequence[int],
    impact_notionals_usd: Sequence[float],
) -> LiquidityState:
    """Build an asset's liquidity surface from every sampled book.

    Depth aggregates across venues; spread does not. A spread is a property of one venue's
    book, so Atlas reports the best and the median across venues rather than a sum, which
    would be meaningless.
    """
    if reference_price is None or reference_price <= 0 or not samples:
        return LiquidityState()

    measured = [
        m
        for m in (
            measure_instrument(s, reference_price=reference_price, bands_bps=bands_bps)
            for s in samples
        )
        if m is not None
    ]
    if not measured:
        return LiquidityState()

    aggregate_bands: list[DepthBand] = []
    for distance in sorted(bands_bps):
        bids = [
            b.bid_depth_usd
            for m in measured
            for b in m.bands
            if b.distance_bps == distance and b.bid_depth_usd is not None
        ]
        asks = [
            b.ask_depth_usd
            for m in measured
            for b in m.bands
            if b.distance_bps == distance and b.ask_depth_usd is not None
        ]
        aggregate_bands.append(
            DepthBand(
                distance_bps=distance,
                bid_depth_usd=math.fsum(bids) if bids else None,
                ask_depth_usd=math.fsum(asks) if asks else None,
            )
        )

    # Impact is estimated against the aggregated book across venues, because a trader
    # choosing the best price at each level is what the visible market offers. This is
    # still an upper bound on available liquidity, not an achievable execution.
    combined_bids, combined_asks = _combine_books(samples)
    impacts: list[ImpactEstimate] = []
    for notional in sorted(impact_notionals_usd):
        buy, buy_ok = _aggregate_impact(
            combined_asks, reference_price=reference_price, target=notional, side="buy"
        )
        sell, sell_ok = _aggregate_impact(
            combined_bids, reference_price=reference_price, target=notional, side="sell"
        )
        impacts.append(
            ImpactEstimate(
                notional_usd=notional,
                buy_impact_bps=buy,
                sell_impact_bps=sell,
                buy_depth_sufficient=buy_ok,
                sell_depth_sufficient=sell_ok,
            )
        )

    share_depths = _venue_share_depths(measured)
    total_share_depth = math.fsum(share_depths.values())
    with_shares = tuple(
        m.model_copy(
            update={
                "depth_share": (
                    share_depths.get(m.venue_slug, 0.0) / total_share_depth
                    if total_share_depth > 0
                    else None
                )
            }
        )
        for m in sorted(measured, key=lambda m: (m.venue_slug, m.instrument_id))
    )

    spreads = [m.spread_bps for m in measured if m.spread_bps is not None]
    share_band = next(
        (b for b in aggregate_bands if b.distance_bps == _SHARE_BAND_BPS), None
    )
    imbalance = None
    if share_band is not None:
        bid, ask = share_band.bid_depth_usd, share_band.ask_depth_usd
        if bid is not None and ask is not None and (bid + ask) > 0:
            imbalance = (bid - ask) / (bid + ask)

    shares = list(share_depths.values())
    return LiquidityState(
        best_spread_bps=min(spreads) if spreads else None,
        median_spread_bps=median(spreads) if len(spreads) >= 2 else None,
        bands=tuple(aggregate_bands),
        impacts=tuple(impacts),
        by_venue=with_shares,
        venue_count=len({m.venue_slug for m in measured}),
        largest_venue_share=(
            max(shares) / total_share_depth if shares and total_share_depth > 0 else None
        ),
        concentration_hhi=herfindahl(shares) if shares else None,
        book_imbalance_50bps=imbalance,
    )


def _venue_share_depths(measured: Sequence[VenueLiquidity]) -> dict[str, float]:
    """Return each venue's combined depth at the share band."""
    depths: dict[str, float] = {}
    for entry in measured:
        band = next((b for b in entry.bands if b.distance_bps == _SHARE_BAND_BPS), None)
        if band is None:
            continue
        total = band.total_depth_usd
        if total is None:
            continue
        depths[entry.venue_slug] = depths.get(entry.venue_slug, 0.0) + total
    return depths


def _combine_books(
    samples: Sequence[BookSample],
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Merge every venue's book into one price-ordered book of USD notional.

    Levels become ``(price, usd_notional)`` pairs so the walk does not need to know which
    instrument each level came from.
    """
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for sample in samples:
        if not sample.can_value or sample.quote_to_usd is None:
            continue
        for side, target in (("bids", bids), ("asks", asks)):
            for level in getattr(sample.book, side):
                notional = _level_notional_usd(
                    level, sample.instrument, sample.quote_to_usd
                )
                if notional is not None and notional > 0:
                    target.append((level.price, notional))
    bids.sort(key=lambda pair: pair[0], reverse=True)
    asks.sort(key=lambda pair: pair[0])
    return bids, asks


def _aggregate_impact(
    levels: Sequence[tuple[float, float]],
    *,
    reference_price: float,
    target: float,
    side: str,
) -> tuple[float | None, bool]:
    """Walk a combined book to estimate impact for one notional."""
    remaining = target
    weighted = 0.0
    consumed = 0.0
    for price, notional in levels:
        take = min(notional, remaining)
        weighted += price * take
        consumed += take
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0 or consumed <= 0:
        return None, False
    vwap = weighted / consumed
    deviation = (vwap - reference_price) / reference_price * 10_000.0
    impact = deviation if side == "buy" else -deviation
    return max(0.0, impact), True
