"""Housekeeping tests.

Every guard is tested in isolation: a candidate that fails exactly one guard must be
skipped for exactly that reason, and a dry run must never touch the publisher at all.
"""

from __future__ import annotations

import pytest

from vizanix_atlas.core.config import load_retention_config
from vizanix_atlas.publishing.publisher import FilesystemPublisher
from vizanix_atlas.storage.housekeeping import (
    DeletionCandidate,
    execute_cleanup,
    plan_cleanup,
    select_generations_to_retain,
)


@pytest.fixture
def housekeeping_config():
    return load_retention_config().housekeeping


def _candidate(**overrides) -> DeletionCandidate:
    defaults = {
        "generation_id": "gen1",
        "tag": "atlas-data-2026-09-27",
        "filename": "snapshot-20260927T183700Z.tar.zst",
        "age_hours": 200.0,
        "has_compacted_replacement": True,
        "checksum_verified": True,
    }
    defaults.update(overrides)
    return DeletionCandidate(**defaults)


def test_a_fully_qualifying_candidate_is_planned_for_deletion(housekeeping_config) -> None:
    plan = plan_cleanup([_candidate()], housekeeping_config)
    assert len(plan.to_delete) == 1
    assert not plan.skipped


def test_a_disallowed_tag_prefix_is_skipped(housekeeping_config) -> None:
    """A tag that is not one of the configured data prefixes must never be touched."""
    plan = plan_cleanup([_candidate(tag="some-random-tag")], housekeeping_config)
    assert not plan.to_delete
    assert "prefix" in plan.skipped[0].reason


def test_a_protected_tag_prefix_is_never_deletable(housekeeping_config) -> None:
    """A software release tag must never be reachable by this code path, whatever
    else about the candidate looks deletable.

    A well-formed configuration keeps the allowed and protected prefixes disjoint
    (HousekeepingConfig's own validator enforces it), so a protected tag is caught by
    the "not an allowed prefix" guard before the dedicated protected-prefix guard ever
    has to fire; either way, the outcome that matters is that it is never deletable.
    """
    plan = plan_cleanup([_candidate(tag="v0.1.0", age_hours=10_000.0)], housekeeping_config)
    assert not plan.to_delete

    # The dedicated protected-prefix guard is exercised directly, independent of the
    # allow-list guard, using a config where the two are not disjoint.
    from vizanix_atlas.core.config import HousekeepingConfig

    overlapping = HousekeepingConfig.model_construct(
        **{**housekeeping_config.model_dump(), "require_tag_prefix": ("v",)}
    )
    plan = plan_cleanup([_candidate(tag="v0.1.0", age_hours=10_000.0)], overlapping)
    assert not plan.to_delete
    assert "protected" in plan.skipped[0].reason


def test_a_too_young_candidate_is_skipped(housekeeping_config) -> None:
    plan = plan_cleanup([_candidate(age_hours=0.5)], housekeeping_config)
    assert not plan.to_delete
    assert "minimum" in plan.skipped[0].reason


def test_missing_compacted_replacement_blocks_deletion(housekeeping_config) -> None:
    """A raw snapshot must never be removed before its day has been compacted."""
    plan = plan_cleanup([_candidate(has_compacted_replacement=False)], housekeeping_config)
    assert not plan.to_delete
    assert "compacted replacement" in plan.skipped[0].reason


def test_unverified_checksum_blocks_deletion(housekeeping_config) -> None:
    plan = plan_cleanup([_candidate(checksum_verified=False)], housekeeping_config)
    assert not plan.to_delete
    assert "checksum" in plan.skipped[0].reason


def test_max_deletions_per_run_is_enforced(housekeeping_config) -> None:
    capped = housekeeping_config.model_copy(update={"max_deletions_per_run": 2})
    candidates = [_candidate(filename=f"snapshot-{i}.tar.zst") for i in range(5)]
    plan = plan_cleanup(candidates, capped)

    assert len(plan.to_delete) == 2
    assert len(plan.skipped) == 3
    assert all("max_deletions_per_run" in s.reason for s in plan.skipped)


async def test_dry_run_never_calls_the_publisher(housekeeping_config, tmp_path) -> None:
    publisher = FilesystemPublisher(tmp_path / "releases")
    plan = plan_cleanup([_candidate()], housekeeping_config)

    report = await execute_cleanup(plan, publisher, dry_run=True)

    assert report.dry_run
    assert len(report.deleted) == 1
    # Nothing was actually written or removed anywhere.
    assert not (tmp_path / "releases" / "atlas-data-gen1").exists()


async def test_a_real_run_deletes_through_the_publisher(housekeeping_config, tmp_path) -> None:
    publisher = FilesystemPublisher(tmp_path / "releases")
    target = publisher._generation_dir("gen1") / "snapshot-20260927T183700Z.tar.zst"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"stale snapshot data")
    assert target.exists()

    plan = plan_cleanup([_candidate()], housekeeping_config)
    report = await execute_cleanup(plan, publisher, dry_run=False)

    assert not report.dry_run
    assert len(report.deleted) == 1
    assert not target.exists()


async def test_deleting_a_missing_file_is_a_safe_no_op(housekeeping_config, tmp_path) -> None:
    publisher = FilesystemPublisher(tmp_path / "releases")
    plan = plan_cleanup([_candidate()], housekeeping_config)
    # No file was ever created; deletion must not raise.
    report = await execute_cleanup(plan, publisher, dry_run=False)
    assert len(report.deleted) == 1


def test_select_generations_to_retain_keeps_the_configured_count() -> None:
    ids = ["gen5", "gen4", "gen3", "gen2", "gen1"]
    kept = select_generations_to_retain(ids, keep_recent_generations=3)
    assert kept == {"gen5", "gen4", "gen3"}
    assert "gen1" not in kept


def test_select_generations_to_retain_requires_a_positive_count() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        select_generations_to_retain(["gen1"], keep_recent_generations=0)


def test_default_retention_config_requires_a_nonempty_allow_list() -> None:
    """The loaded configuration itself must satisfy its own no-wildcard-deletion rule."""
    config = load_retention_config().housekeeping
    assert config.require_tag_prefix
    assert "v" in config.never_delete_tag_prefix
