"""Robust statistics.

Cross-venue aggregation must survive one venue reporting nonsense. A simple mean does
not: one venue quoting a different asset under the same ticker, or a stale price from a
halted market, moves an arithmetic mean by an arbitrary amount.

Everything here is deterministic. Inputs are sorted before aggregation so that the same
observations always produce the same number, bit for bit, regardless of the order they
arrived in (see ``docs/METHODOLOGY.md`` on determinism).
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> float | None:
    """Return the weighted median of ``values``.

    The weighted median is the value at which cumulative weight reaches half the total.
    It is the estimator Atlas uses for the global reference price, because a venue's
    influence is bounded by its weight rather than by its distance from the others: a
    venue quoting a price ten times too high moves a weighted mean enormously and the
    weighted median not at all.

    When cumulative weight lands exactly on half the total, the two straddling values are
    averaged, which keeps the estimator symmetric under reversal of the input.

    Args:
        values: The observations.
        weights: Non-negative weights, the same length as ``values``.

    Returns:
        The weighted median, or ``None`` when there is nothing to aggregate.

    Raises:
        ValueError: If the lengths differ or a weight is negative.
    """
    if len(values) != len(weights):
        raise ValueError("values and weights must be the same length")
    if any(w < 0 for w in weights):
        raise ValueError("weights must be non-negative")

    pairs = [(v, w) for v, w in zip(values, weights, strict=True) if w > 0 and math.isfinite(v)]
    if not pairs:
        return None
    if len(pairs) == 1:
        return pairs[0][0]

    # Sorting by value is what makes this a median; sorting by weight as a tiebreak
    # keeps the result identical for inputs that differ only in order.
    pairs.sort(key=lambda pair: (pair[0], pair[1]))
    total = math.fsum(weight for _, weight in pairs)
    half = total / 2.0

    cumulative = 0.0
    for index, (value, weight) in enumerate(pairs):
        cumulative += weight
        if cumulative > half:
            return value
        if cumulative == half:
            # Exactly on the boundary: average with the next value so the estimator is
            # symmetric. Falling through to the next value instead would make the result
            # depend on which side the sort happened to place equal weights.
            if index + 1 < len(pairs):
                return (value + pairs[index + 1][0]) / 2.0
            return value
    return pairs[-1][0]


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float | None:
    """Return the weighted arithmetic mean, or ``None`` when total weight is zero.

    Provided for metrics where a mean is the correct estimator. Never used for the
    reference price.
    """
    if len(values) != len(weights):
        raise ValueError("values and weights must be the same length")
    pairs = [(v, w) for v, w in zip(values, weights, strict=True) if w > 0 and math.isfinite(v)]
    if not pairs:
        return None
    total = math.fsum(weight for _, weight in pairs)
    if total <= 0:
        return None
    return math.fsum(value * weight for value, weight in pairs) / total


def median(values: Sequence[float]) -> float | None:
    """Return the unweighted median."""
    finite = sorted(v for v in values if math.isfinite(v))
    if not finite:
        return None
    middle = len(finite) // 2
    if len(finite) % 2 == 1:
        return finite[middle]
    return (finite[middle - 1] + finite[middle]) / 2.0


def weighted_mad(
    values: Sequence[float], weights: Sequence[float], *, centre: float | None = None
) -> float | None:
    """Return the weighted median absolute deviation from ``centre``.

    Atlas's dispersion measure. Unlike a standard deviation, one erroneous venue widens
    it by a bounded amount rather than dominating it, so the metric stays informative
    when a venue misbehaves.

    Args:
        values: The observations.
        weights: Non-negative weights.
        centre: The reference point; defaults to the weighted median of ``values``.
    """
    reference = centre if centre is not None else weighted_median(values, weights)
    if reference is None:
        return None
    deviations = [abs(v - reference) for v in values]
    return weighted_median(deviations, weights)


def quantile(values: Sequence[float], q: float) -> float | None:
    """Return the ``q`` quantile using linear interpolation between order statistics.

    Raises:
        ValueError: If ``q`` is outside ``[0, 1]``.
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be between 0 and 1")
    finite = sorted(v for v in values if math.isfinite(v))
    if not finite:
        return None
    if len(finite) == 1:
        return finite[0]
    position = q * (len(finite) - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, len(finite) - 1)
    fraction = position - lower
    return finite[lower] * (1.0 - fraction) + finite[upper] * fraction


