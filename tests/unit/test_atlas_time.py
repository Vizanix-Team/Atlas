"""Time handling tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vizanix_atlas.core.atlas_time import (
    age_seconds,
    day_label,
    floor_to_slot,
    from_epoch_ms,
    parse_iso,
    plausible_contract_time_ms,
    plausible_epoch_ms,
    slot_label,
    to_epoch_ms,
    to_iso,
)

REFERENCE = datetime(2026, 9, 27, 17, 40, 0, tzinfo=UTC)
REFERENCE_MS = to_epoch_ms(REFERENCE)


def test_naive_datetimes_are_refused() -> None:
    """Atlas never guesses a timezone."""
    with pytest.raises(ValueError, match="naive"):
        to_epoch_ms(datetime(2026, 9, 27, 17, 40))


def test_iso_round_trip_preserves_the_instant() -> None:
    assert parse_iso(to_iso(REFERENCE)) == REFERENCE
    # Both the Z and the offset form must parse.
    assert parse_iso("2026-09-27T17:40:00Z") == REFERENCE
    assert parse_iso("2026-09-27T17:40:00+00:00") == REFERENCE
    # A naive ISO string is read as UTC rather than as local time.
    assert parse_iso("2026-09-27T17:40:00") == REFERENCE


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (REFERENCE_MS, REFERENCE_MS),
        (None, None),
        (0, None),
        (-1, None),
        # A seconds value in a milliseconds field is below the floor, so it is
        # rejected rather than silently read as 1970.
        (REFERENCE_MS // 1000, None),
        ("not a number", None),
        # Two days in the future is a venue clock problem, not an observation.
        (REFERENCE_MS + 48 * 3600 * 1000, None),
        # Ten minutes of skew is tolerated.
        (REFERENCE_MS + 600 * 1000, REFERENCE_MS + 600 * 1000),
    ],
)
def test_observation_timestamps_are_bounded(value: object, expected: int | None) -> None:
    assert plausible_epoch_ms(value, reference=REFERENCE) == expected  # type: ignore[arg-type]


def test_contract_dates_allow_a_far_horizon() -> None:
    """A December option expiry must not be discarded as an implausible timestamp.

    Regression test: contract expiry was originally validated with the observation
    window, which rejects anything more than a day ahead. That silently dropped every
    long-dated option and future, reducing Deribit's catalogue from 5,654 instruments
    to 705 and OKX's option coverage from 1,390 to 120.
    """
    three_months = REFERENCE_MS + 90 * 24 * 3600 * 1000
    one_year = REFERENCE_MS + 365 * 24 * 3600 * 1000

    # The observation check rejects both, which is correct for an observation.
    assert plausible_epoch_ms(three_months, reference=REFERENCE) is None
    assert plausible_epoch_ms(one_year, reference=REFERENCE) is None

    # The contract check accepts both, which is correct for an expiry.
    assert plausible_contract_time_ms(three_months, reference=REFERENCE) == three_months
    assert plausible_contract_time_ms(one_year, reference=REFERENCE) == one_year


def test_contract_dates_still_reject_nonsense() -> None:
    # 0 is what several venues use to mean "no expiry"; it must not become 1970.
    assert plausible_contract_time_ms(0, reference=REFERENCE) is None
    assert plausible_contract_time_ms(None, reference=REFERENCE) is None
    # A listing date far in the past is legitimate; the year 2200 is not.
    assert (
        plausible_contract_time_ms(
            to_epoch_ms(datetime(2014, 11, 21, tzinfo=UTC)), reference=REFERENCE
        )
        is not None
    )
    assert (
        plausible_contract_time_ms(
            to_epoch_ms(datetime(2200, 1, 1, tzinfo=UTC)), reference=REFERENCE
        )
        is None
    )


def test_slot_flooring_attributes_a_late_run_to_its_window() -> None:
    """GitHub Actions starts scheduled runs late, so the slot is derived, not assumed."""
    # A run scheduled for :37 that actually started at :41 belongs to the :30 slot.
    started = datetime(2026, 9, 27, 17, 41, 23, tzinfo=UTC)
    assert floor_to_slot(started, slot_minutes=15) == datetime(2026, 9, 27, 17, 30, tzinfo=UTC)
    assert slot_label(floor_to_slot(started, slot_minutes=15)) == "20260927T173000Z"
    assert day_label(started) == "2026-09-27"

    with pytest.raises(ValueError, match="positive"):
        floor_to_slot(started, slot_minutes=0)


def test_negative_ages_are_preserved_so_clock_skew_stays_visible() -> None:
    ahead = REFERENCE + timedelta(seconds=30)
    assert age_seconds(ahead, reference=REFERENCE) == pytest.approx(-30.0)
    assert age_seconds(REFERENCE - timedelta(seconds=30), reference=REFERENCE) == pytest.approx(
        30.0
    )


def test_epoch_round_trip() -> None:
    assert from_epoch_ms(REFERENCE_MS) == REFERENCE
