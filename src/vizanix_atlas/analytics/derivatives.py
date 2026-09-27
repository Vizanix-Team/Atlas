"""Derivative normalisation.

Three quantities differ enough between venues that normalising them carelessly produces
numbers that look right and are wrong by orders of magnitude.

Funding
-------
Venues publish funding over different intervals and in different forms. Observed across
the supported venues: 1 hour (Hyperliquid, dYdX, Kraken Futures), 2, 4 and 8 hours (OKX,
depending on the contract), and 8 hours (Bitget, Gate.io, Deribit).

So Atlas preserves the native interval and publishes an 8-hour equivalent by **linear
rescaling**: ``rate_8h = rate_raw * 8 / interval_hours``. This is deliberately not
compounded. Compounding would embed an assumption that the current rate persists for
eight hours, which it frequently does not, and would make the published number depend on
that assumption rather than on the observation.

Most venues publish a *relative* rate (a dimensionless fraction). Kraken Futures publishes
an **absolute** rate, a currency amount per contract, and its documentation gives no
conversion. The relation was established from the venue's own
``/v4/historicalfundingrates`` endpoint, which publishes both forms for the same hour:

======================  ==========================  =========================
Contract                Atlas computes              Venue published
======================  ==========================  =========================
``PF_XBTUSD`` (linear)  ``0.18037855 / 84420.35``   ``2.136845833333e-06``
                        ``= 2.13668e-06``
``PI_XBTUSD`` (inverse) ``2.40324183e-10 * 84436``  ``2.0286595833333e-05``
                        ``= 2.029211e-05``
======================  ==========================  =========================

So the relative rate is ``absolute / mark`` for a linear contract and
``absolute * mark`` for an inverse one, agreeing with the venue to within the difference
expected from sampling the reference price a moment apart. A conversion Atlas cannot
justify this way is refused, and the observation is excluded with
``undocumented_funding_semantics``.

Open interest
-------------
Venues report open interest in contracts, base units, quote currency or USD. Atlas keeps
the raw value with its unit and converts only when the unit is known and, for a contract
count, the multiplier is published. Everything else is excluded rather than guessed.

Basis
-----
Always computed from **mark** prices. A last traded price on a thin contract reflects one
stale trade rather than the market, and mixing mark and last across venues would make the
metric incomparable between assets.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from vizanix_atlas.analytics.robust import median
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.core.numeric import relative_bps, safe_divide
from vizanix_atlas.models.enums import (
    ExclusionReason,
    FundingSemantics,
    InstrumentType,
    OpenInterestUnit,
    PriceSource,
)
from vizanix_atlas.models.instrument import Instrument
from vizanix_atlas.models.observations import RawDerivativeObservation
from vizanix_atlas.models.state import BasisState, FundingState, OpenInterestState

_log = get_logger(__name__)

#: Funding is republished on this interval so that venues are comparable.
_TARGET_INTERVAL_HOURS: Final = 8.0

#: Funding periods per year, for simple annualisation: three 8-hour periods a day.
_PERIODS_PER_YEAR: Final = 3 * 365


@dataclass(frozen=True, slots=True)
class FundingObservation:
    """One venue's funding rate, normalised to an 8-hour equivalent."""

    venue_slug: str
    instrument: Instrument
    rate_raw: float
    interval_hours: float
    rate_8h: float
    conversion_note: str | None = None


@dataclass(frozen=True, slots=True)
class OpenInterestObservation:
    """One instrument's open interest, converted where the unit permits."""

    venue_slug: str
    instrument: Instrument
    raw: float
    unit: OpenInterestUnit
    base_equivalent: float | None
    usd_equivalent: float | None
    excluded_reason: ExclusionReason | None = None


