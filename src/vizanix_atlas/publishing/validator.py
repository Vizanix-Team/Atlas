"""The publish validity gate.

A generation becomes ``latest`` only after passing every check here (see
``docs/STORAGE.md`` sections 8 and 36-38). The gate exists because a bug elsewhere in
the pipeline - a bad API response, an aggregation regression, a schema change nobody
taught the adapter about - should degrade the dataset's quality, not corrupt the
canonical release. Every failure carries a specific, actionable reason; nothing here
ever refuses silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from vizanix_atlas.core.config import AnomalyGuards
from vizanix_atlas.discovery.pipeline import Generation
from vizanix_atlas.models.manifest import GenerationManifest
from vizanix_atlas.storage.writer import sha256_file


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One reason a generation failed its validity gate."""

    check: str
    detail: str


@dataclass(slots=True)
class ValidationReport:
    """The outcome of running every check against one candidate generation."""

    issues: list[ValidationIssue] = field(default_factory=list)

    def fail(self, check: str, detail: str) -> None:
        """Record a failed check. Does not raise; callers decide what to do with a
        report that has issues, so a caller inspecting several candidates in sequence
        is not forced into exception handling to do it.
        """
        self.issues.append(ValidationIssue(check=check, detail=detail))

    @property
    def passed(self) -> bool:
        """Whether every check succeeded."""
        return not self.issues

    def summary(self) -> str:
        """A one-line-per-issue summary, for logs and CI output."""
        if self.passed:
            return "validation passed"
        return "; ".join(f"{i.check}: {i.detail}" for i in self.issues)


#: Required tables. A generation missing one of these is not publishable regardless of
#: how many assets it produced, because the dataset would be internally inconsistent
#: (instruments with no assets table to resolve their base_asset_id against, for
#: example).
REQUIRED_TABLES: tuple[str, ...] = (
    "assets",
    "asset_aliases",
    "instruments",
    "adapter_health",
    "venue_asset_state",
    "asset_market_state",
)


def validate_schema(manifest: GenerationManifest) -> ValidationReport:
    """Check that every required table is present and every file's basic shape holds."""
    report = ValidationReport()
    published_tables = {f.table for f in manifest.files if f.table is not None}
    missing = [t for t in REQUIRED_TABLES if t not in published_tables]
    if missing:
        report.fail("schema", f"missing required tables: {sorted(missing)}")

    for entry in manifest.files:
        if entry.size_bytes <= 0 and entry.table is not None:
            report.fail("schema", f"{entry.filename} is empty but claims a table")
        if entry.generation_id != manifest.generation_id:
            report.fail(
                "schema",
                f"{entry.filename} is stamped with generation_id "
                f"{entry.generation_id!r}, expected {manifest.generation_id!r}",
            )
    return report


def validate_checksums(manifest: GenerationManifest, dataset_dir: Path) -> ValidationReport:
    """Recompute every file's hash from disk and compare it to the manifest.

    This is what ``atlas verify`` runs, and what a client should run before trusting a
    download (see ``docs/OPERATIONS.md``, ``atlas verify``).
    """
    report = ValidationReport()
    for entry in manifest.files:
        path = dataset_dir / entry.filename
        if not path.is_file():
            report.fail(
                "checksum", f"{entry.filename} is listed in the manifest but missing on disk"
            )
            continue
        actual = sha256_file(path)
        if actual != entry.sha256:
            report.fail(
                "checksum",
                f"{entry.filename} hash mismatch: manifest says {entry.sha256}, disk has {actual}",
            )
        actual_size = path.stat().st_size
        if actual_size != entry.size_bytes:
            report.fail(
                "checksum",
                f"{entry.filename} size mismatch: manifest says {entry.size_bytes}, disk has {actual_size}",
            )
    return report


