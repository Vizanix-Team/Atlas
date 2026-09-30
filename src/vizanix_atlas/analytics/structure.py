"""Market structure: fragmentation, pressure, crowding and the Market Genome.

The unifying decision here is that Atlas publishes **components, not scores**. A single
number like ``pressure = 83`` cannot be checked, cannot be reproduced when the formula
changes, and invites being read as a recommendation. A vector of named, individually
documented components can be audited and can be recombined by whoever needs a summary.

Where a composite is published (``fragmentation_index``), its components are published
alongside it and the formula is in ``docs/METHODOLOGY.md``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from vizanix_atlas.analytics.robust import effective_count, herfindahl, percentile_rank
from vizanix_atlas.core.config import GenomeConfig
from vizanix_atlas.core.numeric import safe_divide
from vizanix_atlas.models.enums import GenomeStatus
from vizanix_atlas.models.quality import WindowCoverage
from vizanix_atlas.models.state import (
    CrowdingComponents,
    FragmentationState,
    GenomeState,
    LiquidityState,
    PressureComponents,
)

#: Price dispersion at which the dispersion component of the fragmentation index
#: saturates. Chosen so that a normally arbitraged major asset sits near zero and a
#: genuinely fragmented microcap sits near one; stated in docs/METHODOLOGY.md.
_DISPERSION_SATURATION_BPS: Final = 50.0


def compute_fragmentation(
    *,
    venue_count: int,
    volume_shares: dict[str, float],
    liquidity: LiquidityState,
    price_dispersion_bps: float | None,
    funding_dispersion_bps: float | None,
    spot_median_usd: float | None,
    derivative_median_usd: float | None,
    reference_price: float | None,
) -> FragmentationState:
    """Quantify how fragmented an asset's market is across venues.

    The composite index averages three bounded components, each in ``[0, 1]``:

    ``venue breadth``
        ``1 - 1 / effective_venue_count``. Rises as trading spreads across more venues.
    ``venue balance``
        ``1 - largest_venue_volume_share``. Falls when one venue dominates.
    ``price separation``
        ``min(price_dispersion_bps / 50, 1)``. Rises when venues disagree on price.

    Returned as ``None`` below two venues, where fragmentation is not a meaningful
    question, rather than as zero.
    """
    shares = list(volume_shares.values())
    concentration = herfindahl(shares)
    effective = effective_count(shares)
    largest = max(shares) if shares else None

    spot_derivative = None
    if (
        spot_median_usd is not None
        and derivative_median_usd is not None
        and reference_price
        and reference_price > 0
    ):
        spot_derivative = abs(spot_median_usd - derivative_median_usd) / reference_price

    index: float | None = None
    if venue_count >= 2 and effective is not None and largest is not None:
        components = [
            1.0 - 1.0 / effective if effective > 0 else 0.0,
            1.0 - largest,
        ]
        if price_dispersion_bps is not None:
            components.append(min(price_dispersion_bps / _DISPERSION_SATURATION_BPS, 1.0))
        index = min(1.0, max(0.0, math.fsum(components) / len(components)))

    return FragmentationState(
        venue_count=venue_count,
        effective_venue_count=effective,
        volume_concentration_hhi=concentration,
        liquidity_concentration_hhi=liquidity.concentration_hhi,
        largest_venue_volume_share=largest,
        price_dispersion_bps=price_dispersion_bps,
        funding_dispersion_bps=funding_dispersion_bps,
        spot_derivative_fragmentation=spot_derivative,
        fragmentation_index=index,
    )


@dataclass(frozen=True, slots=True)
class PressureInputs:
    """Everything needed to compute the pressure vector.

    Historical series are passed in rather than fetched, so that the computation is pure
    and therefore reproducible from a frozen release.
    """

    book_imbalance_50bps: float | None
    funding_8h_current: float | None
    funding_8h_history: Sequence[float]
    basis_bps_current: float | None
    basis_bps_history: Sequence[float]
    open_interest_usd_current: float | None
    open_interest_usd_previous: float | None
    volume_usd_current: float | None
    volume_usd_baseline: float | None
    baseline_window: str
    baseline_coverage: WindowCoverage | None


def _standardise(current: float | None, history: Sequence[float]) -> float | None:
    """Return how far ``current`` sits from its own recent distribution, in z units.

    Requires at least eight observations and a non-degenerate spread. Below that a z-score
    is a number without meaning, so ``None`` is returned instead.
    """
    if current is None or len(history) < 8:
        return None
    mean = math.fsum(history) / len(history)
    variance = math.fsum((v - mean) ** 2 for v in history) / (len(history) - 1)
    deviation = math.sqrt(variance)
    if deviation <= 0:
        return None
    result = (current - mean) / deviation
    return result if math.isfinite(result) else None


def compute_pressure(inputs: PressureInputs) -> PressureComponents:
    """Build the market-pressure vector.

    Components that cannot be computed are ``None``, and ``components_available`` counts
    the ones that could. Two assets' vectors are only comparable when the same components
    are present, which is why that count is published rather than left to be inferred.
    """
    oi_change = None
    if (
        inputs.open_interest_usd_current is not None
        and inputs.open_interest_usd_previous is not None
        and inputs.open_interest_usd_previous > 0
    ):
        oi_change = inputs.open_interest_usd_current / inputs.open_interest_usd_previous - 1.0

    acceleration = safe_divide(inputs.volume_usd_current, inputs.volume_usd_baseline)

    components = PressureComponents(
        book_imbalance=inputs.book_imbalance_50bps,
        funding_deviation_z=_standardise(inputs.funding_8h_current, inputs.funding_8h_history),
        basis_deviation_z=_standardise(inputs.basis_bps_current, inputs.basis_bps_history),
        oi_change=oi_change,
        volume_acceleration=acceleration,
        # Aggressive flow requires a public trade tape, which Atlas does not collect.
        # Left null rather than approximated from something that is not flow.
        spot_flow_proxy=None,
        derivative_flow_proxy=None,
        baseline_window=inputs.baseline_window,
        baseline_coverage=inputs.baseline_coverage,
    )
    available = sum(
        1
        for value in (
            components.book_imbalance,
            components.funding_deviation_z,
            components.basis_deviation_z,
            components.oi_change,
            components.volume_acceleration,
        )
        if value is not None
    )
    return components.model_copy(update={"components_available": available})


@dataclass(frozen=True, slots=True)
class CrowdingInputs:
    """Everything needed to compute the crowding vector."""

    funding_8h_current: float | None
    funding_8h_history_30d: Sequence[float]
    funding_coverage: WindowCoverage | None
    basis_bps_current: float | None
    basis_bps_history_30d: Sequence[float]
    oi_to_volume: float | None
    perp_dominance: float | None
    open_interest_usd_current: float | None
    open_interest_usd_24h_ago: float | None


def compute_crowding(inputs: CrowdingInputs) -> CrowdingComponents:
    """Build the positioning-crowding vector.

    Percentiles are published only when a genuine window exists. Atlas will not compute a
    "30-day percentile" from a week of data.
    """
    sufficient = inputs.funding_coverage is not None and inputs.funding_coverage.sufficient

    funding_percentile = (
        percentile_rank(inputs.funding_8h_current, inputs.funding_8h_history_30d)
        if sufficient and inputs.funding_8h_current is not None
        else None
    )
    basis_percentile = (
        percentile_rank(inputs.basis_bps_current, inputs.basis_bps_history_30d)
        if sufficient and inputs.basis_bps_current is not None
        else None
    )

    oi_change_24h = None
    if (
        inputs.open_interest_usd_current is not None
        and inputs.open_interest_usd_24h_ago is not None
        and inputs.open_interest_usd_24h_ago > 0
    ):
        oi_change_24h = inputs.open_interest_usd_current / inputs.open_interest_usd_24h_ago - 1.0

    components = CrowdingComponents(
        funding_percentile_30d=funding_percentile,
        oi_to_volume=inputs.oi_to_volume,
        perp_dominance=inputs.perp_dominance,
        oi_change_24h=oi_change_24h,
        basis_percentile_30d=basis_percentile,
        baseline_coverage=inputs.funding_coverage,
    )
    available = sum(
        1
        for value in (
            funding_percentile,
            inputs.oi_to_volume,
            inputs.perp_dominance,
            oi_change_24h,
            basis_percentile,
        )
        if value is not None
    )
    return components.model_copy(update={"components_available": available})


def compute_genome(
    *,
    observations_available: int,
    config: GenomeConfig,
    features: dict[str, float] | None = None,
) -> GenomeState:
    """Return the Market Genome lifecycle state and, when available, its features.

    Three states, and the reason they exist: at launch Atlas has no history, so a
    behavioural fingerprint would be fabricated. Rather than emit one, Atlas reports
    ``insufficient_history``, then ``warming_up`` once a week of observations exists, then
    ``available`` at the configured minimum (28 days at the default cadence).

    Features are empty unless the status is ``available``. There is no partial genome.
    """
    if observations_available >= config.minimum_observations:
        status = GenomeStatus.AVAILABLE
        emitted = dict(sorted((features or {}).items()))
    elif observations_available >= config.warming_up_threshold:
        status = GenomeStatus.WARMING_UP
        emitted = {}
    else:
        status = GenomeStatus.INSUFFICIENT_HISTORY
        emitted = {}

    return GenomeState(
        status=status,
        observations_available=observations_available,
        observations_required=config.minimum_observations,
        features=emitted,
        feature_version=config.feature_version if status is GenomeStatus.AVAILABLE else None,
    )


def genome_features(
    *,
    volatility_24h: float | None,
    depth_50bps_usd: float | None,
    venue_count: int,
    effective_venue_count: float | None,
    perp_dominance: float | None,
    funding_8h_median: float | None,
    price_dispersion_bps: float | None,
    oi_to_volume: float | None,
) -> dict[str, float]:
    """Build the genome feature vector from an asset's current behaviour.

    Features are normalised to comparable ranges so that similarity search is not
    dominated by whichever feature has the largest units. Log scaling is used for depth,
    which spans many orders of magnitude across assets.

    Only features with a value are included, so the vector's length says how much is known
    and similarity can require an overlapping basis.
    """
    features: dict[str, float] = {}
    if volatility_24h is not None:
        features["volatility_24h"] = volatility_24h
    if depth_50bps_usd is not None and depth_50bps_usd > 0:
        features["log_depth_50bps"] = math.log10(depth_50bps_usd)
    if venue_count > 0:
        features["venue_count"] = float(venue_count)
    if effective_venue_count is not None:
        features["effective_venue_count"] = effective_venue_count
    if perp_dominance is not None:
        features["perp_dominance"] = perp_dominance
    if funding_8h_median is not None:
        features["funding_8h"] = funding_8h_median
    if price_dispersion_bps is not None:
        features["price_dispersion_bps"] = price_dispersion_bps
    if oi_to_volume is not None:
        features["oi_to_volume"] = oi_to_volume
    return features


def genome_distance(
    left: dict[str, float], right: dict[str, float]
) -> tuple[float | None, tuple[str, ...]]:
    """Return the normalised Euclidean distance between two genome vectors.

    Computed only over features both assets have, and the shared feature names are
    returned alongside the distance so a consumer can see what the comparison rested on.
    Two assets compared on three features are not comparable to two compared on eight, and
    this makes that visible.

    Returns ``(None, ())`` when fewer than three features overlap.
    """
    shared = tuple(sorted(set(left) & set(right)))
    if len(shared) < 3:
        return None, ()
    squared = math.fsum((left[key] - right[key]) ** 2 for key in shared)
    # Divided by the feature count so vectors of different width stay comparable.
    return math.sqrt(squared / len(shared)), shared
