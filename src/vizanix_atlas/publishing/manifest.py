"""Manifest construction.

The manifest is the only thing a client should trust to say a generation is complete
(see ``docs/STORAGE.md``, section 8). This module builds it from a
:class:`~vizanix_atlas.discovery.pipeline.Generation` and the files that were actually
written for it - never from what the code *intended* to write, so that a file the
writer silently skipped cannot be described as present.
"""

from __future__ import annotations

from collections.abc import Sequence

from vizanix_atlas.core.atlas_time import from_epoch_ms, to_iso, utc_now
from vizanix_atlas.core.versions import (
    DATASET_FORMAT_VERSION,
    METHODOLOGY_VERSION,
    SCHEMA_VERSION,
    SOFTWARE_VERSION,
    commit_sha,
    python_version,
)
from vizanix_atlas.discovery.pipeline import Generation
from vizanix_atlas.models.enums import CollectionStatus
from vizanix_atlas.models.manifest import (
    BuildProvenance,
    FileEntry,
    GenerationManifest,
    LatestPointer,
    VenueOutcome,
)
from vizanix_atlas.storage.writer import WrittenFile


def _venue_outcome(generation: Generation) -> VenueOutcome:
    """Summarise per-venue collection status for the manifest."""
    by_status = {status: generation.venues_by_status(status) for status in CollectionStatus}
    return VenueOutcome(
        attempted=len(generation.health),
        successful=len(by_status[CollectionStatus.SUCCESS]),
        degraded=len(by_status[CollectionStatus.DEGRADED]),
        failed=len(by_status[CollectionStatus.FAILED]),
        disabled=len(by_status[CollectionStatus.DISABLED]),
        unavailable_from_collector_network=len(
            by_status[CollectionStatus.UNAVAILABLE_FROM_COLLECTOR_NETWORK]
        ),
        successful_slugs=by_status[CollectionStatus.SUCCESS],
        degraded_slugs=by_status[CollectionStatus.DEGRADED],
        failed_slugs=by_status[CollectionStatus.FAILED],
    )


def build_file_entries(
    written: Sequence[WrittenFile],
    *,
    generation_id: str,
    shard_indices: dict[str, int] | None = None,
) -> tuple[FileEntry, ...]:
    """Turn written files into manifest entries.

    Args:
        written: Every file actually written for this generation.
        generation_id: Stamped onto each entry so a client can confirm a downloaded
            file belongs to the generation its manifest claims to describe.
        shard_indices: Maps a shard file's name to its shard index, for the sharded
            asset-state files. Files not present in this mapping get no shard index.

    """
    shard_indices = shard_indices or {}
    return tuple(
        FileEntry(
            filename=file.filename,
            size_bytes=file.size_bytes,
            sha256=file.sha256,
            row_count=file.row_count,
            table=file.table,
            generation_id=generation_id,
            shard_index=shard_indices.get(file.filename),
            compression="zstd" if file.filename.endswith(".parquet") else None,
            content_type=(
                "application/vnd.apache.parquet"
                if file.filename.endswith(".parquet")
                else "application/json"
            ),
        )
        for file in written
    )


def build_manifest(
    generation: Generation,
    files: Sequence[FileEntry],
    *,
    shard_count: int,
    previous_generation_id: str | None = None,
    runner: str | None = None,
    workflow_run_id: str | None = None,
    workflow_run_attempt: str | None = None,
    published_at: str | None = None,
) -> GenerationManifest:
    """Build the complete manifest for a generation.

    ``published_at`` is left ``None`` until the manifest is actually published; see
    :mod:`vizanix_atlas.publishing.publisher`, which fills it in as the last step of
    the publication sequence, after every file has been verified.
    """
    return GenerationManifest(
        dataset_format_version=DATASET_FORMAT_VERSION,
        schema_version=SCHEMA_VERSION,
        methodology_version=METHODOLOGY_VERSION,
        software_version=SOFTWARE_VERSION,
        generation_id=generation.generation_id,
        slot_label=generation.slot_label,
        collection_started_at=to_iso(from_epoch_ms(generation.collection_started_at)),
        collection_finished_at=to_iso(from_epoch_ms(generation.collection_finished_at)),
        snapshot_effective_time=to_iso(from_epoch_ms(generation.snapshot_effective_time)),
        published_at=published_at,
        venues=_venue_outcome(generation),
        asset_count=generation.asset_count,
        instrument_count=generation.instrument_count,
        venue_count=generation.venue_count,
        observation_count=sum(h.ticker_count for h in generation.health),
        shard_count=shard_count,
        files=tuple(files),
        build=BuildProvenance(
            commit_sha=commit_sha(),
            software_version=SOFTWARE_VERSION,
            python_version=python_version(),
            runner=runner,
            workflow_run_id=workflow_run_id,
            workflow_run_attempt=workflow_run_attempt,
        ),
        previous_generation_id=previous_generation_id,
    )


def build_latest_pointer(
    manifest: GenerationManifest,
    manifest_file: FileEntry,
    *,
    release_tag: str,
    previous_generation_id: str | None,
    latest_attempt_at: str | None = None,
) -> LatestPointer:
    """Build the small pointer document that names the current generation.

    Written last in the publication sequence (see
    :mod:`vizanix_atlas.publishing.publish`); everything a client needs to verify a
    generation is already durable by the time this exists.
    """
    now = to_iso(utc_now())
    return LatestPointer(
        schema_version=manifest.schema_version,
        generation_id=manifest.generation_id,
        generated_at=now,
        manifest_filename=manifest_file.filename,
        manifest_sha256=manifest_file.sha256,
        release_tag=release_tag,
        latest_attempt_at=latest_attempt_at or now,
        latest_success_at=now,
        previous_generation_id=previous_generation_id,
    )