def normalise_funding(
    observation: RawDerivativeObservation,
    instrument: Instrument,
    *,
    mark_price: float | None,
) -> FundingObservation | ExclusionReason:
    """Normalise one funding observation to an 8-hour equivalent.

    Returns an :class:`ExclusionReason` rather than raising when the observation cannot be
    normalised, so the caller can record why a venue is missing from the aggregate.
    """
    if observation.funding_rate_raw is None:
        return ExclusionReason.MISSING_PRICE
    if instrument.instrument_type is not InstrumentType.PERPETUAL:
        return ExclusionReason.INACTIVE_INSTRUMENT

    # The observation's interval wins when present, because it was derived from this
    # response's own timestamps. The instrument's value is the venue's declared default.
    interval = observation.funding_interval_hours or instrument.funding_interval_hours
    if interval is None or interval <= 0:
        return ExclusionReason.UNDOCUMENTED_FUNDING_SEMANTICS

    semantics = instrument.funding_semantics or FundingSemantics.UNDOCUMENTED
    if semantics is FundingSemantics.UNDOCUMENTED:
        return ExclusionReason.UNDOCUMENTED_FUNDING_SEMANTICS

    raw = observation.funding_rate_raw
    note: str | None = None
    if semantics is FundingSemantics.ABSOLUTE_PER_INTERVAL:
        if mark_price is None or mark_price <= 0:
            # Without a mark price an absolute rate cannot be made relative.
            return ExclusionReason.UNDOCUMENTED_FUNDING_SEMANTICS
        # Verified against the venue's own published relative rate; see the module
        # docstring and docs/METHODOLOGY.md.
        relative = raw * mark_price if instrument.is_inverse else raw / mark_price
        note = (
            "derived_from_absolute:"
            + ("raw*mark" if instrument.is_inverse else "raw/mark")
        )
    else:
        relative = raw

    if not math.isfinite(relative):
        return ExclusionReason.SCHEMA_FAILURE

    # Linear rescaling, deliberately not compounded.
    rate_8h = relative * _TARGET_INTERVAL_HOURS / interval
    return FundingObservation(
        venue_slug=observation.venue_slug,
        instrument=instrument,
        rate_raw=raw,
        interval_hours=interval,
        rate_8h=rate_8h,
        conversion_note=note,
    )


def aggregate_funding(observations: Sequence[FundingObservation], excluded: int) -> FundingState:
    """Aggregate 8-hour-equivalent funding across venues.

    The median is used rather than a mean, for the same reason the reference price uses
    one: a single venue's misreported rate must not dominate.
    """
    if not observations:
        return FundingState(venue_count=0, excluded_venue_count=excluded)

    rates = [o.rate_8h for o in observations]
    centre = median(rates)
    intervals = tuple(sorted({o.interval_hours for o in observations}))
    return FundingState(
        rate_8h_median=centre,
        rate_8h_min=min(rates),
        rate_8h_max=max(rates),
        annualised_simple=None if centre is None else centre * _PERIODS_PER_YEAR,
        dispersion_bps=(max(rates) - min(rates)) * 10_000.0 if len(rates) >= 2 else None,
        venue_count=len({o.venue_slug for o in observations}),
        excluded_venue_count=excluded,
        intervals_observed_hours=intervals,
    )


def normalise_open_interest(
    observation: RawDerivativeObservation,
    instrument: Instrument,
    *,
    reference_price: float | None,
    quote_to_usd: float | None,
) -> OpenInterestObservation | None:
    """Convert one open-interest observation, where the unit permits.

    Venues that publish a base or USD figure directly are trusted for it, because the
    venue knows its own contract arithmetic better than Atlas can reconstruct it.

    Returns ``None`` when there is no raw value at all. Returns an observation carrying an
    ``excluded_reason`` when a value exists but cannot be converted, so the raw figure
    stays available and the gap is attributable.
    """
    raw = observation.open_interest_raw
    if raw is None:
        return None

    unit = (
        observation.open_interest_unit
        or instrument.open_interest_unit
        or OpenInterestUnit.UNKNOWN
    )

    # A venue-published base or USD figure needs no reconstruction.
    base = observation.open_interest_base
    usd = observation.open_interest_usd

    if usd is None or base is None:
        multiplier = instrument.contract_multiplier
        match unit:
            case OpenInterestUnit.USD:
                usd = usd if usd is not None else raw
                if base is None and reference_price and reference_price > 0:
                    base = usd / reference_price
            case OpenInterestUnit.BASE_ASSET:
                base = base if base is not None else raw
                if usd is None and reference_price and reference_price > 0:
                    usd = base * reference_price
            case OpenInterestUnit.QUOTE_CURRENCY:
                if quote_to_usd is not None and quote_to_usd > 0:
                    usd = usd if usd is not None else raw * quote_to_usd
                    if base is None and reference_price and reference_price > 0:
                        base = usd / reference_price
            case OpenInterestUnit.CONTRACTS:
                if multiplier is None:
                    return OpenInterestObservation(
                        venue_slug=observation.venue_slug,
                        instrument=instrument,
                        raw=raw,
                        unit=unit,
                        base_equivalent=None,
                        usd_equivalent=None,
                        excluded_reason=ExclusionReason.UNKNOWN_CONTRACT_MULTIPLIER,
                    )
                if instrument.is_inverse:
                    # An inverse contract is worth `multiplier` units of the quote
                    # currency, so contracts convert to notional without the price.
                    notional_quote = raw * multiplier
                    if quote_to_usd is not None and quote_to_usd > 0:
                        usd = usd if usd is not None else notional_quote * quote_to_usd
                    if base is None and reference_price and reference_price > 0 and usd is not None:
                        base = usd / reference_price
                else:
                    base = base if base is not None else raw * multiplier
                    if usd is None and reference_price and reference_price > 0:
                        usd = base * reference_price
            case OpenInterestUnit.UNKNOWN:
                return OpenInterestObservation(
                    venue_slug=observation.venue_slug,
                    instrument=instrument,
                    raw=raw,
                    unit=unit,
                    base_equivalent=None,
                    usd_equivalent=None,
                    excluded_reason=ExclusionReason.SCHEMA_FAILURE,
                )

    if usd is None and base is None:
        return OpenInterestObservation(
            venue_slug=observation.venue_slug,
            instrument=instrument,
            raw=raw,
            unit=unit,
            base_equivalent=None,
            usd_equivalent=None,
            excluded_reason=ExclusionReason.INVALID_QUOTE_CONVERSION,
        )

    return OpenInterestObservation(
        venue_slug=observation.venue_slug,
        instrument=instrument,
        raw=raw,
        unit=unit,
        base_equivalent=base,
        usd_equivalent=usd,
    )


