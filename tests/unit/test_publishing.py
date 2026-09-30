"""Publication pipeline tests.

These exercise the full path from a built :class:`Generation` through Parquet files,
a manifest, and the atomic-like publish sequence, against
:class:`~vizanix_atlas.publishing.publisher.FilesystemPublisher`. No network access is
used anywhere: the generation itself is built from small, hand-constructed collection
results rather than live exchange data, so the suite never depends on a venue being
reachable.

The property under test throughout is the one ``docs/STORAGE.md`` names as the whole
point of the publish sequence: a failure at any point before the last write must leave
the previous generation as canonical, never a partially-published one.
"""

from __future__ import annotations

import json
from pathlib import Path

from vizanix_atlas.core.atlas_time import to_epoch_ms, utc_now
from vizanix_atlas.core.config import load_collection_config
from vizanix_atlas.discovery.pipeline import Generation, build_generation
from vizanix_atlas.models.enums import PriceSource
from vizanix_atlas.models.observations import (
    AdapterHealth,
    CollectionResult,
    ObservationTiming,
    RawFxObservation,
    RawInstrument,
    RawTicker,
)
from vizanix_atlas.publishing.publish import publish_generation
from vizanix_atlas.publishing.publisher import FilesystemPublisher
from vizanix_atlas.storage.dataset_builder import build_dataset_files
from vizanix_atlas.storage.writer import WrittenFile

T0 = to_epoch_ms(utc_now())


def _timing() -> ObservationTiming:
    return ObservationTiming(collector_receive_time=T0, exchange_event_time=T0)


def _health(venue: str, *, tickers: int, instruments: int) -> AdapterHealth:
    return AdapterHealth(
        venue_slug=venue,
        status="success",
        duration_ms=100,
        requests_attempted=2,
        requests_successful=2,
        instrument_count=instruments,
        ticker_count=tickers,
    )


def build_sample_generation(
    *,
    price_multiplier: float = 1.0,
    extra_asset: bool = False,
    effective_time: int | None = None,
) -> Generation:
    """Build a small, real Generation from hand-constructed collection results.

    Runs the actual identity, normalisation and analytics pipeline end to end, just
    against fabricated rather than live-fetched observations, so publishing tests
    exercise the real data shape without any network dependency.
    """
    config = load_collection_config()
    instruments = [
        RawInstrument(
            venue_slug="okx",
            symbol_native="BTC-USDT",
            instrument_class="spot",
            instrument_type="spot",
            base_symbol_native="BTC",
            quote_symbol_native="USDT",
        ),
        RawInstrument(
            venue_slug="coinbase",
            symbol_native="BTC-USD",
            instrument_class="spot",
            instrument_type="spot",
            base_symbol_native="BTC",
            quote_symbol_native="USD",
        ),
    ]
    price = 84_400.0 * price_multiplier
    okx_tickers = [
        RawTicker(
            venue_slug="okx",
            symbol_native="BTC-USDT",
            timing=_timing(),
            last_price=price,
            bid_price=price - 0.5,
            ask_price=price + 0.5,
            base_volume_24h=1_000.0,
            quote_volume_24h=8.44e7,
        )
    ]
    coinbase_tickers = [
        RawTicker(
            venue_slug="coinbase",
            symbol_native="BTC-USD",
            timing=_timing(),
            last_price=price + 20.0,
            base_volume_24h=500.0,
        )
    ]
    fx = [
        RawFxObservation(
            venue_slug="coinbase",
            symbol_native="USDT-USD",
            timing=_timing(),
            from_symbol_native="USDT",
            to_symbol_native="USD",
            rate=0.9997,
            source_price=PriceSource.MID,
        )
    ]
    if extra_asset:
        instruments.append(
            RawInstrument(
                venue_slug="okx",
                symbol_native="ETH-USDT",
                instrument_class="spot",
                instrument_type="spot",
                base_symbol_native="ETH",
                quote_symbol_native="USDT",
            )
        )
        okx_tickers.append(
            RawTicker(
                venue_slug="okx",
                symbol_native="ETH-USDT",
                timing=_timing(),
                last_price=2_700.0,
                base_volume_24h=5_000.0,
                quote_volume_24h=1.35e7,
            )
        )

    results = [
        CollectionResult(
            venue_slug="okx",
            run_id="test",
            collection_started_at=T0,
            collection_finished_at=T0,
            health=_health(
                "okx",
                tickers=len(okx_tickers),
                instruments=len([i for i in instruments if i.venue_slug == "okx"]),
            ),
            instruments=tuple(i for i in instruments if i.venue_slug == "okx"),
            tickers=tuple(okx_tickers),
        ),
        CollectionResult(
            venue_slug="coinbase",
            run_id="test",
            collection_started_at=T0,
            collection_finished_at=T0,
            health=_health("coinbase", tickers=len(coinbase_tickers), instruments=1),
            instruments=tuple(i for i in instruments if i.venue_slug == "coinbase"),
            tickers=tuple(coinbase_tickers),
            fx_observations=tuple(fx),
        ),
    ]
    return build_generation(results, config=config, snapshot_effective_time=effective_time)


