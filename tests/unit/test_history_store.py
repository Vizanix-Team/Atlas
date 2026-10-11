"""History store tests.

The store is what the windowed metrics read, so what matters is that it never invents a
point, never leaks the future, and stays bounded. Generations come from the same sample
builder the publishing tests use, so the rows are real market states.
"""

from __future__ import annotations

import dataclasses

import polars as pl
import pytest

from vizanix_atlas.core.config import HistoryStoreConfig
from vizanix_atlas.core.errors import SchemaMismatch
from vizanix_atlas.storage.history import SCHEMA, HistoryStore
from vizanix_atlas.util.sample import build_sample_generation

HOUR = 3_600_000
SLOT = 15 * 60_000
CONFIG = HistoryStoreConfig(max_assets=600, retention_hours=744, grace_hours=24)


def _at(generation, when: int):
    """The same generation attributed to another snapshot time."""
    return dataclasses.replace(generation, snapshot_effective_time=when)


def _frame(rows: list[tuple]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=SCHEMA, orient="row")


def test_an_empty_store_has_no_history() -> None:
    store = HistoryStore.empty()
    assert store.row_count == 0 and store.asset_count == 0
    assert store.last_observed_at is None
    assert store.view(as_of=10**13) == {}


def test_extending_with_a_generation_records_each_priced_asset() -> None:
    generation = build_sample_generation()
    store = HistoryStore.empty().extended_with(generation, config=CONFIG)

    priced = [b for b in generation.bundles if b.state.reference_price.value is not None]
    assert priced, "the sample generation must contain priced assets"
    assert store.row_count == len(priced)
    assert store.last_observed_at == generation.snapshot_effective_time
    row = store.frame.filter(pl.col("asset_id") == priced[0].state.asset_id).row(0, named=True)
    assert row["reference_price"] == priced[0].state.reference_price.value


def test_the_store_round_trips_through_bytes() -> None:
    generation = build_sample_generation()
    store = HistoryStore.empty().extended_with(generation, config=CONFIG)

    restored = HistoryStore.from_bytes(store.to_bytes())
    assert restored.frame.sort("asset_id").equals(store.frame.sort("asset_id"))
    assert HistoryStore.from_bytes(store.to_bytes()).to_bytes() == store.to_bytes()


def test_a_corrupt_or_foreign_file_is_refused_not_treated_as_empty() -> None:
    with pytest.raises(SchemaMismatch):
        HistoryStore.from_bytes(b"not parquet at all")

    buffer = pl.DataFrame({"unrelated": [1, 2, 3]}).write_parquet
    import io

    out = io.BytesIO()
    buffer(out)
    with pytest.raises(SchemaMismatch):
        HistoryStore.from_bytes(out.getvalue())


def test_re_running_a_slot_replaces_rather_than_duplicates() -> None:
    generation = build_sample_generation()
    once = HistoryStore.empty().extended_with(generation, config=CONFIG)
    twice = once.extended_with(generation, config=CONFIG)
    assert twice.row_count == once.row_count


def test_the_view_never_includes_the_snapshot_being_built() -> None:
    generation = build_sample_generation()
    t0 = generation.snapshot_effective_time
    store = HistoryStore.empty()
    for step in range(4):
        store = store.extended_with(_at(generation, t0 + step * SLOT), config=CONFIG)

    asset_id = generation.bundles[0].state.asset_id
    seen = store.view(as_of=t0 + 3 * SLOT)[asset_id].prices.points
    assert [p.observed_at for p in seen] == [t0, t0 + SLOT, t0 + 2 * SLOT]


def test_price_series_is_oldest_first_and_positive_only() -> None:
    generation = build_sample_generation()
    t0 = generation.snapshot_effective_time
    store = HistoryStore.empty()
    for step in (2, 0, 1):  # appended out of order
        store = store.extended_with(_at(generation, t0 + step * SLOT), config=CONFIG)

    asset_id = generation.bundles[0].state.asset_id
    points = store.view(as_of=t0 + 10 * SLOT)[asset_id].prices.points
    assert [p.observed_at for p in points] == sorted(p.observed_at for p in points)
    assert all(p.reference_price > 0 for p in points)