def cap_weights(weights: Sequence[float], *, maximum_share: float) -> tuple[float, ...]:
    """Normalise weights so no single entry exceeds ``maximum_share`` of the total.

    Without a cap, the venue reporting the largest volume effectively becomes the price,
    which defeats the purpose of aggregating across venues. Reported volume also spans
    several orders of magnitude and is self-reported, so letting it scale weight without
    bound would hand the estimator to whichever venue reports the largest number.

    Excess weight above the cap is redistributed proportionally among the uncapped
    entries, repeatedly, until either no entry exceeds the cap or every entry is capped.
    Iteration is needed because redistributing can push a previously compliant entry over
    the cap.

    Args:
        weights: Non-negative raw weights.
        maximum_share: The largest share any one entry may hold, in ``(0, 1]``.

    Returns:
        Weights summing to 1, or all zeros when the input has no positive weight.

    Raises:
        ValueError: If ``maximum_share`` is outside ``(0, 1]``.
    """
    if not 0.0 < maximum_share <= 1.0:
        raise ValueError("maximum_share must be in (0, 1]")
    count = len(weights)
    if count == 0:
        return ()

    total = math.fsum(weights)
    if total <= 0:
        return tuple(0.0 for _ in weights)

    # With few entries the cap may be unreachable: three venues cannot each hold 0.35
    # of the total. In that case equal weights are the closest satisfiable answer.
    if maximum_share * count <= 1.0:
        return tuple(1.0 / count for _ in weights)

    shares = [w / total for w in weights]
    for _ in range(count):
        excess = math.fsum(max(0.0, s - maximum_share) for s in shares)
        if excess <= 1e-12:
            break
        headroom = math.fsum(maximum_share - s for s in shares if s < maximum_share)
        if headroom <= 1e-12:
            break
        shares = [
            maximum_share
            if s >= maximum_share
            else s + excess * (maximum_share - s) / headroom
            for s in shares
        ]
    # Renormalise to correct accumulated floating-point drift.
    final_total = math.fsum(shares)
    return tuple(s / final_total for s in shares) if final_total > 0 else tuple(shares)


def herfindahl(shares: Sequence[float]) -> float | None:
    """Return the Herfindahl-Hirschman index of ``shares``.

    Ranges from ``1/n`` for ``n`` equal participants to 1 for a single one. Atlas
    publishes it as a structural measurement and attaches no judgement to any value.
    """
    positive = [s for s in shares if s > 0 and math.isfinite(s)]
    if not positive:
        return None
    total = math.fsum(positive)
    if total <= 0:
        return None
    return math.fsum((s / total) ** 2 for s in positive)


def effective_count(shares: Sequence[float]) -> float | None:
    """Return the inverse Herfindahl: the effective number of participants.

    Five equal venues give 5; five venues where one dominates give close to 1. More
    readable than the index itself for a coverage metric.
    """
    index = herfindahl(shares)
    if index is None or index <= 0:
        return None
    return 1.0 / index


def trimmed_mean(values: Sequence[float], *, trim_fraction: float = 0.1) -> float | None:
    """Return the mean after discarding ``trim_fraction`` from each tail.

    Raises:
        ValueError: If ``trim_fraction`` is not in ``[0, 0.5)``.
    """
    if not 0.0 <= trim_fraction < 0.5:
        raise ValueError("trim_fraction must be in [0, 0.5)")
    finite = sorted(v for v in values if math.isfinite(v))
    if not finite:
        return None
    cut = int(len(finite) * trim_fraction)
    kept = finite[cut : len(finite) - cut] or finite
    return math.fsum(kept) / len(kept)


def standard_deviation(values: Sequence[float]) -> float | None:
    """Return the sample standard deviation, or ``None`` below two observations."""
    finite = [v for v in values if math.isfinite(v)]
    if len(finite) < 2:
        return None
    mean = math.fsum(finite) / len(finite)
    variance = math.fsum((v - mean) ** 2 for v in finite) / (len(finite) - 1)
    return math.sqrt(variance)


def percentile_rank(value: float, population: Sequence[float]) -> float | None:
    """Return the percentile rank of ``value`` within ``population``, in ``[0, 100]``.

    Uses the midpoint of the tied range so that a value equal to every observation ranks
    at 50 rather than at 0 or 100.
    """
    finite = [v for v in population if math.isfinite(v)]
    if not finite or not math.isfinite(value):
        return None
    below = sum(1 for v in finite if v < value)
    equal = sum(1 for v in finite if v == value)
    return (below + equal / 2.0) / len(finite) * 100.0
