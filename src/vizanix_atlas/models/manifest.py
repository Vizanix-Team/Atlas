"""Dataset manifest models.

A manifest is the only trustworthy description of a generation. Clients must never
conclude a file is valid because it exists: GitHub Releases offer no transaction,
so a partially uploaded generation is a normal intermediate state. The manifest is
published last and names every file with its size, hash and row count
(see ``docs/STORAGE.md``).
"""

from __future__ import annotations

from pydantic import Field, field_validator

from vizanix_atlas.models.base import AtlasModel

_SHA256_HEX_LENGTH = 64


class FileEntry(AtlasModel):
    """One published file, described well enough to verify without downloading."""

    filename: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(description="Lowercase hex digest of the file's bytes.")
    row_count: int | None = Field(
        default=None, ge=0, description="Rows in the table; null for non-tabular files."
    )
    table: str | None = Field(default=None, description="Logical table name, for tabular files.")
    generation_id: str
    shard_index: int | None = Field(
        default=None, ge=0, description="Shard index, for sharded asset-state files."
    )
    compression: str | None = None
    content_type: str = "application/octet-stream"

    @field_validator("sha256")
    @classmethod
    def _check_digest(cls, value: str) -> str:
        """Require a well-formed lowercase SHA-256 hex digest.

        Enforced here so a malformed digest cannot reach a published manifest, where
        it would make verification fail for every client.
        """
        normalised = value.strip().lower()
        if len(normalised) != _SHA256_HEX_LENGTH or any(
            c not in "0123456789abcdef" for c in normalised
        ):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return normalised


class VenueOutcome(AtlasModel):
    """Per-venue collection outcome, summarised for the manifest."""

    attempted: int = Field(default=0, ge=0)
    successful: int = Field(default=0, ge=0)
    degraded: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    disabled: int = Field(default=0, ge=0)
    unavailable_from_collector_network: int = Field(default=0, ge=0)
    successful_slugs: tuple[str, ...] = ()
    degraded_slugs: tuple[str, ...] = ()
    failed_slugs: tuple[str, ...] = ()

    @property
    def success_ratio(self) -> float | None:
        """Successful over attempted, or ``None`` when nothing was attempted.

        Disabled and network-unavailable venues are counted in ``attempted`` so that
        the ratio reflects what Atlas actually managed to collect.
        """
        if self.attempted == 0:
            return None
        return self.successful / self.attempted


class BuildProvenance(AtlasModel):
    """What produced a generation, recorded for reproducibility."""

    commit_sha: str | None = None
    software_version: str
    python_version: str | None = None
    runner: str | None = Field(
        default=None, description="Where the generation was built, for example 'github-actions'."
    )
    workflow_run_id: str | None = None
    workflow_run_attempt: str | None = None
    dependency_lock_sha256: str | None = Field(
        default=None, description="Hash of the resolved dependency set, when one was recorded."
    )


class GenerationManifest(AtlasModel):
    """The complete description of one dataset generation.

    ``generation_id`` is immutable and is what the ``latest`` pointer references, so
    that advancing ``latest`` is a single small write rather than a bulk copy.
    """

    atlas: str = "Vizanix Atlas"
    dataset_format_version: str
    schema_version: str
    methodology_version: str
    software_version: str

    generation_id: str = Field(
        description="Immutable identifier, for example 20260927T184500Z-2c7af12."
    )
    slot_label: str = Field(description="The scheduled observation slot this generation covers.")

    collection_started_at: str
    collection_finished_at: str
    snapshot_effective_time: str
    published_at: str | None = None

    venues: VenueOutcome
    asset_count: int = Field(default=0, ge=0)
    instrument_count: int = Field(default=0, ge=0)
    venue_count: int = Field(default=0, ge=0)
    observation_count: int = Field(default=0, ge=0)
    shard_count: int = Field(default=1, ge=1)

    files: tuple[FileEntry, ...] = ()
    build: BuildProvenance
    previous_generation_id: str | None = Field(
        default=None,
        description=(
            "The generation this one supersedes. Lets a client detect that it has "
            "skipped a generation, and lets housekeeping find collectable ancestors."
        ),
    )
    notes: tuple[str, ...] = ()

    @property
    def total_bytes(self) -> int:
        """Sum of every published file's size."""
        return sum(f.size_bytes for f in self.files)

    def file(self, filename: str) -> FileEntry | None:
        """Return the entry for ``filename``, or ``None`` if the manifest omits it."""
        return next((f for f in self.files if f.filename == filename), None)

    def shard_files(self) -> tuple[FileEntry, ...]:
        """Return the asset-state shard files, ordered by shard index."""
        shards = [f for f in self.files if f.shard_index is not None]
        return tuple(sorted(shards, key=lambda f: f.shard_index or 0))


