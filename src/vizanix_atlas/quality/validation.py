"""Observation validation and quarantine.

Every observation passes through here before analytics sees it. An invalid observation
is quarantined with a reason, never coerced into something usable: a price of zero is
not a cheap market, and a crossed book is not a tight spread.

The contract this module upholds, asserted by tests, is that
``included + excluded == considered``. An observation is never silently dropped.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from vizanix_atlas.core.atlas_time import from_epoch_ms, utc_now
from vizanix_atlas.core.config import QualityConfig
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.models.enums import ExclusionReason
from vizanix_atlas.models.instrument import Instrument
from vizanix_atlas.models.observations import RawOrderBook, RawTicker
from vizanix_atlas.models.quality import QualityEvent

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """Whether one observation may be used, and why not when it may not."""

    valid: bool
    reason: ExclusionReason | None = None
    detail: str | None = None

    @staticmethod
    def ok() -> ValidationOutcome:
        """An observation that passed every check."""
        return ValidationOutcome(valid=True)

    @staticmethod
    def rejected(reason: ExclusionReason, detail: str | None = None) -> ValidationOutcome:
        """An observation that must be quarantined."""
        return ValidationOutcome(valid=False, reason=reason, detail=detail)


@dataclass(slots=True)
class QuarantineLog:
    """Accumulates quality events for the published ``quality_events`` table."""

    generation_id: str
    events: list[QualityEvent] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    #: Events are recorded in full up to this many per reason. Beyond it only the count
    #: rises, because a venue-wide schema break would otherwise produce a table larger
    #: than the dataset it describes.
    max_events_per_reason: int = 200

    def record(
        self,
        outcome: ValidationOutcome,
        *,
        venue_slug: str,
        instrument_id: str | None,
        asset_id: str | None,
        observed_at: int,
    ) -> None:
        """Record one rejection."""
        if outcome.valid or outcome.reason is None:
            return
        key = outcome.reason.value
        seen = self.counts.get(key, 0)
        self.counts[key] = seen + 1
        if seen < self.max_events_per_reason:
            self.events.append(
                QualityEvent(
                    generation_id=self.generation_id,
                    venue_slug=venue_slug,
                    instrument_id=instrument_id,
                    asset_id=asset_id,
                    event_type=key,
                    severity="warning",
                    detail=outcome.detail or key,
                    observed_at=observed_at,
                )
            )

    @property
    def total(self) -> int:
        """Total rejections, including those not recorded individually."""
        return sum(self.counts.values())

    def summary(self) -> dict[str, int]:
        """Rejection counts by reason, sorted for stable output."""
        return dict(sorted(self.counts.items()))


class ObservationValidator:
    """Applies Atlas's validity rules to raw observations."""

    def __init__(self, quality: QualityConfig) -> None:
        self.quality = quality

    def validate_ticker(
        self,
        ticker: RawTicker,
        instrument: Instrument | None,
        *,
        reference_ms: int | None = None,
    ) -> ValidationOutcome:
        """Validate one ticker against the instrument it claims to describe.

        Order matters: structural problems are reported before staleness, so that a
        broken record is described by what is wrong with it rather than by its age.
        """
        if instrument is None:
            # A quote for something discovery never saw. Recorded, but it cannot be
            # attributed to an asset, so it cannot contribute to a derived value.
            return ValidationOutcome.rejected(
                ExclusionReason.UNRESOLVED_INSTRUMENT,
                f"no discovered instrument for {ticker.venue_slug}:{ticker.symbol_native}",
            )
        if not instrument.active:
            return ValidationOutcome.rejected(
                ExclusionReason.INACTIVE_INSTRUMENT,
                f"{instrument.instrument_id} is not active",
            )

        candidate = ticker.reference_candidate
        if candidate is None:
            # Either no price at all, or a crossed book that made the mid meaningless.
            if ticker.bid_price is not None and ticker.ask_price is not None:
                return ValidationOutcome.rejected(
                    ExclusionReason.CROSSED_BOOK,
                    f"bid {ticker.bid_price} is above ask {ticker.ask_price}",
                )
            return ValidationOutcome.rejected(
                ExclusionReason.MISSING_PRICE, "no mid or last price was published"
            )

        price, _ = candidate
        if price <= 0:
            return ValidationOutcome.rejected(
                ExclusionReason.NON_POSITIVE_PRICE, f"price was {price}"
            )

        # A high below a low describes no market that ever existed.
        if (
            ticker.high_24h is not None
            and ticker.low_24h is not None
            and ticker.high_24h < ticker.low_24h
        ):
            return ValidationOutcome.rejected(
                ExclusionReason.SCHEMA_FAILURE,
                f"24h high {ticker.high_24h} is below 24h low {ticker.low_24h}",
            )

        return self._check_freshness(ticker, reference_ms=reference_ms)

    def _check_freshness(self, ticker: RawTicker, *, reference_ms: int | None) -> ValidationOutcome:
        """Reject an observation too old to describe the current snapshot.

        Only venue-supplied times are judged. A venue that publishes no timestamp cannot
        be found stale, which is a real limitation and is why
        ``exchange_event_time`` is published separately rather than being filled in from
        the collector's clock.
        """
        venue_time = ticker.timing.exchange_event_time or ticker.timing.exchange_server_time
        if venue_time is None:
            return ValidationOutcome.ok()

        reference = from_epoch_ms(reference_ms) if reference_ms is not None else utc_now()
        age = (reference - from_epoch_ms(venue_time)).total_seconds()
        if age > self.quality.max_observation_age_seconds:
            return ValidationOutcome.rejected(
                ExclusionReason.STALE,
                f"observation is {age:.0f}s old; the limit is "
                f"{self.quality.max_observation_age_seconds:.0f}s",
            )
        # A venue clock far ahead of the collector's means the timestamp cannot be
        # trusted for freshness, which is a schema problem rather than staleness.
        if age < -self.quality.max_observation_age_seconds:
            return ValidationOutcome.rejected(
                ExclusionReason.MISSING_TIMESTAMP,
                f"venue timestamp is {-age:.0f}s in the future",
            )
        return ValidationOutcome.ok()

    def validate_order_book(
        self, book: RawOrderBook, instrument: Instrument | None
    ) -> ValidationOutcome:
        """Validate one order-book snapshot."""
        if instrument is None:
            return ValidationOutcome.rejected(
                ExclusionReason.UNRESOLVED_INSTRUMENT,
                f"no discovered instrument for {book.venue_slug}:{book.symbol_native}",
            )
        if not book.bids or not book.asks:
            return ValidationOutcome.rejected(
                ExclusionReason.SCHEMA_FAILURE, "order book has an empty side"
            )
        if book.is_crossed:
            return ValidationOutcome.rejected(
                ExclusionReason.CROSSED_BOOK,
                f"best bid {book.best_bid} is at or above best ask {book.best_ask}",
            )
        return ValidationOutcome.ok()

    def check_deviation(self, price: float, centre: float) -> ValidationOutcome:
        """Reject a price too far from the provisional cross-venue centre.

        Runs after a provisional robust centre exists, so the comparison is against the
        market rather than against one venue. The threshold is wide enough to keep a
        genuinely fragmented microcap and narrow enough to drop a venue quoting a
        different asset under the same ticker.
        """
        if centre <= 0:
            return ValidationOutcome.ok()
        deviation_bps = abs(price - centre) / centre * 10_000.0
        if deviation_bps > self.quality.max_deviation_bps:
            return ValidationOutcome.rejected(
                ExclusionReason.EXTREME_DEVIATION,
                f"price deviates {deviation_bps:.0f} bps from the cross-venue centre; "
                f"the limit is {self.quality.max_deviation_bps:.0f} bps",
            )
        return ValidationOutcome.ok()