def write_generation_files(generation: Generation, out_dir: Path, *, config) -> list[WrittenFile]:
    """Write every table for a generation, via the real production dataset builder."""
    built = build_dataset_files(generation, out_dir, config=config)
    return list(built.written_files), built.shard_indices


def _permissive_config():
    """A config whose anomaly guards do not reject a two-venue sample generation."""
    config = load_collection_config()
    guards = config.publishing.anomaly_guards.model_copy(
        update={"min_successful_venues": 1, "min_successful_venue_ratio": 0.01}
    )
    return config.model_copy(
        update={"publishing": config.publishing.model_copy(update={"anomaly_guards": guards})}
    )


async def test_first_publish_becomes_latest(tmp_path: Path) -> None:
    config = _permissive_config()
    generation = build_sample_generation()
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    written, shard_indices = write_generation_files(generation, dataset_dir, config=config)
    publisher = FilesystemPublisher(tmp_path / "releases")

    outcome = await publish_generation(
        generation,
        dataset_dir=dataset_dir,
        written_files=written,
        publisher=publisher,
        config=config,
        release_tag=f"atlas-data-{generation.generation_id}",
        shard_indices=shard_indices,
    )

    assert outcome.published
    assert outcome.is_new_latest
    assert outcome.validation.passed

    pointer = await publisher.read_latest_pointer()
    assert pointer is not None
    assert pointer.generation_id == generation.generation_id
    assert pointer.previous_generation_id is None

    manifest = await publisher.read_generation_manifest(generation.generation_id)
    assert manifest is not None
    assert manifest.published_at is not None
    assert manifest.asset_count == generation.asset_count
    assert len(manifest.files) == len(written)


async def test_manifest_and_pointer_round_trip_through_json(tmp_path: Path) -> None:
    """Every field must survive a real JSON round trip, including enums and tuples."""
    config = _permissive_config()
    generation = build_sample_generation()
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    written, shard_indices = write_generation_files(generation, dataset_dir, config=config)
    publisher = FilesystemPublisher(tmp_path / "releases")

    outcome = await publish_generation(
        generation,
        dataset_dir=dataset_dir,
        written_files=written,
        publisher=publisher,
        config=config,
        release_tag="atlas-data-test",
        shard_indices=shard_indices,
    )
    assert outcome.published

    # Read the raw JSON directly, bypassing the publisher, to prove the file on disk
    # is genuinely well-formed JSON matching the schema, not just round-trippable
    # through the same Pydantic model that wrote it.
    manifest_path = (
        tmp_path / "releases" / f"atlas-data-{generation.generation_id}" / "manifest.json"
    )
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert raw["generation_id"] == generation.generation_id
    assert raw["venues"]["successful"] == 2
    assert isinstance(raw["files"], list) and raw["files"]

    pointer_path = tmp_path / "releases" / "data-latest" / "latest.json"
    pointer_raw = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert pointer_raw["generation_id"] == generation.generation_id
    assert len(pointer_raw["manifest_sha256"]) == 64