class LatestPointer(AtlasModel):
    """The canonical pointer to the current generation.

    Written last in the publication sequence and kept deliberately small, because
    the window during which it is being replaced is the only window in which a
    client can see an inconsistent dataset.
    """

    schema_version: str
    generation_id: str
    generated_at: str
    manifest_filename: str
    manifest_sha256: str
    release_tag: str
    latest_attempt_at: str | None = Field(
        default=None,
        description=(
            "When Atlas last tried to publish. Differs from generated_at when the most "
            "recent attempt failed its validity gate and the previous generation stands."
        ),
    )
    latest_success_at: str | None = None
    previous_generation_id: str | None = None


class DailyCompactionReport(AtlasModel):
    """What daily compaction found and produced.

    Missing windows are recorded explicitly. Atlas never fabricates a snapshot for a
    scheduled run that GitHub did not execute (see ``docs/OPERATIONS.md``).
    """

    day: str = Field(description="UTC calendar day, as YYYY-MM-DD.")
    expected_windows: tuple[str, ...] = ()
    successful_windows: tuple[str, ...] = ()
    missing_windows: tuple[str, ...] = ()
    duplicate_windows: tuple[str, ...] = ()
    invalid_windows: tuple[str, ...] = ()
    compacted: bool = False
    compacted_at: str | None = None
    source_snapshot_count: int = Field(default=0, ge=0)
    output_row_counts: dict[str, int] = Field(default_factory=dict)
    source_assets_deleted: bool = Field(
        default=False,
        description="Whether intra-day snapshots were removed after compacted output was verified.",
    )
    notes: tuple[str, ...] = ()

    @property
    def completeness(self) -> float | None:
        """Successful windows over expected windows, or ``None`` if none were expected."""
        if not self.expected_windows:
            return None
        return len(self.successful_windows) / len(self.expected_windows)


class SystemHealth(AtlasModel):
    """Operational health of the Atlas pipeline itself.

    Published as ``system-health.json`` and rendered by the website, so that a stale
    dataset is visibly stale rather than silently old.
    """

    generated_at: str
    latest_attempt_at: str | None = None
    latest_success_at: str | None = None
    latest_generation_id: str | None = None
    snapshot_age_seconds: float | None = None
    schema_version: str
    software_version: str
    methodology_version: str
    venues_successful: tuple[str, ...] = ()
    venues_degraded: tuple[str, ...] = ()
    venues_failed: tuple[str, ...] = ()
    venues_disabled: tuple[str, ...] = ()
    venues_unavailable_from_collector_network: tuple[str, ...] = ()
    asset_count: int = Field(default=0, ge=0)
    instrument_count: int = Field(default=0, ge=0)
    collection_duration_ms: int | None = Field(default=None, ge=0)
    missing_windows_today: tuple[str, ...] = ()
    dataset_bytes: int | None = Field(default=None, ge=0)
    notes: tuple[str, ...] = ()


class StorageReport(AtlasModel):
    """Measured dataset growth, so maintainers can see cost trends."""

    generated_at: str
    current_generation_bytes: int = Field(ge=0)
    current_generation_rows: int = Field(ge=0)
    daily_release_bytes: dict[str, int] = Field(default_factory=dict)
    total_release_bytes: int = Field(default=0, ge=0)
    compression_ratio: float | None = Field(
        default=None, gt=0, description="Uncompressed over compressed size, where both are known."
    )
    release_count: int = Field(default=0, ge=0)
    asset_count: int = Field(default=0, ge=0)
    notes: tuple[str, ...] = ()