def deduplicate_instruments(
    instruments: Sequence[Instrument],
) -> tuple[tuple[Instrument, ...], tuple[Instrument, ...]]:
    """Split instruments into those to count and those that would double-count.

    A venue that lists the same asset pair as both a spot market and a margin market, or
    under two symbols, would otherwise have its volume counted twice. Atlas keeps one
    instrument per (venue, type, base, quote, expiry, strike) and reports the rest as
    duplicates.

    The kept instrument is chosen deterministically by identifier, so the same inputs
    always produce the same choice.

    Returns:
        The instruments to use, and the duplicates that were set aside.

    """
    seen: dict[tuple[str, str, str, str, int | None, float | None], Instrument] = {}
    duplicates: list[Instrument] = []

    for instrument in sorted(instruments, key=lambda i: i.instrument_id):
        key = (
            instrument.venue_slug,
            instrument.instrument_type.value,
            instrument.base_asset_id,
            instrument.quote_asset_id,
            instrument.expiry,
            instrument.strike,
        )
        if key in seen:
            duplicates.append(instrument)
        else:
            seen[key] = instrument

    if duplicates:
        _log.info(
            "set aside duplicate instruments to avoid double-counting volume",
            extra={"duplicates": len(duplicates), "kept": len(seen)},
        )
    return tuple(seen.values()), tuple(duplicates)


def validated_tickers(
    tickers: Iterable[RawTicker],
    lookup: object,
    validator: ObservationValidator,
    quarantine: QuarantineLog,
    *,
    reference_ms: int | None = None,
) -> tuple[list[tuple[RawTicker, Instrument]], int]:
    """Validate a batch of tickers, quarantining the failures.

    Args:
        tickers: The observations to validate.
        lookup: An object with
            ``by_venue_symbol(venue_slug, symbol_native, instrument_class=...)``.
        validator: The rule set to apply.
        quarantine: Where rejections are recorded.
        reference_ms: The instant to measure age against; defaults to now.

    Returns:
        The accepted ``(ticker, instrument)`` pairs, and the number considered. The
        difference between the two is exactly the number quarantined.

    """
    accepted: list[tuple[RawTicker, Instrument]] = []
    considered = 0

    for ticker in tickers:
        considered += 1
        instrument = lookup.by_venue_symbol(  # type: ignore[attr-defined]
            ticker.venue_slug, ticker.symbol_native, instrument_class=ticker.instrument_class
        )
        outcome = validator.validate_ticker(ticker, instrument, reference_ms=reference_ms)
        if outcome.valid and instrument is not None:
            accepted.append((ticker, instrument))
        else:
            quarantine.record(
                outcome,
                venue_slug=ticker.venue_slug,
                instrument_id=instrument.instrument_id if instrument else None,
                asset_id=instrument.base_asset_id if instrument else None,
                observed_at=ticker.timing.best_effective_time,
            )
    return accepted, considered
