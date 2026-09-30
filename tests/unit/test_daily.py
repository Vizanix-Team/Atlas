"""End-to-end daily compaction against a filesystem publisher (no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from tests.unit.test_publishing import _permissive_config, build_sample_generation
from vizanix_atlas.core.atlas_time import to_epoch_ms
from vizanix_atlas.publishing.daily import REPORT_FILENAME, compact_day
from vizanix_atlas.publishing.publish import publish_generation
from vizanix_atlas.publishing.publisher import FilesystemPublisher
from vizanix_atlas.storage.dataset_builder import build_dataset_files


class LocalSource:
    """Serves generation files straight from a FilesystemPublisher tree."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def fetch_generation_file(self, generation_id: str, filename: str) -> bytes:
        return (self.root / f"atlas-data-{generation_id}" / filename).read_bytes()


async def _publish_at(publisher: FilesystemPublisher, tmp: Path, moment: datetime) -> str:
    config = _permissive_config()
    generation = build_sample_generation(effective_time=to_epoch_ms(moment))
    out = tmp / f"stage-{generation.generation_id}"
    built = build_dataset_files(generation, out, config=config)
    outcome = await publish_generation(
        generation,
        dataset_dir=out,
        written_files=list(built.written_files),
        publisher=publisher,
        config=config,
        release_tag=f"atlas-data-{generation.generation_id}",
        shard_indices=built.shard_indices,
    )
    assert outcome.published
    return generation.generation_id


async def test_compact_day_records_gaps_and_publishes_verified_output(tmp_path: Path) -> None:
    root = tmp_path / "releases"
    publisher = FilesystemPublisher(root)
    await _publish_at(publisher, tmp_path, datetime(2026, 1, 5, 0, 7, tzinfo=UTC))
    await _publish_at(publisher, tmp_path, datetime(2026, 1, 5, 0, 22, tzinfo=UTC))

    report = await compact_day(
        "2026-01-05",
        publisher=publisher,
        source=LocalSource(root),
        config=_permissive_config(),
        workdir=tmp_path / "work",
    )

    assert report.compacted
    assert report.successful_windows == ("20260105T000000Z", "20260105T001500Z")
    assert len(report.missing_windows) == 94  # never interpolated, only recorded
    day_dir = root / "atlas-data-2026-01-05"
    assert (day_dir / REPORT_FILENAME).is_file()
    shard = next(day_dir.glob("assets-*.parquet"))
    frame = pl.read_parquet(shard)
    assert set(frame["slot_label"].unique()) == {"20260105T000000Z", "20260105T001500Z"}

    manifest = await publisher.read_generation_manifest("2026-01-05")
    assert manifest is not None
    assert manifest.file(REPORT_FILENAME) is not None


async def test_compact_day_with_no_generations_publishes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "releases"
    report = await compact_day(
        "2026-01-06",
        publisher=FilesystemPublisher(root),
        source=LocalSource(root),
        config=_permissive_config(),
        workdir=tmp_path / "work",
    )
    assert not report.compacted
    assert not (root / "atlas-data-2026-01-06").exists()


async def test_housekeeping_only_targets_compacted_old_generations(tmp_path: Path) -> None:
    from datetime import timedelta

    from vizanix_atlas.core.config import load_retention_config
    from vizanix_atlas.publishing.retention import find_candidates
    from vizanix_atlas.storage.housekeeping import plan_cleanup

    root = tmp_path / "releases"
    publisher = FilesystemPublisher(root)
    ids = [
        await _publish_at(publisher, tmp_path, datetime(2026, 1, 5, 0, minute, tzinfo=UTC))
        for minute in (7, 22, 37, 52)
    ]
    retention = load_retention_config()
    later = datetime.now(UTC) + timedelta(days=10)

    # Before compaction nothing has a verified replacement, so nothing may be deleted.
    before = await find_candidates(publisher, LocalSource(root), retention, now=later)
    assert before
    assert not plan_cleanup(before, retention.housekeeping).to_delete

    await compact_day(
        "2026-01-05",
        publisher=publisher,
        source=LocalSource(root),
        config=_permissive_config(),
        workdir=tmp_path / "work",
    )
    after = await find_candidates(publisher, LocalSource(root), retention, now=later)
    plan = plan_cleanup(after, retention.housekeeping)
    assert plan.to_delete
    # Only generations outside the recent window are eligible.
    assert {c.generation_id for c in plan.to_delete} == {sorted(ids)[0]}