def aggregate_open_interest(
    observations: Sequence[OpenInterestObservation],
) -> OpenInterestState:
    """Sum convertible open interest, segmented by instrument type."""
    usable = [o for o in observations if o.excluded_reason is None]
    excluded = [o for o in observations if o.excluded_reason is not None]
    if not usable:
        return OpenInterestState(
            venue_count=0,
            instrument_count=0,
            excluded_instrument_count=len(excluded),
        )

    def total(kind: InstrumentType | None) -> float | None:
        values = [
            o.usd_equivalent
            for o in usable
            if o.usd_equivalent is not None
            and (kind is None or o.instrument.instrument_type is kind)
        ]
        return math.fsum(values) if values else None

    base_values = [o.base_equivalent for o in usable if o.base_equivalent is not None]
    return OpenInterestState(
        total_usd=total(None),
        total_base=math.fsum(base_values) if base_values else None,
        perp_usd=total(InstrumentType.PERPETUAL),
        futures_usd=total(InstrumentType.FUTURE),
        option_usd=total(InstrumentType.OPTION),
        venue_count=len({o.venue_slug for o in usable}),
        instrument_count=len(usable),
        excluded_instrument_count=len(excluded),
    )


@dataclass(frozen=True, slots=True)
class MarkObservation:
    """One derivative's mark price, expressed in USD."""

    venue_slug: str
    instrument: Instrument
    mark_price_usd: float


def compute_basis(
    marks: Sequence[MarkObservation], *, reference_price: float | None
) -> BasisState:
    """Compute derivative premium over the reference price, in basis points.

    Perpetual and dated-futures basis are reported separately, because a perpetual's
    premium reflects funding pressure while a dated contract's reflects time to expiry.
    Combining them would produce a number that means neither.
    """
    if reference_price is None or reference_price <= 0 or not marks:
        return BasisState(price_source=PriceSource.MARK, venue_count=0)

    def basis_points(kind: InstrumentType) -> list[float]:
        return [
            value
            for observation in marks
            if observation.instrument.instrument_type is kind
            and (value := relative_bps(observation.mark_price_usd, reference_price)) is not None
        ]

    perp = basis_points(InstrumentType.PERPETUAL)
    futures = basis_points(InstrumentType.FUTURE)

    return BasisState(
        perp_basis_bps_median=median(perp),
        perp_basis_bps_min=min(perp) if perp else None,
        perp_basis_bps_max=max(perp) if perp else None,
        futures_basis_bps_median=median(futures),
        price_source=PriceSource.MARK,
        venue_count=len({o.venue_slug for o in marks}),
    )


def open_interest_to_volume(
    open_interest_usd: float | None, derivative_volume_usd: float | None
) -> float | None:
    """Return open interest divided by reported derivative volume."""
    return safe_divide(open_interest_usd, derivative_volume_usd)
