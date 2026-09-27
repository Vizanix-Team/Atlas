"""Quality, coverage and provenance models.

Quality is a first-class part of the schema, not a footnote. Atlas publishes
individual auditable dimensions rather than one opaque confidence score, because a
single number cannot tell a user whether a value is weak because a venue was
stale, because only two venues listed the asset, or because a conversion failed
(see ``docs/QUALITY.md``).
"""

from __future__ import annotations

from pydantic import Field, model_validator

from vizanix_atlas.models.base import AtlasModel
from vizanix_atlas.models.enums import (
    ConversionMethod,
    ExclusionReason,
    ObservationOrigin,
    PriceSource,
)


class QuoteConversion(AtlasModel):
    """How one quote currency was converted to USD.

    Attached to every USD-normalised figure. Atlas refuses to publish a
    USD-normalised value whose ``method`` is :attr:`ConversionMethod.UNAVAILABLE`.
    """

    from_asset_id: str
    to_asset_id: str = Field(default="asset:fiat:usd")
    rate: float | None = Field(default=None, gt=0, description="Units of USD per one unit of `from`.")
    method: ConversionMethod
    observed_at: int | None = Field(
        default=None, description="Epoch milliseconds of the observation the rate came from."
    )
    source_venue_slug: str | None = None
    path: tuple[str, ...] = Field(
        default=(),
        description=(
            "The conversion path taken, for example ('USDT', 'USDC', 'USD'). Length "
            "above two means the rate was derived, not directly observed."
        ),
    )
    deviation_from_parity_bps: float | None = Field(
        default=None,
        description=(
            "How far a stablecoin's observed rate sits from 1.0, in basis points. "
            "Published so that a depeg is visible instead of silently rescaling every metric."
        ),
    )

    @property
    def usable(self) -> bool:
        """Whether this conversion may be used to publish a USD figure."""
        return self.rate is not None and self.method is not ConversionMethod.UNAVAILABLE


class ProvenanceEntry(AtlasModel):
    """One venue observation's contribution to a derived value.

    This is the record that makes a reference price auditable end to end: which
    instrument, at what time, at what price, converted how, with what weight, and
    if excluded, why.
    """

    venue_slug: str
    instrument_id: str
    symbol_native: str
    observed_at: int | None
    age_seconds: float | None
    raw_price: float | None
    price_source: PriceSource | None
    normalised_price_usd: float | None
    conversion: QuoteConversion | None = None
    weight: float | None = Field(
        default=None, ge=0, description="Normalised weight after capping; null when excluded."
    )
    included: bool
    exclusion_reason: ExclusionReason | None = None

    @model_validator(mode="after")
    def _check_exclusion_consistency(self) -> ProvenanceEntry:
        """Require a reason for every exclusion and forbid one on an inclusion.

        Prevents the two failure modes that would make provenance untrustworthy: a
        silent drop, and an included observation carrying a stale reason.
        """
        if not self.included and self.exclusion_reason is None:
            raise ValueError("an excluded observation must record an exclusion_reason")
        if self.included and self.exclusion_reason is not None:
            raise ValueError("an included observation must not carry an exclusion_reason")
        return self


class Provenance(AtlasModel):
    """The full evidence set behind one derived value."""

    metric: str
    methodology_version: str
    entries: tuple[ProvenanceEntry, ...] = ()

    @property
    def included(self) -> tuple[ProvenanceEntry, ...]:
        """Entries that contributed to the value."""
        return tuple(e for e in self.entries if e.included)

    @property
    def excluded(self) -> tuple[ProvenanceEntry, ...]:
        """Entries that were considered and rejected."""
        return tuple(e for e in self.entries if not e.included)

    def exclusion_counts(self) -> dict[str, int]:
        """Return exclusion reasons and how often each occurred."""
        counts: dict[str, int] = {}
        for entry in self.excluded:
            if entry.exclusion_reason is not None:
                counts[entry.exclusion_reason.value] = counts.get(entry.exclusion_reason.value, 0) + 1
        return dict(sorted(counts.items()))


class Coverage(AtlasModel):
    """How much of the observable market a state row rests on.

    Each family of metrics has its own coverage, because a state may have seven
    price sources, five funding sources and three liquidity sources. One number
    would hide that.
    """

    price_venue_count: int = Field(default=0, ge=0)
    liquidity_venue_count: int = Field(default=0, ge=0)
    funding_venue_count: int = Field(default=0, ge=0)
    open_interest_venue_count: int = Field(default=0, ge=0)
    listing_venue_count: int = Field(
        default=0, ge=0, description="Venues whose catalogue lists this asset at all."
    )
    expected_venue_count: int = Field(
        default=0,
        ge=0,
        description="Venues that listed the asset and were expected to report this generation.",
    )
    coverage_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    partial_data: bool = False
    ambiguous_identity_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _derive_ratio(self) -> Coverage:
        """Fill in ``coverage_ratio`` from the counts when it was not supplied.

        Kept in the model so that the invariant ``0 <= ratio <= 1`` holds for every
        row regardless of which code path built it.
        """
        if self.coverage_ratio is None and self.expected_venue_count > 0:
            ratio = min(1.0, self.price_venue_count / self.expected_venue_count)
            object.__setattr__(self, "coverage_ratio", ratio)
        return self


class Freshness(AtlasModel):
    """Per-family observation ages.

    Published separately because a state's ticker data may be four minutes old
    while its order books are fifty. Reporting one timestamp for all of it would be
    a fabrication.
    """

    snapshot_effective_time: int
    freshest_observation_age_seconds: float | None = None
    oldest_observation_age_seconds: float | None = None
    ticker_age_seconds: float | None = None
    funding_age_seconds: float | None = None
    open_interest_age_seconds: float | None = None
    order_book_age_seconds: float | None = None

    @property
    def has_venue_timestamps(self) -> bool:
        """Whether any age could be computed from venue-supplied times."""
        return self.freshest_observation_age_seconds is not None


class DataQuality(AtlasModel):
    """The complete quality record attached to a state row."""

    coverage: Coverage
    freshness: Freshness
    generation_id: str
    schema_version: str
    methodology_version: str
    origin: ObservationOrigin = ObservationOrigin.SCHEDULED_SNAPSHOT
    excluded_observation_count: int = Field(default=0, ge=0)
    exclusion_reasons: dict[str, int] = Field(default_factory=dict)
    validation_warnings: tuple[str, ...] = ()


class WindowCoverage(AtlasModel):
    """How complete a rolling window's inputs are.

    A 30-day percentile computed from two days of data is not a 30-day percentile.
    Atlas publishes this so that a consumer can tell the difference, and returns
    null for the metric itself when the window is too sparse.
    """

    window: str
    observations_present: int = Field(ge=0)
    observations_expected: int = Field(ge=0)
    observations_required: int = Field(ge=0)

    @property
    def ratio(self) -> float | None:
        """Present over expected, or ``None`` when nothing was expected."""
        if self.observations_expected == 0:
            return None
        return min(1.0, self.observations_present / self.observations_expected)

    @property
    def sufficient(self) -> bool:
        """Whether the window has enough observations for its metric to be published."""
        return self.observations_present >= self.observations_required


class QualityEvent(AtlasModel):
    """A recorded data-quality incident.

    Written to the ``quality_events`` table so that quarantined observations remain
    inspectable after a generation is published.
    """

    generation_id: str
    venue_slug: str
    instrument_id: str | None = None
    asset_id: str | None = None
    event_type: str
    severity: str = Field(description="info, warning or error.")
    detail: str
    observed_at: int
