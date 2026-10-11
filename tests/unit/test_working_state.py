"""Working-state tests: history and the order-book plan carried between runs.

The rule these tests protect: neither may fail a run, and the history is never overwritten
from a read that failed (that would erase weeks of accumulated series).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from vizanix_atlas.core.config import HistoryStoreConfig
from vizanix_atlas.core.errors import PublicationFailure
from vizanix_atlas.discovery.books_plan import PLAN_FILENAME, BooksPlan
from vizanix_atlas.publishing.publisher import FilesystemPublisher
from vizanix_atlas.publishing.working_state import (
    load_history,
    publish_books_plan,
    save_history,
)
from vizanix_atlas.storage.history import STORE_SLOTS, HistoryStore
from vizanix_atlas.util.sample import build_sample_generation

CONFIG = HistoryStoreConfig()
SLOT = 15 * 60_000


def _generation(step: int):
    base = build_sample_generation()
    return dataclasses.replace(
        base, snapshot_effective_time=base.snapshot_effective_time + step * SLOT
    )


async def test_a_first_run_starts_with_an_empty_trustworthy_history(tmp_path: Path) -> None:
    loaded = await load_history(FilesystemPublisher(tmp_path))
    assert loaded.store.row_count == 0
    assert loaded.source_slot is None
    assert loaded.trustworthy


async def test_history_accumulates_across_runs_and_alternates_slots(tmp_path: Path) -> None:
    publisher = FilesystemPublisher(tmp_path)
    slots_used: list[str | None] = []
    for step in range(3):
        loaded = await load_history(publisher)
        assert await save_history(publisher, loaded, _generation(step), config=CONFIG)
        slots_used.append((await load_history(publisher)).source_slot)

    final = await load_history(publisher)
    assert final.store.asset_count > 0
    times = sorted(set(final.store.frame["observed_at"].to_list()))
    assert len(times) == 3, "one snapshot per run is kept"
    # The newest state alternates between the two slots, so one is always intact.
    assert slots_used[0] != slots_used[1] and slots_used[1] != slots_used[2]


async def test_a_corrupt_slot_is_ignored_and_the_other_one_is_used(tmp_path: Path) -> None:
    publisher = FilesystemPublisher(tmp_path)
    await save_history(publisher, await load_history(publisher), _generation(0), config=CONFIG)
    good = (await load_history(publisher)).source_slot
    other = next(s for s in STORE_SLOTS if s != good)
    await publisher.publish_data_latest_asset(other, b"garbage")

    loaded = await load_history(publisher)
    assert loaded.source_slot == good
    assert loaded.store.row_count > 0
    assert loaded.trustworthy


class _FailingReads:
    """A publisher whose reads fail, as a flaky network would."""

    async def read_data_latest_asset(self, filename: str) -> bytes | None:
        raise PublicationFailure("could not look up release", status=502)

    async def publish_data_latest_asset(self, filename: str, content: bytes, **_: object) -> None:
        raise AssertionError("must never write after a failed read")


async def test_a_failed_read_means_no_history_and_no_save() -> None:
    publisher = _FailingReads()
    loaded = await load_history(publisher)
    assert not loaded.trustworthy
    assert loaded.store.row_count == 0
    # Saving would overwrite weeks of history with a store rebuilt from nothing.
    assert await save_history(publisher, loaded, _generation(0), config=CONFIG) is False


async def test_a_publisher_without_auxiliary_storage_is_tolerated() -> None:
    class Bare:
        pass

    loaded = await load_history(Bare())
    assert not loaded.trustworthy
    assert await save_history(Bare(), loaded, _generation(0), config=CONFIG) is False
    plan = BooksPlan(generation_id="g", generated_at=1, depth=100)
    assert await publish_books_plan(Bare(), plan) is False


async def test_a_failed_write_does_not_raise(tmp_path: Path) -> None:
    class FailingWrites(FilesystemPublisher):
        async def publish_data_latest_asset(
            self, filename: str, content: bytes, **_: object
        ) -> None:
            raise PublicationFailure("asset upload failed", status=500)

    publisher = FailingWrites(tmp_path)
    loaded = await load_history(publisher)
    assert await save_history(publisher, loaded, _generation(0), config=CONFIG) is False
    plan = BooksPlan(generation_id="g", generated_at=1, depth=100)
    assert await publish_books_plan(publisher, plan) is False


async def test_the_books_plan_is_published_next_to_the_pointer(tmp_path: Path) -> None:
    publisher = FilesystemPublisher(tmp_path)
    plan = BooksPlan(generation_id="g1", generated_at=5, depth=50, venues={"okx": ("BTC-USDT",)})
    assert await publish_books_plan(publisher, plan)

    stored = await publisher.read_data_latest_asset(PLAN_FILENAME)
    assert stored is not None
    assert BooksPlan.model_validate_json(stored) == plan


@pytest.mark.parametrize("step", [0, 1])
def test_the_sample_generation_has_a_history_to_store(step: int) -> None:
    assert HistoryStore.empty().extended_with(_generation(step), config=CONFIG).row_count > 0
