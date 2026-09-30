"""Turn published generations into housekeeping deletion candidates.

A file becomes a candidate only when its generation is outside the recent-generations
window, older than ``raw_snapshot_days``, and its UTC day has a daily release whose
report says ``compacted`` (which :func:`~vizanix_atlas.publishing.daily.compact_day`
only sets after re-downloading and re-hashing every compacted file). The guard rails in
:func:`~vizanix_atlas.storage.housekeeping.plan_cleanup` then apply on top.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from vizanix_atlas.core.config import RetentionConfig
from vizanix_atlas.core.errors import AtlasError
from vizanix_atlas.models.manifest import DailyCompactionReport
from vizanix_atlas.publishing.daily import REPORT_FILENAME
from vizanix_atlas.publishing.publisher import Publisher
from vizanix_atlas.sdk.remote import GenerationFileSource
from vizanix_atlas.storage.housekeeping import DeletionCandidate, select_generations_to_retain

_DAY_ID = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _age_hours(published_at: str | None, now: datetime) -> float:
    """Return hours since ``published_at``; unknown age counts as brand new (never deleted)."""
    if published_at is None:
        return 0.0
    moment = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    return max((now - moment).total_seconds() / 3600.0, 0.0)


def _day_of(slot_label: str) -> str:
    return f"{slot_label[0:4]}-{slot_label[4:6]}-{slot_label[6:8]}"


async def find_candidates(
    publisher: Publisher,
    source: GenerationFileSource,
    retention: RetentionConfig,
    *,
    now: datetime | None = None,
) -> list[DeletionCandidate]:
    """List every intra-day generation file retention policy might remove."""
    now = now or datetime.now(UTC)
    ids = await publisher.list_generation_ids()
    day_ids = {i for i in ids if _DAY_ID.match(i)}
    snapshot_ids = sorted((i for i in ids if i not in day_ids), reverse=True)
    keep = select_generations_to_retain(
        snapshot_ids, keep_recent_generations=retention.generations.keep_recent_generations
    )

    compacted_days: set[str] = set()
    for day in sorted(day_ids):
        try:
            report = DailyCompactionReport.model_validate_json(
                source.fetch_generation_file(day, REPORT_FILENAME)
            )
        except (AtlasError, ValueError):  # an unreadable report simply means "not compacted"
            continue
        if report.compacted:
            compacted_days.add(day)

    minimum_age = retention.retention.raw_snapshot_days * 24.0
    candidates: list[DeletionCandidate] = []
    for generation_id in snapshot_ids:
        if generation_id in keep:
            continue
        manifest = await publisher.read_generation_manifest(generation_id)
        if manifest is None:
            continue
        age = _age_hours(manifest.published_at, now)
        if age < minimum_age:
            continue
        replaced = _day_of(manifest.slot_label) in compacted_days
        candidates.extend(
            DeletionCandidate(
                generation_id=generation_id,
                tag=f"atlas-data-{generation_id}",
                filename=entry.filename,
                age_hours=age,
                has_compacted_replacement=replaced,
                checksum_verified=replaced,
            )
            for entry in manifest.files
        )
    return candidates
