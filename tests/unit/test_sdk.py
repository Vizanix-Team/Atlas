"""SDK tests, exercised against a locally built and published dataset.

No network access is used anywhere: :class:`Atlas.from_local` reads straight from a
:class:`~vizanix_atlas.publishing.publisher.FilesystemPublisher` layout built by the
same helpers ``tests/unit/test_publishing.py`` uses, so these tests exercise the
genuine dataset shape rather than a hand-rolled stand-in for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.test_publishing import _permissive_config, build_sample_generation
from vizanix_atlas.core.errors import AssetNotFound, DatasetUnavailable
from vizanix_atlas.publishing.publish import publish_generation
from vizanix_atlas.publishing.publisher import FilesystemPublisher
from vizanix_atlas.sdk.client import Atlas
from vizanix_atlas.storage.dataset_builder import build_dataset_files


@pytest.fixture
def published_releases_dir(tmp_path: Path) -> Path:
    config = _permissive_config()
    generation = build_sample_generation(extra_asset=True)
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    built = build_dataset_files(generation, dataset_dir, config=config)
    releases_dir = tmp_path / "releases"
    publisher = FilesystemPublisher(releases_dir)

    import asyncio

    outcome = asyncio.run(
        publish_generation(
            generation,
            dataset_dir=dataset_dir,
            written_files=list(built.written_files),
            publisher=publisher,
            config=config,
            release_tag=f"atlas-data-{generation.generation_id}",
            shard_indices=built.shard_indices,
        )
    )
    assert outcome.published
    return releases_dir


def test_from_local_opens_the_latest_generation(published_releases_dir: Path) -> None:
    atlas = Atlas.from_local(published_releases_dir)
    assert atlas.manifest.asset_count == 2


def test_from_local_opens_a_generation_directory_directly(published_releases_dir: Path) -> None:
    atlas = Atlas.from_local(published_releases_dir)
    generation_dir = published_releases_dir / f"atlas-data-{atlas.generation_id}"
    reopened = Atlas.from_local(generation_dir)
    assert reopened.generation_id == atlas.generation_id


def test_from_local_rejects_an_unrelated_directory(tmp_path: Path) -> None:
    with pytest.raises(DatasetUnavailable):
        Atlas.from_local(tmp_path)


def test_asset_returns_the_matching_asset(published_releases_dir: Path) -> None:
    atlas = Atlas.from_local(published_releases_dir)
    btc = atlas.asset("BTC")
    assert btc.symbol == "BTC"
    assert btc.reference_price is not None
    assert btc.reference_price > 0


def test_asset_raises_for_an_unknown_symbol(published_releases_dir: Path) -> None:
    atlas = Atlas.from_local(published_releases_dir)
    with pytest.raises(AssetNotFound):
        atlas.asset("NOPE")


def test_asset_state_unflattens_into_a_nested_tree(published_releases_dir: Path) -> None:
    atlas = Atlas.from_local(published_releases_dir)
    state = atlas.asset("BTC").state()
    assert state.reference_price.value == atlas.asset("BTC").reference_price


def test_search_finds_by_symbol_and_name(published_releases_dir: Path) -> None:
    atlas = Atlas.from_local(published_releases_dir)
    by_symbol = atlas.search("BTC")
    by_name = atlas.search("bitcoin")
    assert {m["asset_id"] for m in by_symbol} == {m["asset_id"] for m in by_name}
    assert by_symbol[0]["symbol"] == "BTC"


def test_market_summarises_every_asset(published_releases_dir: Path) -> None:
    atlas = Atlas.from_local(published_releases_dir)
    market = atlas.market()
    assert market.asset_count == 2
    assert market.qualified_asset_count(min_venue_count=1) >= 1


def test_query_runs_mql_against_the_downloaded_shards(published_releases_dir: Path) -> None:
    atlas = Atlas.from_local(published_releases_dir)
    result = atlas.query("SELECT asset, venue_count FROM market ORDER BY asset")
    assert result["asset"].to_list() == ["BTC", "ETH"]
