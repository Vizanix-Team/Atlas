"""Daily compaction: many 15-minute generations become one release per UTC day.

Gap-aware by construction. The set of expected slots comes from the schedule; a slot
with no generation is recorded as missing and is never interpolated or reconstructed
(``docs/OPERATIONS.md``). Only after every compacted file has been uploaded *and*
re-downloaded and re-hashed does the report claim ``compacted=True``, which is the
signal housekeeping later trusts before it deletes a source file.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import polars as pl

from vizanix_atlas.core.atlas_time import to_iso, utc_now
from vizanix_atlas.core.config import CollectionConfig
from vizanix_atlas.core.errors import ChecksumMismatch, PublicationFailure
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.core.versions import (
    DATASET_FORMAT_VERSION,
    METHODOLOGY_VERSION,
    SCHEMA_VERSION,
    SOFTWARE_VERSION,
)
from vizanix_atlas.models.manifest import (
    BuildProvenance,
    DailyCompactionReport,
    GenerationManifest,
    VenueOutcome,
)
from vizanix_atlas.publishing.manifest import build_file_entries
from vizanix_atlas.publishing.publisher import Publisher
from vizanix_atlas.sdk.dataset import Dataset
from vizanix_atlas.sdk.remote import GenerationFileSource
from vizanix_atlas.storage.compaction import (
    build_daily_report,
    compact_tables,
    discover_day_generations,
    generations_to_compact,
)
from vizanix_atlas.storage.writer import WrittenFile, sha256_file, write_json, write_parquet

_log = get_logger(__name__)

REPORT_FILENAME = "daily-report.json"

#: Sharded tables are compacted shard by shard so a daily file never exceeds what one
#: shard of one generation implies times the number of slots.
_UNSHARDED_DAILY_TABLES = ("adapter_health",)


def _tag_free_day_id(day: str) -> str:
    """Return the generation identifier used for a day's release (``YYYY-MM-DD``)."""
    return day


async def _verify_round_trip(
    source: GenerationFileSource, day_id: str, written: list[WrittenFile]
) -> None:
    """Re-download every uploaded file and compare its hash to the local original."""
    for file in written:
        data = source.fetch_generation_file(day_id, file.filename)
        actual = hashlib.sha256(data).hexdigest()
        if actual != file.sha256:
            raise ChecksumMismatch(
                "an uploaded daily file did not round-trip intact",
                filename=file.filename,
                expected=file.sha256,
                actual=actual,
            )


