"""The global reference price.

Atlas publishes ``reference_price``, not ``price``. There is no single true price for an
asset traded on sixteen venues in four quote currencies; there is a defensible estimate
of where the market is, and a record of how it was reached.

Why not an arithmetic mean
--------------------------
A mean is sensitive to a single bad input without bound. One venue quoting a different
asset under the same ticker, or a halted market's last price from hours ago, moves a mean
arbitrarily far. Atlas therefore uses a **weighted median**, in which a venue's influence
is bounded by its weight and is independent of how wrong its price is.

The weighting
-------------
Each qualifying venue observation gets a weight that is the product of three factors:

``volume``
    ``(reported USD volume) ** volume_weight_exponent``, with the exponent at 0.5 by
    default. A square root rewards real liquidity while compressing the several orders of
    magnitude between the largest and smallest venues. A venue reporting no usable volume
    gets the smallest non-zero weight rather than zero, so it still counts as a source.

``freshness``
    Decays linearly from 1 at zero age to ``min_freshness_weight`` at the staleness
    limit. A venue whose data is nearly stale contributes, but less.

``source quality``
    A mid price scores above a last price, because a mid is a live two-sided quote while
    a last price may be a single old trade.

Weights are then **capped** so that no venue exceeds ``max_venue_weight`` of the total
(see :func:`~vizanix_atlas.analytics.robust.cap_weights`). Without the cap the largest
venue's self-reported volume would effectively determine the price.

Exclusions
----------
No observation is silently dropped. Every one is recorded in provenance as included, or
as excluded with a reason, and ``included + excluded`` always equals the number
considered. Outlier rejection runs against a provisional robust centre computed from the
observations themselves, so the comparison is against the market rather than against any
single venue.

Fallback
--------
Spot is preferred. If no spot venue qualifies, Atlas will use derivative index and mark
prices, and records ``reference_price_method = derivative_fallback`` so the substitution
is visible rather than implied.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from vizanix_atlas.analytics.robust import (
    cap_weights,
    quantile,
    weighted_mad,
    weighted_median,
)
from vizanix_atlas.core.config import QualityConfig, ReferencePriceConfig
from vizanix_atlas.core.numeric import relative_bps
from vizanix_atlas.models.enums import (
    ExclusionReason,
    InstrumentType,
    PriceSource,
    ReferencePriceMethod,
)
from vizanix_atlas.models.instrument import Instrument
from vizanix_atlas.models.quality import Provenance, ProvenanceEntry, QuoteConversion
from vizanix_atlas.models.state import Dispersion, ReferencePrice

#: Relative weight by which price source the observation came from. A mid is a live
#: two-sided quote; a last price may be one stale trade; a mark or index price is the
#: venue's own derived value and is used only in the derivative fallback.
_SOURCE_QUALITY: Final = {
    PriceSource.MID: 1.0,
    PriceSource.LAST: 0.6,
    PriceSource.MARK: 0.5,
    PriceSource.INDEX: 0.5,
}

#: Smallest weight a qualifying observation can receive. A venue with no usable volume
#: figure is still a price source, so it must not be weighted to zero, which would
#: remove it from the median entirely.
_MINIMUM_WEIGHT: Final = 1e-3


@dataclass(frozen=True, slots=True)
class PriceCandidate:
    """One venue observation eligible to contribute to a reference price."""

    venue_slug: str
    instrument: Instrument
    price_usd: float
    price_source: PriceSource
    observed_at: int | None
    age_seconds: float | None
    raw_price: float
    reported_volume_usd: float | None
    conversion: QuoteConversion | None

    @property
    def is_spot(self) -> bool:
        """Whether this candidate comes from a spot market."""
        return self.instrument.instrument_type is InstrumentType.SPOT


@dataclass(frozen=True, slots=True)
class ReferencePriceResult:
    """A reference price, its dispersion and the full evidence behind it."""

    reference_price: ReferencePrice
    dispersion: Dispersion
    provenance: Provenance
    weights: dict[str, float]

    @property
    def value(self) -> float | None:
        """The reference price itself, or ``None`` when none could be produced."""
        return self.reference_price.value


def _freshness_weight(
    age_seconds: float | None, *, quality: QualityConfig, config: ReferencePriceConfig
) -> float:
    """Return the freshness factor for an observation of the given age.

    An observation with no venue timestamp gets the floor rather than full credit: Atlas
    cannot show it is fresh, so it should not be treated as though it were.
    """
    if age_seconds is None:
        return config.min_freshness_weight
    if age_seconds <= 0:
        return 1.0
    limit = quality.max_observation_age_seconds
    if age_seconds >= limit:
        return config.min_freshness_weight
    decay = 1.0 - (age_seconds / limit)
    return config.min_freshness_weight + (1.0 - config.min_freshness_weight) * decay


def _raw_weight(
    candidate: PriceCandidate, *, quality: QualityConfig, config: ReferencePriceConfig
) -> float:
    """Return a candidate's weight before capping and normalisation."""
    volume = candidate.reported_volume_usd
    volume_factor = (
        volume**config.volume_weight_exponent if volume is not None and volume > 0 else 0.0
    )
    if volume_factor <= 0.0:
        # No usable volume figure. Still a price source, so it gets the floor rather
        # than zero, which would drop it out of the median altogether.
        volume_factor = _MINIMUM_WEIGHT
    freshness = _freshness_weight(candidate.age_seconds, quality=quality, config=config)
    source = _SOURCE_QUALITY.get(candidate.price_source, 0.5)
    return max(_MINIMUM_WEIGHT, volume_factor * freshness * source)