def validate_row_counts(manifest: GenerationManifest) -> ValidationReport:
    """Check that row counts are non-negative and that the totals reconcile.

    A sharded table's row counts must sum to the manifest's own declared asset count,
    which is the cheapest possible check that a shard was not silently dropped between
    being written and being described.
    """
    report = ValidationReport()
    shard_files = manifest.shard_files()
    if shard_files:
        total = sum(f.row_count or 0 for f in shard_files)
        if total != manifest.asset_count:
            report.fail(
                "row_counts",
                f"asset-state shards sum to {total} rows, manifest declares "
                f"asset_count={manifest.asset_count}",
            )
    for entry in manifest.files:
        if entry.row_count is not None and entry.row_count < 0:
            report.fail("row_counts", f"{entry.filename} declares a negative row count")
    return report


def validate_anomalies(
    generation: Generation, guards: AnomalyGuards, previous: GenerationManifest | None
) -> ValidationReport:
    """Compare a candidate generation against the previous published one.

    Without a previous generation to compare against (the very first publish), only
    the absolute floors apply; the relative-change guards have nothing to be relative
    to yet.
    """
    report = ValidationReport()

    successful_venues = sum(1 for h in generation.health if h.status in ("success", "degraded"))
    attempted = len(generation.health) or 1
    if successful_venues < guards.min_successful_venues:
        report.fail(
            "anomaly",
            f"only {successful_venues} venues succeeded; the floor is "
            f"{guards.min_successful_venues}",
        )
    ratio = successful_venues / attempted
    if ratio < guards.min_successful_venue_ratio:
        report.fail(
            "anomaly",
            f"only {ratio:.0%} of attempted venues succeeded; the floor is "
            f"{guards.min_successful_venue_ratio:.0%}",
        )

    total_requests = sum(h.requests_successful + h.parse_failures for h in generation.health)
    total_parse_failures = sum(h.parse_failures for h in generation.health)
    if total_requests > 0:
        failure_ratio = total_parse_failures / total_requests
        if failure_ratio > guards.max_adapter_parse_failure_ratio:
            report.fail(
                "anomaly",
                f"{failure_ratio:.0%} of adapter responses failed to parse; the "
                f"ceiling is {guards.max_adapter_parse_failure_ratio:.0%}",
            )

    if previous is None:
        return report

    if previous.asset_count > 0:
        asset_drop = 1.0 - (generation.asset_count / previous.asset_count)
        if asset_drop > guards.max_asset_count_drop_ratio:
            report.fail(
                "anomaly",
                f"asset count dropped {asset_drop:.0%} versus the previous generation "
                f"({previous.asset_count} -> {generation.asset_count}); the ceiling is "
                f"{guards.max_asset_count_drop_ratio:.0%}",
            )
    if previous.instrument_count > 0:
        instrument_drop = 1.0 - (generation.instrument_count / previous.instrument_count)
        if instrument_drop > guards.max_instrument_count_drop_ratio:
            report.fail(
                "anomaly",
                f"instrument count dropped {instrument_drop:.0%} versus the previous "
                f"generation ({previous.instrument_count} -> "
                f"{generation.instrument_count}); the ceiling is "
                f"{guards.max_instrument_count_drop_ratio:.0%}",
            )
    return report


def run_validity_gate(
    generation: Generation,
    manifest: GenerationManifest,
    dataset_dir: Path,
    *,
    guards: AnomalyGuards,
    previous: GenerationManifest | None,
) -> ValidationReport:
    """Run every check and return the combined report.

    A caller publishes only when ``report.passed`` is true. This function never raises
    on a failed check; raising is the caller's decision, made in
    :mod:`vizanix_atlas.publishing.publish`, which also decides what happens to the
    previous generation when this one is refused.
    """
    report = ValidationReport()
    for sub_report in (
        validate_schema(manifest),
        validate_checksums(manifest, dataset_dir),
        validate_row_counts(manifest),
        validate_anomalies(generation, guards, previous),
    ):
        report.issues.extend(sub_report.issues)
    return report