def test_rows_older_than_the_retention_are_dropped() -> None:
    generation = build_sample_generation()
    t0 = generation.snapshot_effective_time
    store = HistoryStore.empty().extended_with(_at(generation, t0), config=CONFIG)
    later = t0 + (CONFIG.retention_hours + 1) * HOUR
    store = store.extended_with(_at(generation, later), config=CONFIG)

    times = set(store.frame["observed_at"].to_list())
    assert t0 not in times and later in times


def test_only_the_most_liquid_assets_are_tracked() -> None:
    generation = build_sample_generation()
    narrow = HistoryStoreConfig(max_assets=1, retention_hours=744, grace_hours=0)
    store = HistoryStore.empty().extended_with(generation, config=narrow)
    assert store.asset_count == 1


def test_an_asset_that_drops_out_of_the_ranking_keeps_its_history_for_the_grace_period() -> None:
    t0 = 1_800_000_000_000
    old = _frame([("asset:a", t0 - SLOT, 10.0, None, None, None, 5.0)])
    generation = build_sample_generation()
    # The sample's assets rank first, so "asset:a" is not in the new top-1 ranking.
    narrow = HistoryStoreConfig(max_assets=1, retention_hours=744, grace_hours=24)
    store = HistoryStore(old).extended_with(_at(generation, t0), config=narrow)
    assert "asset:a" in set(store.frame["asset_id"].to_list())

    much_later = t0 + 30 * HOUR
    store = store.extended_with(_at(generation, much_later), config=narrow)
    assert "asset:a" not in set(store.frame["asset_id"].to_list())


def test_funding_and_basis_history_cover_the_last_thirty_days_and_skip_nulls() -> None:
    as_of = 1_800_000_000_000
    rows = [
        ("asset:x", as_of - 40 * 24 * HOUR, 100.0, 0.0001, 5.0, None, None),  # too old
        ("asset:x", as_of - 2 * SLOT, 101.0, 0.0002, None, None, None),
        ("asset:x", as_of - SLOT, 102.0, None, 7.0, None, None),
    ]
    history = HistoryStore(_frame(rows)).view(as_of=as_of)["asset:x"]
    assert history.funding_8h == (0.0002,)
    assert history.basis_bps == (7.0,)


def test_open_interest_previous_requires_a_recent_snapshot() -> None:
    as_of = 1_800_000_000_000
    fresh = HistoryStore(_frame([("a", as_of - SLOT, 1.0, None, None, 500.0, None)]))
    stale = HistoryStore(_frame([("a", as_of - 5 * HOUR, 1.0, None, None, 500.0, None)]))
    assert fresh.view(as_of=as_of)["a"].open_interest_previous_usd == 500.0
    assert stale.view(as_of=as_of)["a"].open_interest_previous_usd is None


def test_open_interest_24h_ago_uses_the_nearest_snapshot_within_tolerance() -> None:
    as_of = 1_800_000_000_000
    target = as_of - 24 * HOUR
    rows = [
        ("a", target - SLOT, 1.0, None, None, 400.0, None),
        ("a", target + 5 * SLOT, 1.0, None, None, 999.0, None),  # outside the tolerance window
        ("a", as_of - SLOT, 1.0, None, None, 600.0, None),
    ]
    assert HistoryStore(_frame(rows)).view(as_of=as_of)["a"].open_interest_24h_ago_usd == 400.0
    none_near = [("a", as_of - 12 * HOUR, 1.0, None, None, 123.0, None)]
    assert HistoryStore(_frame(none_near)).view(as_of=as_of)["a"].open_interest_24h_ago_usd is None


def test_volume_baseline_needs_enough_snapshots_to_be_a_baseline() -> None:
    as_of = 1_800_000_000_000
    few = [("a", as_of - i * SLOT, 1.0, None, None, None, 10.0) for i in range(1, 6)]
    many = [("a", as_of - i * SLOT, 1.0, None, None, None, float(i)) for i in range(1, 40)]
    assert HistoryStore(_frame(few)).view(as_of=as_of)["a"].volume_baseline_usd is None
    baseline = HistoryStore(_frame(many)).view(as_of=as_of)["a"].volume_baseline_usd
    assert baseline is not None and 40 > baseline > 0