def compute_reference_price(
    candidates: Sequence[PriceCandidate],
    *,
    quality: QualityConfig,
    config: ReferencePriceConfig,
    methodology_version: str,
    excluded_before: Sequence[tuple[PriceCandidate, ExclusionReason]] = (),
) -> ReferencePriceResult:
    """Compute the global reference price for one asset.

    Args:
        candidates: Observations that passed validation and have a USD price.
        quality: Staleness and deviation thresholds.
        config: Weighting and capping parameters.
        methodology_version: Recorded in provenance so a value can be reproduced.
        excluded_before: Observations already rejected upstream, with their reasons, so
            that provenance accounts for every observation considered rather than only
            those that reached this function.

    Returns:
        The reference price, dispersion, provenance and the final weights.

    """
    entries: list[ProvenanceEntry] = [
        _entry(candidate, included=False, weight=None, reason=reason)
        for candidate, reason in excluded_before
    ]

    if not candidates:
        return _unavailable(entries, methodology_version, len(excluded_before))

    # Spot is preferred. Derivatives are used only when no spot venue qualifies, and the
    # method field records that it happened.
    spot = [c for c in candidates if c.is_spot]
    if spot:
        eligible, method = spot, ReferencePriceMethod.SPOT_WEIGHTED_MEDIAN
    elif config.allow_derivative_fallback:
        eligible, method = list(candidates), ReferencePriceMethod.DERIVATIVE_FALLBACK
    else:
        return _unavailable(entries, methodology_version, len(excluded_before))

    # Derivative observations set aside because spot was available are recorded, so a
    # reader can see they existed and why they were not used.
    if spot:
        for candidate in candidates:
            if not candidate.is_spot:
                entries.append(
                    _entry(
                        candidate,
                        included=False,
                        weight=None,
                        reason=ExclusionReason.SPOT_PREFERRED,
                    )
                )

    # A provisional centre from the eligible observations, so outlier rejection compares
    # against the market rather than against any single venue.
    provisional_weights = [_raw_weight(c, quality=quality, config=config) for c in eligible]
    provisional_centre = weighted_median([c.price_usd for c in eligible], provisional_weights)

    kept: list[PriceCandidate] = []
    for candidate in eligible:
        if provisional_centre is not None and provisional_centre > 0:
            deviation = (
                abs(candidate.price_usd - provisional_centre) / provisional_centre * 10_000.0
            )
            if deviation > quality.max_deviation_bps:
                entries.append(
                    _entry(
                        candidate,
                        included=False,
                        weight=None,
                        reason=ExclusionReason.EXTREME_DEVIATION,
                    )
                )
                continue
        kept.append(candidate)

    if not kept:
        return _unavailable(entries, methodology_version, len(excluded_before) + len(eligible))

    raw_weights = [_raw_weight(c, quality=quality, config=config) for c in kept]
    final_weights = cap_weights(raw_weights, maximum_share=config.max_venue_weight)
    prices = [c.price_usd for c in kept]

    value = weighted_median(prices, list(final_weights))
    if value is None or value <= 0:
        return _unavailable(entries, methodology_version, len(excluded_before) + len(eligible))

    if len(kept) == 1 and method is ReferencePriceMethod.SPOT_WEIGHTED_MEDIAN:
        # One venue is still a published price, but calling it a cross-venue median
        # would overstate what it is.
        method = ReferencePriceMethod.SINGLE_VENUE_SPOT

    weights_by_venue: dict[str, float] = {}
    for candidate, weight in zip(kept, final_weights, strict=True):
        entries.append(_entry(candidate, included=True, weight=weight, reason=None))
        weights_by_venue[candidate.venue_slug] = (
            weights_by_venue.get(candidate.venue_slug, 0.0) + weight
        )

    dispersion = _dispersion(prices, list(final_weights), centre=value, count=len(kept))
    included = [e for e in entries if e.included]
    excluded = [e for e in entries if not e.included]

    reference = ReferencePrice(
        value=value,
        method=method,
        venue_count=len({c.venue_slug for c in kept}),
        included_venue_count=len(included),
        excluded_venue_count=len(excluded),
        excluded_reasons=_reason_counts(excluded),
        min_qualified_price=min(prices),
        max_qualified_price=max(prices),
    )
    return ReferencePriceResult(
        reference_price=reference,
        dispersion=dispersion,
        provenance=Provenance(
            metric="reference_price",
            methodology_version=methodology_version,
            entries=tuple(_sorted_entries(entries)),
        ),
        weights=weights_by_venue,
    )