async def compact_day(
    day: str,
    *,
    publisher: Publisher,
    source: GenerationFileSource,
    config: CollectionConfig,
    workdir: Path | None = None,
) -> DailyCompactionReport:
    """Compact one UTC day's generations into ``atlas-data-<day>``.

    Args:
        day: The UTC day to compact, ``YYYY-MM-DD``.
        publisher: Where generation manifests are read and the daily release written.
        source: Where generation files are downloaded from (verified against manifests).
        config: Collection policy (schedule and size budgets).
        workdir: Scratch directory; a temporary one is used when omitted.

    Returns:
        The published report. ``compacted`` is false (and nothing is uploaded) when the
        day has no compactable generation.

    Raises:
        PublicationFailure: If an upload fails; the day's release is then left without a
            ``compacted`` report, so housekeeping treats it as not replaced.
        ChecksumMismatch: If a source download or an uploaded file fails verification.

    """
    manifests = []
    for generation_id in await publisher.list_generation_ids():
        if generation_id == _tag_free_day_id(day) or not generation_id[:1].isdigit():
            continue
        manifest = await publisher.read_generation_manifest(generation_id)
        if manifest is not None:
            manifests.append(manifest)

    discovered = discover_day_generations(manifests, day)
    report = build_daily_report(day, discovered, config.schedule)
    chosen = generations_to_compact(discovered, report)
    if not chosen:
        _log.warning("nothing to compact", extra={"day": day})
        return report

    owns_dir = workdir is None
    scratch = Path(tempfile.mkdtemp(prefix="atlas-daily-")) if owns_dir else workdir
    assert scratch is not None
    out_dir = scratch / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    shard_frames: dict[int, list[pl.DataFrame]] = {}
    table_frames: dict[str, list[pl.DataFrame]] = {name: [] for name in _UNSHARDED_DAILY_TABLES}
    for generation in chosen:
        dataset = Dataset(
            manifest=generation.manifest,
            root=scratch / "src" / generation.generation_id,
            downloader=source,
        )
        for entry in generation.manifest.shard_files():
            frame = pl.read_parquet(dataset.ensure_file(entry.filename)).with_columns(
                pl.lit(generation.slot_label).alias("slot_label"),
                pl.lit(generation.generation_id).alias("generation_id"),
            )
            shard_frames.setdefault(entry.shard_index or 0, []).append(frame)
        for name in _UNSHARDED_DAILY_TABLES:
            table_file = f"{name}.parquet"
            if generation.manifest.file(table_file) is not None:
                table_frames[name].append(
                    pl.read_parquet(dataset.ensure_file(table_file)).with_columns(
                        pl.lit(generation.slot_label).alias("slot_label")
                    )
                )

    written: list[WrittenFile] = []
    shard_indices: dict[str, int] = {}
    for index, frames in sorted(shard_frames.items()):
        name = f"assets-{index:02d}.parquet"
        combined = compact_tables({"asset_market_state": frames})["asset_market_state"]
        written.append(
            write_parquet(
                combined, out_dir / name, table="asset_market_state", config=config.publishing
            )
        )
        shard_indices[name] = index
    for table, frame in compact_tables(table_frames).items():
        written.append(
            write_parquet(
                frame, out_dir / f"{table}.parquet", table=table, config=config.publishing
            )
        )

    last = chosen[-1].manifest
    report = report.model_copy(
        update={
            "compacted": False,
            "output_row_counts": {f.filename: f.row_count or 0 for f in written},
        }
    )
    day_id = _tag_free_day_id(day)
    try:
        for file in written:
            await publisher.upload_generation_file(day_id, file.filename, file.path)
        await _verify_round_trip(source, day_id, written)
    except (PublicationFailure, ChecksumMismatch):
        _log.error("daily compaction failed before the report was published", extra={"day": day})
        raise

    report = report.model_copy(update={"compacted": True, "compacted_at": to_iso(utc_now())})
    report_file = write_json(report.model_dump_json(indent=2), out_dir / REPORT_FILENAME)
    await publisher.upload_generation_file(day_id, REPORT_FILENAME, report_file.path)

    files = build_file_entries(
        [*written, report_file], generation_id=day_id, shard_indices=shard_indices
    )
    manifest = GenerationManifest(
        dataset_format_version=DATASET_FORMAT_VERSION,
        schema_version=SCHEMA_VERSION,
        methodology_version=METHODOLOGY_VERSION,
        software_version=SOFTWARE_VERSION,
        generation_id=day_id,
        slot_label=day,
        collection_started_at=chosen[0].manifest.collection_started_at,
        collection_finished_at=last.collection_finished_at,
        snapshot_effective_time=last.snapshot_effective_time,
        published_at=to_iso(utc_now()),
        venues=VenueOutcome(),
        asset_count=last.asset_count,
        instrument_count=last.instrument_count,
        venue_count=last.venue_count,
        shard_count=last.shard_count,
        files=files,
        build=BuildProvenance(software_version=SOFTWARE_VERSION),
        notes=(f"daily compaction of {len(chosen)} generation(s) for {day}",),
    )
    await publisher.publish_generation_manifest(day_id, manifest)
    _log.info(
        "daily compaction published",
        extra={"day": day, "generations": len(chosen), "missing": len(report.missing_windows)},
    )
    return report


def verify_local_files(files: list[WrittenFile]) -> bool:
    """Return whether every file still hashes to its recorded digest."""
    return all(sha256_file(f.path) == f.sha256 for f in files)
