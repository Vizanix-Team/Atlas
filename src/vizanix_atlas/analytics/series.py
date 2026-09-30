"""Metrics over the snapshot history.

Anything measured over time depends on Atlas having a history to measure. At launch there
is none, and Atlas says so rather than computing a 30-day percentile from two days of
observations and labelling it as such.

Every windowed metric therefore has two gates, both configured in
``config/collection.yaml``:

``minimum_observations``
    Fewest observations required. Below it the metric is ``None``.
``max_window_missingness``
    Fewest observations *relative to what the window should contain*. A window meeting
    the absolute count but riddled with gaps is still insufficient, because a sparse
    window is not the window it claims to be.

Both are published as :class:`~vizanix_atlas.models.quality.WindowCoverage`, so a consumer
can see how solid a value's basis is instead of inferring it.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from vizanix_atlas.analytics.robust import percentile_rank, standard_deviation
from vizanix_atlas.core.config import HistoryConfig
from vizanix_atlas.models.quality import WindowCoverage
from vizanix_atlas.models.state import ReturnsState, TechnicalState, VolatilityState

#: Minutes per snapshot slot. Used to turn a window duration into an expected count.
_SLOT_MINUTES: Final = 15

#: Window durations in hours.
_WINDOW_HOURS: Final = {"1h": 1, "4h": 4, "24h": 24, "7d": 168, "30d": 720}


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """One historical observation of an asset's reference price."""

    observed_at: int
    reference_price: float


@dataclass(slots=True)
class PriceSeries:
    """An asset's reference-price history, oldest first.

    Constructed per asset from the compacted history. Kept small: only the columns the
    windowed metrics need, so that a full-market pass does not load years of rows.
    """

    points: tuple[SeriesPoint, ...]

    @property
    def latest(self) -> SeriesPoint | None:
        """The most recent point."""
        return self.points[-1] if self.points else None

    def window(self, hours: int) -> tuple[SeriesPoint, ...]:
        """Return the points falling within the last ``hours``."""
        if not self.points:
            return ()
        cutoff = self.points[-1].observed_at - hours * 3_600_000
        return tuple(p for p in self.points if p.observed_at >= cutoff)

    def prices(self, hours: int) -> tuple[float, ...]:
        """Return the prices within the last ``hours``."""
        return tuple(p.reference_price for p in self.window(hours))

    def coverage(self, window: str, *, required: int) -> WindowCoverage:
        """Return how complete a window is.

        ``observations_expected`` is derived from the window duration and the snapshot
        cadence, so a window missing scheduled runs reports below 1 even when it meets
        the absolute minimum.
        """
        hours = _WINDOW_HOURS.get(window, 24)
        expected = max(1, hours * 60 // _SLOT_MINUTES)
        return WindowCoverage(
            window=window,
            observations_present=len(self.window(hours)),
            observations_expected=expected,
            observations_required=required,
        )


def _sufficient(coverage: WindowCoverage, history: HistoryConfig) -> bool:
    """Return whether a window may be published.

    Both gates must pass: the absolute count and the missingness fraction.
    """
    if not coverage.sufficient:
        return False
    ratio = coverage.ratio
    if ratio is None:
        return False
    return (1.0 - ratio) <= history.max_window_missingness


def log_returns(prices: Sequence[float]) -> list[float]:
    """Return consecutive log returns, skipping any non-positive price.

    Log returns are used because they are additive over time, which makes the
    square-root-of-time annualisation below correct rather than approximate.
    """
    returns: list[float] = []
    for previous, current in itertools.pairwise(prices):
        if previous > 0 and current > 0:
            returns.append(math.log(current / previous))
    return returns


def realised_volatility(prices: Sequence[float], *, periods_per_year: float) -> float | None:
    """Return annualised realised volatility from a price series.

    The standard deviation of log returns scaled by the square root of the number of
    periods in a year. ``periods_per_year`` is derived from the snapshot cadence, so the
    figure is explicitly a snapshot-cadence measurement and not comparable to one computed
    from a venue's own candles.
    """
    returns = log_returns(prices)
    deviation = standard_deviation(returns)
    if deviation is None:
        return None
    return deviation * math.sqrt(periods_per_year)


def compute_volatility(series: PriceSeries, history: HistoryConfig) -> VolatilityState:
    """Compute realised volatility over every configured window.

    A window with insufficient history yields ``None`` for that window only, so an asset
    with two days of history publishes 1-hour and 24-hour volatility and nothing longer.
    """
    # Snapshots per year at the configured cadence.
    periods_per_year = 365 * 24 * 60 / _SLOT_MINUTES
    values: dict[str, float | None] = {}
    coverages: list[WindowCoverage] = []

    for window in ("1h", "4h", "24h", "7d"):
        required = history.required_for(f"realised_volatility_{window}")
        coverage = series.coverage(window, required=required)
        coverages.append(coverage)
        if _sufficient(coverage, history):
            values[window] = realised_volatility(
                series.prices(_WINDOW_HOURS[window]), periods_per_year=periods_per_year
            )
        else:
            values[window] = None

    atr_required = history.required_for("atr_14")
    atr_coverage = series.coverage("24h", required=atr_required)
    atr = _average_true_range_bps(series.prices(24)) if _sufficient(atr_coverage, history) else None

    return VolatilityState(
        realised_1h=values["1h"],
        realised_4h=values["4h"],
        realised_24h=values["24h"],
        realised_7d=values["7d"],
        atr_14_bps=atr,
        sampling_interval_minutes=_SLOT_MINUTES,
        annualisation="sqrt_time",
        windows=tuple(coverages),
    )


def _average_true_range_bps(prices: Sequence[float]) -> float | None:
    """Return a 14-period average true range, relative to the last price.

    Atlas has no intra-snapshot high and low, so the true range degenerates to the
    absolute change between consecutive snapshots. Stated here because calling this an ATR
    without that caveat would imply a candle-based calculation Atlas cannot perform.
    """
    if len(prices) < 15:
        return None
    ranges = [abs(b - a) for a, b in itertools.pairwise(prices)]
    recent = ranges[-14:]
    if not recent or prices[-1] <= 0:
        return None
    return math.fsum(recent) / len(recent) / prices[-1] * 10_000.0


def compute_returns(series: PriceSeries, history: HistoryConfig) -> ReturnsState:
    """Compute fractional price change over each window."""
    coverages: list[WindowCoverage] = []
    values: dict[str, float | None] = {}

    for window in ("1h", "24h", "7d"):
        coverage = series.coverage(window, required=2)
        coverages.append(coverage)
        points = series.window(_WINDOW_HOURS[window])
        if len(points) >= 2 and points[0].reference_price > 0:
            values[window] = points[-1].reference_price / points[0].reference_price - 1.0
        else:
            values[window] = None

    return ReturnsState(
        change_1h=values["1h"],
        change_24h=values["24h"],
        change_7d=values["7d"],
        windows=tuple(coverages),
    )


def wilder_rsi(prices: Sequence[float], *, period: int = 14) -> float | None:
    """Return Wilder's relative strength index.

    A measurement of the input series, not a recommendation. Atlas does not emit buy or
    sell labels (see ``docs/MARKET_STATE.md``).
    """
    if len(prices) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in itertools.pairwise(prices):
        change = current - previous
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))

    average_gain = math.fsum(gains[:period]) / period
    average_loss = math.fsum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period

    if average_loss == 0:
        # No losses in the window. 100 is the defined limit, not a missing value.
        return 100.0 if average_gain > 0 else 50.0
    rs = average_gain / average_loss
    return 100.0 - 100.0 / (1.0 + rs)


