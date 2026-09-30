"""Retention-based cleanup.

Every guard in ``config/retention.yaml`` is enforced here before anything is deleted
(see ``docs/OPERATIONS.md`` sections 193-195). Planning and executing a cleanup are
two separate functions on purpose: :func:`plan_cleanup` is pure and side-effect-free,
so a maintainer (or a test) can inspect exactly what a run would do before anything
calls a ``Publisher``'s delete method, and a workflow can log the plan even when it
runs in dry-run mode.

Scope: this module deletes individual **files** within an existing generation's
release - the redundant intra-day snapshot bundles a successful daily compaction has
already superseded (see ``docs/STORAGE.md`` section 10). It does not delete whole
releases or generations; nothing in Atlas does that automatically, and
``never_delete_tag_prefix`` exists specifically so a software release can never be
reached by this code path regardless of what else changes here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from vizanix_atlas.core.config import HousekeepingConfig
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.publishing.publisher import Publisher

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DeletionCandidate:
    """One file that retention policy might consider removing.

    Attributes:
        generation_id: Which generation's release the file belongs to.
        tag: The release tag the file lives under (``atlas-data-<generation_id>`` or
            ``data-latest``), checked against the configured prefixes.
        filename: The asset's name within that release.
        age_hours: How long since the file was published.
        has_compacted_replacement: Whether a verified daily-compacted table already
            covers this file's observations.
        checksum_verified: Whether the file's current bytes have been re-hashed and
            matched against its manifest entry in this run.

    """

    generation_id: str
    tag: str
    filename: str
    age_hours: float
    has_compacted_replacement: bool
    checksum_verified: bool


@dataclass(frozen=True, slots=True)
class SkippedCandidate:
    """A candidate that was not deleted, and why."""

    candidate: DeletionCandidate
    reason: str


@dataclass(slots=True)
class CleanupPlan:
    """What a cleanup run would do, computed without touching anything."""

    to_delete: list[DeletionCandidate] = field(default_factory=list)
    skipped: list[SkippedCandidate] = field(default_factory=list)

    def summary(self) -> str:
        """A one-line summary for logs and workflow output."""
        return f"{len(self.to_delete)} to delete, {len(self.skipped)} skipped"


def plan_cleanup(
    candidates: Sequence[DeletionCandidate], config: HousekeepingConfig
) -> CleanupPlan:
    """Decide what a cleanup run would delete, applying every guard in order.

    Every candidate that fails a guard is recorded in ``skipped`` with the specific
    reason, so a maintainer reviewing a dry run never has to guess why a file that
    looks stale was left alone.
    """
    plan = CleanupPlan()
    for candidate in candidates:
        if not any(candidate.tag.startswith(prefix) for prefix in config.require_tag_prefix):
            plan.skipped.append(
                SkippedCandidate(candidate, "tag does not match any allowed prefix")
            )
            continue
        if any(candidate.tag.startswith(prefix) for prefix in config.never_delete_tag_prefix):
            plan.skipped.append(SkippedCandidate(candidate, "tag matches a protected prefix"))
            continue
        if candidate.age_hours < config.min_age_hours_before_delete:
            plan.skipped.append(
                SkippedCandidate(
                    candidate,
                    f"age {candidate.age_hours:.1f}h is below the "
                    f"{config.min_age_hours_before_delete}h minimum",
                )
            )
            continue
        if config.require_compacted_replacement and not candidate.has_compacted_replacement:
            plan.skipped.append(
                SkippedCandidate(candidate, "no verified compacted replacement exists yet")
            )
            continue
        if config.require_checksum_verification and not candidate.checksum_verified:
            plan.skipped.append(
                SkippedCandidate(candidate, "checksum has not been verified this run")
            )
            continue
        if len(plan.to_delete) >= config.max_deletions_per_run:
            plan.skipped.append(
                SkippedCandidate(
                    candidate,
                    f"run already reached max_deletions_per_run ({config.max_deletions_per_run})",
                )
            )
            continue
        plan.to_delete.append(candidate)
    return plan


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """What actually happened when a plan was carried out."""

    dry_run: bool
    deleted: tuple[DeletionCandidate, ...]
    skipped: tuple[str, ...]


async def execute_cleanup(
    plan: CleanupPlan,
    publisher: Publisher,
    *,
    dry_run: bool = True,
    log_every_deletion: bool = True,
) -> ExecutionReport:
    """Carry out (or simulate) a cleanup plan.

    Args:
        plan: The plan from :func:`plan_cleanup`. Never recomputed here: this function
            trusts what it is given rather than re-deriving it, so a caller can log or
            review the exact plan that will be executed.
        publisher: Where the deletions happen. Untouched entirely in dry-run mode.
        dry_run: When true (the default, matching
            ``config/retention.yaml``'s ``dry_run_default``), nothing is deleted; the
            plan is only logged.
        log_every_deletion: Whether each deletion is logged individually, as
            ``config/retention.yaml`` requires for a real run.

    """
    deleted: list[DeletionCandidate] = []
    for candidate in plan.to_delete:
        if dry_run:
            _log.info(
                "dry run: would delete",
                extra={
                    "generation_id": candidate.generation_id,
                    "filename": candidate.filename,
                    "age_hours": round(candidate.age_hours, 1),
                },
            )
        else:
            await publisher.delete_generation_file(candidate.generation_id, candidate.filename)
            if log_every_deletion:
                _log.info(
                    "deleted",
                    extra={
                        "generation_id": candidate.generation_id,
                        "filename": candidate.filename,
                        "age_hours": round(candidate.age_hours, 1),
                    },
                )
        deleted.append(candidate)
    return ExecutionReport(
        dry_run=dry_run,
        deleted=tuple(deleted),
        skipped=tuple(f"{s.candidate.filename}: {s.reason}" for s in plan.skipped),
    )


def select_generations_to_retain(
    generation_ids_newest_first: Sequence[str], *, keep_recent_generations: int
) -> frozenset[str]:
    """Return the generations that must remain fully downloadable.

    Keeping more than one recent generation (``config/retention.yaml``,
    ``generations.keep_recent_generations``) is what lets a client whose download was
    interrupted mid-generation-switch fall back to the previous complete one instead
    of failing outright (see ``docs/STORAGE.md`` section 7).

    Raises:
        ValueError: If ``keep_recent_generations`` is not positive.

    """
    if keep_recent_generations < 1:
        raise ValueError("keep_recent_generations must be at least 1")
    return frozenset(generation_ids_newest_first[:keep_recent_generations])
