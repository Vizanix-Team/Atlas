"""Assembles a :class:`~vizanix_atlas.discovery.pipeline.Generation` into published files.

This is the one place that ties every table builder in :mod:`vizanix_atlas.storage.tables`
together and writes them to disk with :mod:`vizanix_atlas.storage.writer`, so
``atlas build-dataset`` and the publication tests describe the same dataset shape
rather than two that could quietly drift apart (see ``docs/STORAGE.md``, "Tables").
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from vizanix_atlas.core.config import CollectionConfig
from vizanix_atlas.discovery.pipeline import Generation
from vizanix_atlas.storage.tables import (
    build_adapter_health_table,
    build_asset_alias_table,
    build_asset_market_state_table,
    build_asset_table,
    build_instrument_table,
    build_quality_event_table,
    build_venue_asset_state_table,
    split_by_shard,
)
from vizanix_atlas.storage.writer import WrittenFile, write_parquet

#: Table name to builder, for every table that is not sharded. Order is insertion
#: order and controls the order files are written, which has no functional effect but
#: keeps repeated runs' file listings stable for humans reading a diff.
_UNSHARDED_TABLES: dict[str, Callable[[Sequence[Any]], pl.DataFrame]] = {
    "assets": build_asset_table,
    "asset_aliases": build_asset_alias_table,
    "instruments": build_instrument_table,
    "adapter_health": build_adapter_health_table,
    "quality_events": build_quality_event_table,
    "venue_asset_state": build_venue_asset_state_table,
}

#: Attribute on :class:`Generation` each unshared table builder consumes.
_UNSHARDED_SOURCE_ATTR = {
    "assets": "assets",
    "asset_aliases": "aliases",
    "instruments": "instruments",
    "adapter_health": "health",
    "quality_events": "quality_events",
    "venue_asset_state": "bundles",
}


@dataclass(frozen=True, slots=True)
class BuiltDataset:
    """Every file written for one generation, ready to hand to a :class:`Publisher`."""

    written_files: tuple[WrittenFile, ...]
    shard_indices: dict[str, int]


def build_dataset_files(
    generation: Generation, out_dir: Path, *, config: CollectionConfig
) -> BuiltDataset:
    """Write every table for ``generation`` to Parquet under ``out_dir``.

    Args:
        generation: The generation to write. Its tuples (``assets``, ``instruments``,
            ``bundles``, ...) are the sole source of truth for what gets written;
            this function never re-derives them.
        out_dir: Directory the Parquet files are written into, one file per table
            plus one file per asset-market-state shard.
        config: Collection policy, for the writer's size budgets and shard count.

    Returns:
        Every :class:`~vizanix_atlas.storage.writer.WrittenFile` produced, plus the
        shard-index-by-filename mapping the manifest needs.

    """
    written: list[WrittenFile] = []
    for table_name, builder in _UNSHARDED_TABLES.items():
        source = getattr(generation, _UNSHARDED_SOURCE_ATTR[table_name])
        frame = builder(source)
        written.append(
            write_parquet(
                frame, out_dir / f"{table_name}.parquet", table=table_name, config=config.publishing
            )
        )

    market_state = build_asset_market_state_table(
        generation.bundles, shard_count=config.publishing.shard_count
    )
    shard_indices: dict[str, int] = {}
    for index, shard_frame in split_by_shard(
        market_state, shard_count=config.publishing.shard_count
    ).items():
        filename = f"assets-{index:02d}.parquet"
        written.append(
            write_parquet(
                shard_frame,
                out_dir / filename,
                table="asset_market_state",
                config=config.publishing,
            )
        )
        shard_indices[filename] = index

    return BuiltDataset(written_files=tuple(written), shard_indices=shard_indices)
