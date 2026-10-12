"""Records exchanged between the live layer's parts.

A feed turns one venue's WebSocket frames into :class:`LiveTick` values. A tick is a
*partial update*: venues that stream deltas send only the fields that changed, so every
field is optional and the book merges ticks into per-instrument state instead of
replacing it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class LiveTick:
    """One partial update for one instrument on one venue."""

    venue: str
    instrument_class: str
    symbol: str
    received_ms: int
    event_ms: int | None = None
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    #: 24-hour volume in the quote currency, when the venue states it that way.
    quote_volume_24h: float | None = None
    #: 24-hour volume in the base asset, when the venue states it that way.
    base_volume_24h: float | None = None
    mark: float | None = None
    index: float | None = None
    #: Funding rate exactly as the venue states it, for its own interval. Bybit sends the
    #: rate and the interval in different deltas, so the book pairs and rescales them.
    funding_rate: float | None = None
    funding_interval_hours: float | None = None
    #: Open interest in USD, only when the venue states it in USD.
    open_interest_usd: float | None = None


@dataclass(slots=True)
class Parsed:
    """The outcome of parsing one frame: ticks, plus frames the feed must send back."""

    ticks: list[LiveTick] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)


def positive(value: object) -> float | None:
    """Coerce a venue value to a finite positive float, or ``None``.

    Venues send numbers as strings, empty strings and zeros for "no value". A zero price
    or volume is the absence of data, never a measurement, so it maps to ``None``.
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def finite(value: object) -> float | None:
    """Coerce to a finite float, allowing zero and negatives (funding can be negative)."""
    if value is None or value == "":
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