async def test_second_generation_advances_latest_and_records_the_predecessor(
    tmp_path: Path,
) -> None:
    config = _permissive_config()
    publisher = FilesystemPublisher(tmp_path / "releases")

    first = build_sample_generation()
    first_dir = tmp_path / "gen1"
    first_dir.mkdir()
    written1, shards1 = write_generation_files(first, first_dir, config=config)
    outcome1 = await publish_generation(
        first,
        dataset_dir=first_dir,
        written_files=written1,
        publisher=publisher,
        config=config,
        release_tag="atlas-data-1",
        shard_indices=shards1,
    )
    assert outcome1.published

    second = build_sample_generation(price_multiplier=1.001, extra_asset=True)
    second_dir = tmp_path / "gen2"
    second_dir.mkdir()
    written2, shards2 = write_generation_files(second, second_dir, config=config)
    outcome2 = await publish_generation(
        second,
        dataset_dir=second_dir,
        written_files=written2,
        publisher=publisher,
        config=config,
        release_tag="atlas-data-2",
        shard_indices=shards2,
    )
    assert outcome2.published
    assert outcome2.manifest.previous_generation_id == first.generation_id
    assert outcome2.latest_pointer.previous_generation_id == first.generation_id

    pointer = await publisher.read_latest_pointer()
    assert pointer is not None
    assert pointer.generation_id == second.generation_id

    # Both generations remain independently readable; the first was never deleted.
    assert await publisher.read_generation_manifest(first.generation_id) is not None
    ids = await publisher.list_generation_ids()
    assert set(ids) == {first.generation_id, second.generation_id}


async def test_a_generation_with_too_few_successful_venues_never_becomes_latest(
    tmp_path: Path,
) -> None:
    """The default anomaly guard requires at least 4 successful venues.

    A two-venue sample generation must fail the gate under the real, unmodified
    configuration, and the previous generation (here: none at all) must not be
    replaced by a bad first publish either.
    """
    config = load_collection_config()  # the real, strict guards
    generation = build_sample_generation()
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    written, shard_indices = write_generation_files(generation, dataset_dir, config=config)
    publisher = FilesystemPublisher(tmp_path / "releases")

    outcome = await publish_generation(
        generation,
        dataset_dir=dataset_dir,
        written_files=written,
        publisher=publisher,
        config=config,
        release_tag="atlas-data-test",
        shard_indices=shard_indices,
    )

    assert not outcome.published
    assert not outcome.validation.passed
    assert "anomaly" in outcome.validation.summary()
    assert await publisher.read_latest_pointer() is None


async def test_a_severe_asset_count_drop_is_refused_and_the_previous_generation_stands(
    tmp_path: Path,
) -> None:
    """A generation that collapses versus its predecessor must not replace it."""
    config = _permissive_config()
    publisher = FilesystemPublisher(tmp_path / "releases")

    first = build_sample_generation(extra_asset=True)  # 2 assets (BTC, ETH)
    first_dir = tmp_path / "gen1"
    first_dir.mkdir()
    written1, shards1 = write_generation_files(first, first_dir, config=config)
    outcome1 = await publish_generation(
        first,
        dataset_dir=first_dir,
        written_files=written1,
        publisher=publisher,
        config=config,
        release_tag="atlas-data-1",
        shard_indices=shards1,
    )
    assert outcome1.published
    assert first.asset_count == 2

    # A second generation with only BTC: a 50% drop, at the configured ceiling but not
    # exceeding it, so tighten the guard for this test to make the drop unambiguous.
    strict = config.model_copy(
        update={
            "publishing": config.publishing.model_copy(
                update={
                    "anomaly_guards": config.publishing.anomaly_guards.model_copy(
                        update={"max_asset_count_drop_ratio": 0.1}
                    )
                }
            )
        }
    )
    second = build_sample_generation()  # 1 asset (BTC only)
    second_dir = tmp_path / "gen2"
    second_dir.mkdir()
    written2, shards2 = write_generation_files(second, second_dir, config=strict)
    outcome2 = await publish_generation(
        second,
        dataset_dir=second_dir,
        written_files=written2,
        publisher=publisher,
        config=strict,
        release_tag="atlas-data-2",
        shard_indices=shards2,
    )

    assert not outcome2.published
    assert "asset count dropped" in outcome2.validation.summary()

    # The previous generation is still canonical.
    pointer = await publisher.read_latest_pointer()
    assert pointer is not None
    assert pointer.generation_id == first.generation_id