def _dispersion(
    prices: Sequence[float], weights: Sequence[float], *, centre: float, count: int
) -> Dispersion:
    """Compute cross-venue dispersion around ``centre``.

    Below two venues there is nothing to disperse, so the metrics are null rather than
    zero: one venue agreeing with itself is not zero dispersion.
    """
    if count < 2:
        return Dispersion(contributing_venue_count=count)

    mad = weighted_mad(list(prices), list(weights), centre=centre)
    mad_bps = None if mad is None else mad / centre * 10_000.0
    p10 = quantile(list(prices), 0.10)
    p90 = quantile(list(prices), 0.90)
    spread_bps = (p90 - p10) / centre * 10_000.0 if p10 is not None and p90 is not None else None
    max_deviation = max(abs(relative_bps(p, centre) or 0.0) for p in prices)

    return Dispersion(
        price_dispersion_bps=mad_bps,
        weighted_mad_bps=mad_bps,
        p10_p90_spread_bps=spread_bps if count >= 4 else None,
        max_absolute_deviation_bps=max_deviation,
        contributing_venue_count=count,
    )


def _entry(
    candidate: PriceCandidate,
    *,
    included: bool,
    weight: float | None,
    reason: ExclusionReason | None,
) -> ProvenanceEntry:
    """Build one provenance row."""
    return ProvenanceEntry(
        venue_slug=candidate.venue_slug,
        instrument_id=candidate.instrument.instrument_id,
        symbol_native=candidate.instrument.symbol_native,
        observed_at=candidate.observed_at,
        age_seconds=candidate.age_seconds,
        raw_price=candidate.raw_price,
        price_source=candidate.price_source,
        normalised_price_usd=candidate.price_usd,
        conversion=candidate.conversion,
        weight=weight,
        included=included,
        exclusion_reason=reason,
    )


def _sorted_entries(entries: Sequence[ProvenanceEntry]) -> list[ProvenanceEntry]:
    """Order provenance deterministically: included first, then by venue and instrument."""
    return sorted(entries, key=lambda e: (not e.included, e.venue_slug, e.instrument_id))


def _reason_counts(entries: Sequence[ProvenanceEntry]) -> dict[str, int]:
    """Count exclusion reasons, sorted for stable output."""
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.exclusion_reason is not None:
            key = entry.exclusion_reason.value
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _unavailable(
    entries: Sequence[ProvenanceEntry], methodology_version: str, considered: int
) -> ReferencePriceResult:
    """Build a result for an asset with no usable price.

    The provenance still lists every excluded observation, so a null reference price can
    be explained rather than merely observed.
    """
    excluded = list(entries)
    return ReferencePriceResult(
        reference_price=ReferencePrice(
            value=None,
            method=ReferencePriceMethod.UNAVAILABLE,
            venue_count=0,
            included_venue_count=0,
            excluded_venue_count=len(excluded),
            excluded_reasons=_reason_counts(excluded),
        ),
        dispersion=Dispersion(),
        provenance=Provenance(
            metric="reference_price",
            methodology_version=methodology_version,
            entries=tuple(_sorted_entries(excluded)),
        ),
        weights={},
    )
