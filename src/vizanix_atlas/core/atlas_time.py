"""Time handling.

Atlas keeps several distinct notions of "when" apart, because collapsing them
destroys the ability to reason about freshness (see ``docs/DATA_MODEL.md``):

``exchange_event_time``
    When the venue says the market event happened.
``exchange_server_time``
    When the venue says it generated the response.
``collector_receive_time``
    When Atlas received the response. Always available; the other two are not.
``snapshot_effective_time``
    The scheduled observation slot a generation is attributed to.

All internal timestamps are timezone-aware UTC. Storage uses integer
milliseconds since the Unix epoch; JSON uses ISO-8601 with an explicit ``Z``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

MS_PER_SECOND = 1_000

# Bounds used to reject nonsensical venue timestamps. A venue occasionally emits
# 0, a seconds value where milliseconds are documented, or a far-future value.
# Coercing those silently would corrupt freshness, so they are rejected instead.
_MIN_PLAUSIBLE_MS = 1_262_304_000_000  # 2010-01-01T00:00:00Z
_MAX_CLOCK_SKEW = timedelta(hours=26)

# Contract expiry and listing dates are legitimately far from now: an option may
# expire next December and a listing date may be years in the past. They need a wide
# window, which is why they do not share the observation-timestamp check. Using the
# observation window for an expiry silently discards every long-dated contract.
_MAX_CONTRACT_HORIZON = timedelta(days=365 * 10)


def utc_now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_epoch_ms(moment: datetime) -> int:
    """Convert a datetime to integer milliseconds since the Unix epoch.

    Raises:
        ValueError: If ``moment`` is naive. Atlas never guesses a timezone.
    """
    if moment.tzinfo is None:
        raise ValueError("refusing to convert a naive datetime; attach a timezone")
    return int(moment.astimezone(UTC).timestamp() * MS_PER_SECOND)


def from_epoch_ms(epoch_ms: int | float) -> datetime:
    """Convert milliseconds since the Unix epoch to a UTC datetime."""
    return datetime.fromtimestamp(epoch_ms / MS_PER_SECOND, tz=UTC)


def to_iso(moment: datetime) -> str:
    """Render a datetime as ISO-8601 UTC with a ``Z`` suffix and second precision."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC datetime.

    Accepts a trailing ``Z``, which several venues emit and which
    :meth:`datetime.fromisoformat` did not accept before Python 3.11.
    """
    normalised = text.strip()
    if normalised.endswith(("Z", "z")):
        normalised = f"{normalised[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalised)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def plausible_epoch_ms(value: int | float | None, *, reference: datetime | None = None) -> int | None:
    """Return ``value`` if it is a believable epoch-millisecond timestamp, else ``None``.

    Returning ``None`` rather than substituting the current time is deliberate: a
    venue that stops sending timestamps must show up as missing freshness data,
    not as perfectly fresh data.

    Args:
        value: Candidate timestamp in milliseconds.
        reference: Instant to compare against; defaults to now.
    """
    if value is None:
        return None
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return None
    if as_int < _MIN_PLAUSIBLE_MS:
        return None
    ceiling = to_epoch_ms((reference or utc_now()) + _MAX_CLOCK_SKEW)
    if as_int > ceiling:
        return None
    return as_int


def plausible_contract_time_ms(
    value: int | float | None, *, reference: datetime | None = None
) -> int | None:
    """Return ``value`` if it is a believable contract date, else ``None``.

    Separate from :func:`plausible_epoch_ms` because the two answer different
    questions. An *observation* timestamp more than a day in the future is a venue
    clock problem; a *contract* date ten months in the future is an ordinary December
    option. Validating an expiry against the observation window silently drops every
    long-dated contract, so expiry, listing and delisting times use this instead.

    Args:
        value: Candidate timestamp in milliseconds.
        reference: Instant to compare against; defaults to now.
    """
    if value is None:
        return None
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return None
    if as_int < _MIN_PLAUSIBLE_MS:
        # Also rejects the 0 several venues use to mean "no expiry".
        return None
    ceiling = to_epoch_ms((reference or utc_now()) + _MAX_CONTRACT_HORIZON)
    if as_int > ceiling:
        return None
    return as_int


def age_seconds(moment: datetime, *, reference: datetime | None = None) -> float:
    """Return the age of ``moment`` in seconds relative to ``reference`` (default now).

    A negative result is possible when a venue's clock runs ahead of the
    collector's and is preserved rather than clamped, so that clock skew stays
    visible.
    """
    return ((reference or utc_now()) - moment).total_seconds()


def floor_to_slot(moment: datetime, *, slot_minutes: int) -> datetime:
    """Round ``moment`` down to the start of its slot of ``slot_minutes``.

    Used to attribute a run to a scheduled observation window. GitHub Actions
    starts scheduled workflows late often enough that the wall-clock start time
    cannot be used as the slot identifier directly.
    """
    if slot_minutes <= 0:
        raise ValueError("slot_minutes must be positive")
    aligned = moment.astimezone(UTC).replace(second=0, microsecond=0)
    return aligned - timedelta(minutes=aligned.minute % slot_minutes)


def slot_label(moment: datetime) -> str:
    """Render a compact slot identifier such as ``20260927T184500Z``."""
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def day_label(moment: datetime) -> str:
    """Render the UTC calendar day as ``YYYY-MM-DD``."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d")