def exponential_moving_average(prices: Sequence[float], *, span: int) -> float | None:
    """Return an exponential moving average with ``alpha = 2 / (span + 1)``."""
    if len(prices) < span:
        return None
    alpha = 2.0 / (span + 1)
    value = prices[0]
    for price in prices[1:]:
        value = alpha * price + (1.0 - alpha) * value
    return value


def compute_technical(series: PriceSeries, history: HistoryConfig) -> TechnicalState:
    """Compute technical indicators over the reference-price series."""
    prices = [p.reference_price for p in series.points]
    rsi_required = history.required_for("rsi_14")
    rsi = wilder_rsi(prices) if len(prices) >= rsi_required else None

    ema_12 = exponential_moving_average(prices, span=12)
    ema_26 = exponential_moving_average(prices, span=26)
    macd = ema_12 - ema_26 if ema_12 is not None and ema_26 is not None else None

    bollinger = None
    if len(prices) >= 20:
        recent = prices[-20:]
        mean = math.fsum(recent) / len(recent)
        deviation = standard_deviation(recent)
        if deviation is not None and mean > 0:
            # Width of a two-standard-deviation band, as a fraction of the mean.
            bollinger = 4.0 * deviation / mean * 10_000.0

    return TechnicalState(
        rsi_14=rsi,
        ema_12=ema_12,
        ema_26=ema_26,
        macd=macd,
        # A signal line needs a history of MACD values, which a single snapshot pass does
        # not have. Null rather than an approximation from the price series.
        macd_signal=None,
        bollinger_width_bps=bollinger,
        observations_used=len(prices),
    )


def window_percentile(
    current: float | None,
    history_values: Sequence[float],
    *,
    window: str,
    required: int,
    history: HistoryConfig,
) -> tuple[float | None, WindowCoverage]:
    """Return the percentile rank of ``current`` within its own history.

    Returns ``None`` when the window is too short or too sparse. A 30-day percentile
    computed from two days of data is not a 30-day percentile, and labelling it as one
    would be the most misleading thing Atlas could publish.
    """
    hours = _WINDOW_HOURS.get(window, 720)
    expected = max(1, hours * 60 // _SLOT_MINUTES)
    coverage = WindowCoverage(
        window=window,
        observations_present=len(history_values),
        observations_expected=expected,
        observations_required=required,
    )
    if current is None or not _sufficient(coverage, history):
        return None, coverage
    return percentile_rank(current, history_values), coverage
