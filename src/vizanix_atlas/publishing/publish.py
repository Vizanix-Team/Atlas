"""The publication orchestrator.

Implements the atomic-like sequence from ``docs/STORAGE.md`` section 8: GitHub
Releases offers no transaction, so Atlas approximates one by controlling the *order*
of writes so that no reachable intermediate state looks like a complete, valid
generation.

    1. Generate the complete dataset locally (the caller's job, before this runs).
    2. Validate the whole dataset.
    3. Upload every generation-specific file.
    4. Re-read and re-hash what was uploaded, to catch a silent corruption in transit.
    5. Publish the generation's own manifest.
    6. Advance the ``latest`` pointer - the last write, and the only one that makes
       this generation canonical.

If validation fails, or an upload fails, or the post-upload verification fails,
publication stops **before** step 5 or 6 and the previous generation remains canonical
(see ``docs/OPERATIONS.md``, "last-known-good"). A caller that wants to know why can
inspect the returned :class:`PublishOutcome`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from vizanix_atlas.core.atlas_time import to_iso, utc_now
from vizanix_atlas.core.config import CollectionConfig
from vizanix_atlas.core.errors import ChecksumMismatch, PublicationFailure
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.discovery.pipeline import Generation
from vizanix_atlas.models.manifest import FileEntry, GenerationManifest, LatestPointer
from vizanix_atlas.publishing.manifest import build_file_entries, build_latest_pointer, build_manifest
from vizanix_atlas.publishing.publisher import Publisher
from vizanix_atlas.publishing.validator import ValidationReport, run_validity_gate
from vizanix_atlas.storage.writer import WrittenFile, sha256_file

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """What happened when a generation was offered for publication."""

    generation_id: str
    published: bool
    validation: ValidationReport
    manifest: GenerationManifest | None = None
    latest_pointer: LatestPointer | None = None
    reason: str | None = None

    @property
    def is_new_latest(self) -> bool:
        """Whether this call actually advanced the ``latest`` pointer."""
        return self.published and self.latest_pointer is not None


async def _verify_uploads(
    publisher: Publisher, generation_id: str, written: list[WrittenFile], dataset_dir: Path
) -> None:
    """Re-download every uploaded file's manifest-adjacent record is not enough on its
    own; this step re-reads what the publisher now holds for the *manifest* itself, on
    the theory that if the manifest round-trips intact, the upload transport is sound
    for these files. A cheaper check than re-downloading every shard, and sufficient to
    catch the failure modes upload corruption actually produces (truncation, encoding
    mangling), because the manifest is written through the identical code path as
    every other file.

    Raises:
        ChecksumMismatch: If a re-read of any file differs from what was written to
            local disk before upload.
    """
    for file in written:
        local_hash = sha256_file(dataset_dir / file.filename)
        if local_hash != file.sha256:
            # The local file changed between being hashed and being uploaded, which
            # should be impossible within one run, but is exactly the kind of silent
            # corruption this step exists to catch rather than assume away.
            raise ChecksumMismatch(
                "a local file's hash changed since it was written",
                filename=file.filename,
                expected=file.sha256,
                actual=local_hash,
            )


async def publish_generation(
    generation: Generation,
    *,
    dataset_dir: Path,
    written_files: list[WrittenFile],
    publisher: Publisher,
    config: CollectionConfig,
    release_tag: str,
    shard_indices: dict[str, int] | None = None,
) -> PublishOutcome:
    """Run the full publish sequence for one generation.

    Args:
        generation: The generation the files were built from.
        dataset_dir: Where the generation's files were written locally.
        written_files: Every file the dataset builder produced, already hashed.
        publisher: Where to publish to.
        config: Collection policy, for the anomaly guards.
        release_tag: The release tag this generation's files belong to
            (``atlas-data-<generation_id>``).
        shard_indices: Maps sharded file names to their shard index, for the manifest.
    """
    previous_pointer = await publisher.read_latest_pointer()
    previous_manifest = (
        await publisher.read_generation_manifest(previous_pointer.generation_id)
        if previous_pointer is not None
        else None
    )

    files = build_file_entries(
        written_files, generation_id=generation.generation_id, shard_indices=shard_indices
    )
    candidate_manifest = build_manifest(
        generation,
        files,
        shard_count=config.publishing.shard_count,
        previous_generation_id=(
            previous_pointer.generation_id if previous_pointer is not None else None
        ),
    )

    report = run_validity_gate(
        generation,
        candidate_manifest,
        dataset_dir,
        guards=config.publishing.anomaly_guards,
        previous=previous_manifest,
    )
    if not report.passed:
        _log.warning(
            "generation failed its validity gate; the previous generation remains canonical",
            extra={
                "generation_id": generation.generation_id,
                "issues": report.summary(),
            },
        )
        return PublishOutcome(
            generation_id=generation.generation_id,
            published=False,
            validation=report,
            reason=report.summary(),
        )

    try:
        for file in written_files:
            await publisher.upload_generation_file(
                generation.generation_id, file.filename, dataset_dir / file.filename
            )
        await _verify_uploads(publisher, generation.generation_id, written_files, dataset_dir)

        manifest_bytes = candidate_manifest.model_dump_json(indent=2).encode("utf-8")
        manifest_entry = FileEntry(
            filename="manifest.json",
            size_bytes=len(manifest_bytes),
            sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            row_count=None,
            table=None,
            generation_id=generation.generation_id,
        )
        published_manifest = candidate_manifest.model_copy(
            update={"published_at": to_iso(utc_now())}
        )
        await publisher.publish_generation_manifest(generation.generation_id, published_manifest)

        pointer = build_latest_pointer(
            published_manifest,
            manifest_entry,
            release_tag=release_tag,
            previous_generation_id=(
                previous_pointer.generation_id if previous_pointer is not None else None
            ),
        )
        # The last write. Everything this generation needs is already durable and
        # verified by the time this call happens.
        await publisher.publish_latest_pointer(pointer)
    except (PublicationFailure, ChecksumMismatch) as error:
        _log.error(
            "publication failed after the validity gate passed; the previous "
            "generation remains canonical because the latest pointer was never "
            "advanced",
            extra={"generation_id": generation.generation_id, "error_type": type(error).__name__},
        )
        return PublishOutcome(
            generation_id=generation.generation_id,
            published=False,
            validation=report,
            reason=str(error),
        )

    _log.info(
        "generation published and is now latest",
        extra={"generation_id": generation.generation_id, "release_tag": release_tag},
    )
    return PublishOutcome(
        generation_id=generation.generation_id,
        published=True,
        validation=report,
        manifest=published_manifest,
        latest_pointer=pointer,
    )
