"""Storage layer tests: flattening, table building, and Parquet writing.

These use small synthetic models rather than a live generation, so the suite never
depends on network access. The live-pipeline probe that exercises this layer against
real exchange data lives outside the test suite (see the engineering notes in
docs/OPERATIONS.md); these tests instead pin down the specific behaviours that
mattered when building it: mixed-type columns, deterministic sharding, and size
enforcement.
"""

from __future__ import annotations

import json

import polars as pl
import pytest

from vizanix_atlas.core.config import load_collection_config
from vizanix_atlas.core.identifiers import shard_for
from vizanix_atlas.models.asset import CanonicalAsset
from vizanix_atlas.models.enums import (
    ConversionMethod,
    ReferencePriceMethod,
)
from vizanix_atlas.models.quality import QuoteConversion
from vizanix_atlas.models.state import Dispersion, ReferencePrice
from vizanix_atlas.storage.flatten import flatten_model, flatten_models
from vizanix_atlas.storage.tables import _frame, build_asset_table, split_by_shard
from vizanix_atlas.storage.writer import sha256_file, write_json, write_parquet


def test_flatten_inlines_nested_models_with_prefixed_names() -> None:
    rp = ReferencePrice(
        value=100.5,
        method=ReferencePriceMethod.SPOT_WEIGHTED_MEDIAN,
        venue_count=3,
        included_venue_count=3,
    )
    row = flatten_model(rp)
    assert row["value"] == 100.5
    # Enums flatten to their value, not the member.
    assert row["method"] == "spot_weighted_median"
    # A dict field becomes a JSON string, not a struct column.
    assert row["excluded_reasons"] == "{}"


def test_flatten_serialises_sequences_of_models_as_json() -> None:
    conversion = QuoteConversion(
        from_asset_id="asset:evm:1:0xaa",
        rate=0.9997,
        method=ConversionMethod.DIRECT_OBSERVED,
        path=("asset:evm:1:0xaa", "asset:fiat:usd"),
    )
    row = flatten_model(conversion)
    # A tuple of scalars is preserved as a JSON array, not exploded into columns.
    assert json.loads(row["path"]) == ["asset:evm:1:0xaa", "asset:fiat:usd"]


def test_flatten_empty_sequence_is_an_empty_json_array() -> None:
    dispersion = Dispersion()
    row = flatten_model(dispersion)
    assert row["contributing_venue_count"] == 0


def test_flatten_preserves_none_as_none_not_a_sentinel() -> None:
    """A missing name must stay None; it must never become an empty string."""
    asset = CanonicalAsset(asset_id="asset:native:bitcoin:BTC", symbol="BTC", name=None)
    row = flatten_model(asset)
    assert row["name"] is None
    assert row["chain_slug"] is None


def test_flatten_models_preserves_order() -> None:
    assets = [CanonicalAsset(asset_id=f"asset:x:{i}", symbol=f"X{i}") for i in range(5)]
    rows = flatten_models(assets)
    assert [r["asset_id"] for r in rows] == [a.asset_id for a in assets]


def test_frame_handles_a_column_that_is_none_in_most_rows_and_a_string_later() -> None:
    """Regression test: polars' default schema inference samples only the first rows.

    Building the assets table from a live generation failed here: chain_slug is None
    for the great majority of unresolved assets and a string for the few resolved
    native-chain ones, and the default `infer_schema_length` (100) inferred the column
    as non-nullable from the leading None-only rows, then raised when a string finally
    appeared. `_frame` must scan every row before choosing a type.
    """
    rows = [{"asset_id": f"a{i}", "chain_slug": None} for i in range(150)]
    rows.append({"asset_id": "a150", "chain_slug": "bitcoin"})
    frame = _frame(rows, ("asset_id", "chain_slug"))
    assert frame.height == 151
    assert frame["chain_slug"][150] == "bitcoin"
    assert frame["chain_slug"][0] is None


def test_frame_adds_missing_declared_columns_as_null() -> None:
    """Every declared column must exist even if no row this run had a value for it."""
    frame = _frame([{"asset_id": "a"}], ("asset_id", "chain_id", "venue_count"))
    assert list(frame.columns) == ["asset_id", "chain_id", "venue_count"]
    assert frame["chain_id"][0] is None


def test_build_asset_table_has_a_fixed_column_order() -> None:
    from vizanix_atlas.storage.tables import ASSET_COLUMNS

    frame = build_asset_table(
        [CanonicalAsset(asset_id="asset:native:bitcoin:BTC", symbol="BTC", name="Bitcoin")]
    )
    assert tuple(frame.columns) == ASSET_COLUMNS


def test_sharding_is_deterministic_and_row_counts_reconcile() -> None:
    """The sum of shard row counts must equal the input, and shard assignment must
    match core.identifiers.shard_for exactly, since the SDK computes it the same way
    on read.
    """
    shard_count = 8
    asset_ids = [f"asset:native:x{i}:X{i}" for i in range(50)]
    frame = pl.DataFrame(
        {
            "asset_id": asset_ids,
            "shard_index": [shard_for(a, shard_count) for a in asset_ids],
        }
    )
    shards = split_by_shard(frame, shard_count=shard_count)
    assert sum(f.height for f in shards.values()) == len(asset_ids)
    for shard_index, shard_frame in shards.items():
        for asset_id in shard_frame["asset_id"]:
            assert shard_for(asset_id, shard_count) == shard_index
    # A shard with no assets is absent, not present as an empty file.
    assert all(f.height > 0 for f in shards.values())


def test_split_by_shard_requires_a_shard_index_column() -> None:
    with pytest.raises(ValueError, match="shard_index"):
        split_by_shard(pl.DataFrame({"asset_id": ["a"]}), shard_count=4)


def test_sha256_file_matches_a_known_digest(tmp_path) -> None:
    path = tmp_path / "sample.txt"
    path.write_bytes(b"vizanix atlas")
    import hashlib

    assert sha256_file(path) == hashlib.sha256(b"vizanix atlas").hexdigest()


def test_write_parquet_reports_size_and_row_count(tmp_path) -> None:
    config = load_collection_config().publishing
    frame = pl.DataFrame({"asset_id": ["a", "b"], "value": [1.0, 2.0]})
    written = write_parquet(frame, tmp_path / "t.parquet", table="t", config=config)

    assert written.row_count == 2
    assert written.table == "t"
    assert written.size_bytes == written.path.stat().st_size
    assert written.sha256 == sha256_file(written.path)
    # A round trip through DuckDB/polars must reproduce the same data.
    assert pl.read_parquet(written.path).height == 2


def test_write_parquet_refuses_a_file_over_its_size_budget(tmp_path) -> None:
    config = load_collection_config().publishing.model_copy(update={"max_single_asset_bytes": 16})
    frame = pl.DataFrame({"asset_id": [f"a{i}" for i in range(1000)]})

    from vizanix_atlas.core.errors import ValidationFailure

    with pytest.raises(ValidationFailure):
        write_parquet(frame, tmp_path / "oversized.parquet", table="t", config=config)


def test_write_json_round_trips(tmp_path) -> None:
    payload = json.dumps({"a": 1, "b": None})
    written = write_json(payload, tmp_path / "manifest.json")
    assert written.row_count is None
    assert written.table is None
    assert (tmp_path / "manifest.json").read_text(encoding="utf-8") == payload
    assert written.sha256 == sha256_file(tmp_path / "manifest.json")