async def test_a_corrupted_file_on_disk_fails_the_checksum_gate_before_publishing(
    tmp_path: Path,
) -> None:
    """Simulates the disk state after a write was interrupted or silently corrupted.

    The manifest is built from what write_parquet *reported*; if the bytes on disk no
    longer match that report by the time publication runs, the checksum check inside
    the validity gate must catch it before anything is uploaded.
    """
    config = _permissive_config()
    generation = build_sample_generation()
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    written, shard_indices = write_generation_files(generation, dataset_dir, config=config)
    publisher = FilesystemPublisher(tmp_path / "releases")

    # Corrupt one file after it was hashed but before publication runs.
    corrupted = dataset_dir / written[0].filename
    corrupted.write_bytes(b"not the parquet file the manifest describes")

    outcome = await publish_generation(
        generation,
        dataset_dir=dataset_dir,
        written_files=written,
        publisher=publisher,
        config=config,
        release_tag="atlas-data-test",
        shard_indices=shard_indices,
    )

    assert not outcome.published
    assert not outcome.validation.passed
    assert "hash mismatch" in outcome.validation.summary()
    assert await publisher.read_latest_pointer() is None


async def test_atlas_verify_style_checksum_check_catches_post_publish_tampering(
    tmp_path: Path,
) -> None:
    """What `atlas verify` runs: recomputing every file's hash from the manifest.

    Simulates a generation directory that was corrupted (or an incomplete transfer)
    after publication, which the checksum-only check (independent of the full gate)
    must still catch.
    """
    from vizanix_atlas.publishing.validator import validate_checksums

    config = _permissive_config()
    generation = build_sample_generation()
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    written, shard_indices = write_generation_files(generation, dataset_dir, config=config)

    from vizanix_atlas.publishing.manifest import build_file_entries, build_manifest

    manifest = build_manifest(
        generation,
        build_file_entries(
            written, generation_id=generation.generation_id, shard_indices=shard_indices
        ),
        shard_count=config.publishing.shard_count,
    )

    clean = validate_checksums(manifest, dataset_dir)
    assert clean.passed

    (dataset_dir / written[0].filename).write_bytes(b"tampered after the fact")
    dirty = validate_checksums(manifest, dataset_dir)
    assert not dirty.passed
    assert "hash mismatch" in dirty.summary()


async def test_a_missing_file_fails_checksum_validation() -> None:
    from vizanix_atlas.models.manifest import (
        BuildProvenance,
        FileEntry,
        GenerationManifest,
        VenueOutcome,
    )
    from vizanix_atlas.publishing.validator import validate_checksums

    manifest = GenerationManifest(
        dataset_format_version="1.0.0",
        schema_version="1.0.0",
        methodology_version="1.0.0",
        software_version="0.1.0",
        generation_id="g",
        slot_label="20260101T000000Z",
        collection_started_at="2026-01-01T00:00:00Z",
        collection_finished_at="2026-01-01T00:00:00Z",
        snapshot_effective_time="2026-01-01T00:00:00Z",
        venues=VenueOutcome(),
        files=(
            FileEntry(
                filename="missing.parquet", size_bytes=10, sha256="a" * 64, generation_id="g"
            ),
        ),
        build=BuildProvenance(software_version="0.1.0"),
    )
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        report = validate_checksums(manifest, Path(tmp))
    assert not report.passed
    assert "missing on disk" in report.summary()
