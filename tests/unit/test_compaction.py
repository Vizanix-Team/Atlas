"""Daily compaction tests.

The property under test throughout: a scheduled window that never produced a snapshot
is reported as missing, never fabricated, interpolated, or silently absorbed into a
neighbouring window.
"""

from __future__ import annotations

import pytest

from vizanix_atlas.core.config import load_collection_config
from vizanix_atlas.models.manifest import BuildProvenance, GenerationManifest, VenueOutcome
from vizanix_atlas.storage.compaction import (
    DiscoveredGeneration,
    build_daily_report,
    compact_tables,
    discover_day_generations,
    expected_slots_for_day,
    generations_to_compact,
)

DAY = "2026-09-27"


@pytest.fixture
def schedule():
    return load_collection_config().schedule


def _manifest(generation_id: str, slot: str) -> GenerationManifest:
    return GenerationManifest(
        dataset_format_version="1.0.0",
        schema_version="1.0.0",
        methodology_version="1.0.0",
        software_version="0.1.0",
        generation_id=generation_id,
        slot_label=slot,
        collection_started_at="2026-09-27T00:07:00Z",
        collection_finished_at="2026-09-27T00:08:00Z",
        snapshot_effective_time="2026-09-27T00:07:00Z",
        venues=VenueOutcome(attempted=5, successful=5),
        build=BuildProvenance(software_version="0.1.0"),
    )


def test_expected_slots_follow_the_configured_cron_minutes(schedule) -> None:
    slots = expected_slots_for_day(DAY, schedule)
    assert len(slots) == 24 * len(schedule.cron_minutes)
    assert slots == tuple(sorted(slots)), "slots must already be in chronological order"
    assert slots[0] == "20260927T000000Z"
    assert slots[-1] == "20260927T234500Z"


def test_discovery_filters_by_the_generations_own_slot_day() -> None:
    manifests = [
        _manifest("g1", "20260927T000000Z"),
        _manifest("g2", "20260928T000000Z"),  # the next day; must be excluded
        _manifest("g3", "20260927T003000Z"),
    ]
    found = discover_day_generations(manifests, DAY)
    assert {g.generation_id for g in found} == {"g1", "g3"}
    # Sorted by slot for deterministic downstream processing.
    assert [g.generation_id for g in found] == ["g1", "g3"]


def test_a_missing_window_is_reported_not_fabricated(schedule) -> None:
    """The central behaviour: a scheduled slot with zero generations is `missing`,
    and never appears in `successful`.
    """
    slots = expected_slots_for_day(DAY, schedule)
    # Every slot has a generation except the third one.
    present = [s for i, s in enumerate(slots) if i != 2]
    generations = tuple(
        DiscoveredGeneration(generation_id=f"g{i}", slot_label=s, manifest=_manifest(f"g{i}", s))
        for i, s in enumerate(present)
    )
    report = build_daily_report(DAY, generations, schedule)

    assert report.missing_windows == (slots[2],)
    assert slots[2] not in report.successful_windows
    assert len(report.successful_windows) == len(present)
    assert report.completeness == pytest.approx(len(present) / len(slots))


def test_a_duplicate_window_is_flagged_and_still_counted_successful_if_one_is_valid(
    schedule,
) -> None:
    """Two generations claiming the same slot (a retried workflow run) are flagged as
    a duplicate; if at least one of them is valid, the slot is still successful.
    """
    slot = expected_slots_for_day(DAY, schedule)[0]
    generations = (
        DiscoveredGeneration(generation_id="a", slot_label=slot, manifest=_manifest("a", slot)),
        DiscoveredGeneration(generation_id="b", slot_label=slot, manifest=_manifest("b", slot)),
    )
    report = build_daily_report(DAY, generations, schedule)

    assert slot in report.duplicate_windows
    assert slot in report.successful_windows
    assert slot not in report.missing_windows


def test_a_slot_with_only_invalid_generations_is_invalid_not_successful(schedule) -> None:
    slot = expected_slots_for_day(DAY, schedule)[0]
    generations = (
        DiscoveredGeneration(generation_id="a", slot_label=slot, manifest=_manifest("a", slot)),
    )
    report = build_daily_report(DAY, generations, schedule, invalid_generation_ids=frozenset({"a"}))

    assert slot in report.invalid_windows
    assert slot not in report.successful_windows
    assert slot not in report.missing_windows


def test_an_out_of_schedule_generation_is_noted_but_not_counted_as_a_scheduled_window(
    schedule,
) -> None:
    """A manually triggered run at an odd minute exists, but is not one of the
    scheduled windows, so it must not inflate the expected/successful accounting.
    """
    odd_slot = "20260927T001234Z"
    generations = (
        DiscoveredGeneration(
            generation_id="manual", slot_label=odd_slot, manifest=_manifest("manual", odd_slot)
        ),
    )
    report = build_daily_report(DAY, generations, schedule)

    assert odd_slot not in report.expected_windows
    assert odd_slot not in report.successful_windows
    assert report.notes and "manual" in report.notes[0] and odd_slot in report.notes[0]
    # Every scheduled window is still missing; the manual run does not fill any of them.
    assert len(report.missing_windows) == len(report.expected_windows)


def test_generations_to_compact_excludes_duplicates_and_invalid_slots(schedule) -> None:
    slots = expected_slots_for_day(DAY, schedule)[:3]
    clean = DiscoveredGeneration(
        generation_id="clean", slot_label=slots[0], manifest=_manifest("clean", slots[0])
    )
    dup_a = DiscoveredGeneration(
        generation_id="dup_a", slot_label=slots[1], manifest=_manifest("dup_a", slots[1])
    )
    dup_b = DiscoveredGeneration(
        generation_id="dup_b", slot_label=slots[1], manifest=_manifest("dup_b", slots[1])
    )
    invalid = DiscoveredGeneration(
        generation_id="bad", slot_label=slots[2], manifest=_manifest("bad", slots[2])
    )
    generations = (clean, dup_a, dup_b, invalid)

    report = build_daily_report(
        DAY, generations, schedule, invalid_generation_ids=frozenset({"bad"})
    )
    to_compact = generations_to_compact(generations, report)

    assert {g.generation_id for g in to_compact} == {"clean"}


def test_completeness_is_none_with_no_expected_windows() -> None:
    from vizanix_atlas.models.manifest import DailyCompactionReport

    report = DailyCompactionReport(day=DAY)
    assert report.completeness is None


def test_compact_tables_concatenates_and_omits_absent_tables() -> None:
    import polars as pl

    per_generation = {
        "assets": [
            pl.DataFrame({"asset_id": ["a"], "symbol": ["A"]}),
            pl.DataFrame({"asset_id": ["b"], "symbol": ["B"]}),
        ],
        "quality_events": [],  # no generation this day produced any quality events
    }
    compacted = compact_tables(per_generation)

    assert set(compacted) == {"assets"}
    assert compacted["assets"].height == 2
    assert sorted(compacted["assets"]["asset_id"]) == ["a", "b"]
