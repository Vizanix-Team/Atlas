"""Volume aggregation.

Every volume figure Atlas publishes is described as **reported**. Venues self-report
volume, Atlas does not verify it, and the documentation says so wherever a figure appears.

Three rules keep the totals defensible:

1. **Deduplicate first.** A venue listing the same pair twice would otherwise contribute
   twice. Deduplication happens in
   :func:`~vizanix_atlas.quality.validation.deduplicate_instruments`.
2. **Only convert what the venue quoted.** A venue publishing base volume but not quote
   volume is excluded from the USD total rather than having one derived by multiplying by
   a price it never quoted against. That derivation would differ from how every other
   venue computes its own figure, making the totals inconsistent.
3. **Segment by market type.** Spot, perpetual, dated futures and option volume are
   summed separately. Combining them is offered as a convenience, with the caveat that
   they are not equivalent exposures.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from vizanix_atlas.core.numeric import safe_divide
from vizanix_atlas.models.enums import InstrumentType
from vizanix_atlas.models.instrument import Instrument
from vizanix_atlas.models.state import VolumeState


@dataclass(frozen=True, slots=True)
class VolumeContribution:
    """One instrument's reported volume, valued in USD where possible."""

    venue_slug: str
    instrument: Instrument
    reported_base_volume: float | None
    reported_quote_volume: float | None
    #: USD-normalised quote volume. ``None`` when the venue published no quote volume or
    #: no conversion was available, which excludes it from the totals.
    reported_volume_usd: float | None

    @property
    def counts_toward_usd_total(self) -> bool:
        """Whether this contribution enters the published USD totals."""
        return self.reported_volume_usd is not None and self.reported_volume_usd >= 0


def value_volume(
    *,
    venue_slug: str,
    instrument: Instrument,
    base_volume: float | None,
    quote_volume: float | None,
    quote_to_usd: float | None,
) -> VolumeContribution:
    """Express one instrument's reported volume in USD, if the venue quoted it.

    Deliberately conservative: only a venue-published quote volume is converted. Deriving
    quote volume from base volume times a price would invent a figure and would be
    inconsistent with venues that publish it directly.
    """
    usd: float | None = None
    if quote_volume is not None and quote_to_usd is not None and quote_to_usd > 0:
        usd = quote_volume * quote_to_usd

    return VolumeContribution(
        venue_slug=venue_slug,
        instrument=instrument,
        reported_base_volume=base_volume,
        reported_quote_volume=quote_volume,
        reported_volume_usd=usd,
    )


def aggregate_volume(contributions: Sequence[VolumeContribution]) -> VolumeState:
    """Sum reported volume, segmented by market type.

    A segment with no usable contribution is ``None`` rather than ``0.0``: an asset with
    no reported perpetual volume data is different from one with genuinely no perpetual
    trading.
    """
    if not contributions:
        return VolumeState()

    def total(kind: InstrumentType) -> float | None:
        values = [
            c.reported_volume_usd
            for c in contributions
            if c.instrument.instrument_type is kind and c.counts_toward_usd_total
        ]
        return math.fsum(values) if values else None

    base_values = [
        c.reported_base_volume
        for c in contributions
        if c.instrument.instrument_type is InstrumentType.SPOT
        and c.reported_base_volume is not None
    ]

    return VolumeState(
        reported_spot_volume_24h_usd=total(InstrumentType.SPOT),
        reported_perp_volume_24h_usd=total(InstrumentType.PERPETUAL),
        reported_futures_volume_24h_usd=total(InstrumentType.FUTURE),
        reported_option_volume_24h_usd=total(InstrumentType.OPTION),
        reported_base_volume_24h=math.fsum(base_values) if base_values else None,
        instrument_count=len(contributions),
        venues_reporting_quote_volume=len(
            {c.venue_slug for c in contributions if c.counts_toward_usd_total}
        ),
    )


def venue_volume_shares(
    contributions: Sequence[VolumeContribution],
) -> dict[str, float]:
    """Return each venue's share of the asset's reported USD volume.

    Returns an empty mapping when no contribution could be valued, rather than dividing
    by zero or assigning equal shares.
    """
    per_venue: dict[str, float] = {}
    for contribution in contributions:
        if not contribution.counts_toward_usd_total:
            continue
        assert contribution.reported_volume_usd is not None
        per_venue[contribution.venue_slug] = (
            per_venue.get(contribution.venue_slug, 0.0) + contribution.reported_volume_usd
        )

    total = math.fsum(per_venue.values())
    if total <= 0:
        return {}
    return {venue: value / total for venue, value in sorted(per_venue.items())}


def derivative_volume_usd(state: VolumeState) -> float | None:
    """Return combined perpetual and dated-futures reported volume."""
    parts = [
        v
        for v in (state.reported_perp_volume_24h_usd, state.reported_futures_volume_24h_usd)
        if v is not None
    ]
    return math.fsum(parts) if parts else None


def perp_dominance(state: VolumeState) -> float | None:
    """Return the perpetual share of combined spot and perpetual reported volume."""
    perp = state.reported_perp_volume_24h_usd
    spot = state.reported_spot_volume_24h_usd
    if perp is None or spot is None:
        return None
    return safe_divide(perp, perp + spot)
