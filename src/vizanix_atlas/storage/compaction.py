"""Daily compaction.

Every successful scheduled snapshot is its own small generation. At the end of each
UTC day, compaction gathers that day's generations into a partitioned daily dataset
and, just as importantly, records **which scheduled windows never produced a
snapshot at all** (see ``docs/OPERATIONS.md`` sections 10 and 74-75).

Atlas never fabricates a missing window. A gap in the schedule is published as a gap,
not interpolated, not backfilled silently, and not hidden by re-labelling the
generation before or after it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import polars as pl

from vizanix_atlas.core.atlas_time import floor_to_slot, slot_label
from vizanix_atlas.core.config import ScheduleConfig
from vizanix_atlas.models.manifest import DailyCompactionReport, GenerationManifest


def expected_slots_for_day(day: str, schedule: ScheduleConfig) -> tuple[str, ...]:
    """Return every scheduled slot label expected on a UTC calendar day, in order.

    Derived from the schedule's own cron minutes rather than a hardcoded cadence, so a
    schedule change in ``config/collection.yaml`` is reflected here automatically
    rather than needing a matching change to this function. Each cron time is floored
    to its slot exactly as :func:`~vizanix_atlas.discovery.pipeline.build_generation`
    floors a run's start, so a run at 00:07 is expected under the label ``000000Z``.

    Args:
        day: A UTC calendar day as ``YYYY-MM-DD``.
        schedule: The collection schedule whose cron minutes define the slots.

    """
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    return tuple(
        slot_label(
            floor_to_slot(
                base + timedelta(hours=hour, minutes=minute), slot_minutes=schedule.slot_minutes
            )
        )
        for hour in range(24)
        for minute in schedule.cron_minutes
    )


@dataclass(frozen=True, slots=True)
class DiscoveredGeneration:
    """One generation found to belong to a compaction day."""

    generation_id: str
    slot_label: str
    manifest: GenerationManifest


def discover_day_generations(
    manifests: Sequence[GenerationManifest], day: str
) -> tuple[DiscoveredGeneration, ...]:
    """Select the manifests whose slot falls on ``day``.

    A generation's day is read from its own ``slot_label`` (the ``YYYYMMDD`` prefix),
    never from when compaction happens to run, so a compaction job that starts late
    still attributes each generation to the day it was actually scheduled for.
    """
    prefix = day.replace("-", "")
    return tuple(
        sorted(
            (
                DiscoveredGeneration(
                    generation_id=m.generation_id, slot_label=m.slot_label, manifest=m
                )
                for m in manifests
                if m.slot_label.startswith(prefix)
            ),
            key=lambda g: (g.slot_label, g.generation_id),
        )
    )


def build_daily_report(
    day: str,
    generations: Sequence[DiscoveredGeneration],
    schedule: ScheduleConfig,
    *,
    invalid_generation_ids: frozenset[str] = frozenset(),
) -> DailyCompactionReport:
    """Build the gap-aware compaction report for one day.

    A slot's categories are independent facts rather than one mutually-exclusive
    bucket, because they can co-occur: a workflow retry can leave two generations
    claiming the same slot (``duplicate``), and both attempts can still have failed
    validation (``invalid``) at once.

    Args:
        day: The UTC calendar day this report describes, as ``YYYY-MM-DD``.
        generations: Every generation discovered for this day (see
            :func:`discover_day_generations`).
        schedule: The collection schedule, for the expected-window calculation.
        invalid_generation_ids: Generations that exist but failed validation (did not
            pass the publish validity gate, or failed a checksum re-check).

    """
    expected = expected_slots_for_day(day, schedule)
    by_slot: dict[str, list[DiscoveredGeneration]] = defaultdict(list)
    for generation in generations:
        by_slot[generation.slot_label].append(generation)

    successful: list[str] = []
    missing: list[str] = []
    duplicate: list[str] = []
    invalid: list[str] = []

    for slot in expected:
        occupants = by_slot.get(slot, [])
        if not occupants:
            missing.append(slot)
            continue
        if len(occupants) > 1:
            duplicate.append(slot)
        valid = [g for g in occupants if g.generation_id not in invalid_generation_ids]
        if valid:
            successful.append(slot)
        else:
            invalid.append(slot)

    # A generation whose slot was not even an expected one (a manual or out-of-band
    # run) is neither missing nor successful in the schedule's own terms, but its
    # existence is still worth recording rather than silently ignoring.
    unexpected_slots = sorted(set(by_slot) - set(expected))
    unexpected_notes = tuple(
        f"generation {g.generation_id} at slot {slot} falls outside the scheduled windows"
        for slot in unexpected_slots
        for g in by_slot[slot]
    )

    return DailyCompactionReport(
        day=day,
        expected_windows=expected,
        successful_windows=tuple(successful),
        missing_windows=tuple(missing),
        duplicate_windows=tuple(duplicate),
        invalid_windows=tuple(invalid),
        source_snapshot_count=len(generations),
        notes=unexpected_notes,
    )


def generations_to_compact(
    generations: Sequence[DiscoveredGeneration],
    report: DailyCompactionReport,
) -> tuple[DiscoveredGeneration, ...]:
    """Return the generations that should feed the compacted daily output.

    Only successful, non-duplicate slots are compacted from. A duplicate slot needs a
    human or a documented tie-break rule to choose between its occupants before it can
    safely enter a daily aggregate; compacting both would double-count that window's
    observations, and compacting an arbitrary one would silently discard the other
    without that being visible anywhere.
    """
    duplicate_slots = set(report.duplicate_windows)
    successful_slots = set(report.successful_windows) - duplicate_slots
    return tuple(g for g in generations if g.slot_label in successful_slots)


def compact_tables(per_generation_tables: dict[str, list[pl.DataFrame]]) -> dict[str, pl.DataFrame]:
    """Concatenate each table across a day's compactable generations.

    Args:
        per_generation_tables: Table name to the list of that table's frame from each
            generation being compacted, already loaded from wherever they were stored.

    Returns:
        One concatenated frame per table name. A table entirely absent from every
        generation (an empty input list) is omitted rather than published as an empty
        file with no rows to justify its existence.

    """
    return {
        name: pl.concat(frames, how="vertical_relaxed")
        for name, frames in per_generation_tables.items()
        if frames
    }
