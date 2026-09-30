"""The ``Atlas`` entry point.

``Atlas.latest()`` is the ordinary way to start: it downloads (and caches) the
current published generation's manifest, then resolves individual files - a single
asset's shard, or every shard for a market-wide query - only as they are actually
needed. ``Atlas.from_local()`` is for a dataset already on disk (a local
``atlas build-dataset`` run, or a checked-out release), and never touches the
network.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from vizanix_atlas.core.errors import AmbiguousAsset, AssetNotFound, DatasetUnavailable
from vizanix_atlas.models.manifest import GenerationManifest, LatestPointer
from vizanix_atlas.mql.engine import run_query
from vizanix_atlas.publishing.publisher import MANIFEST_FILENAME
from vizanix_atlas.sdk.cache import DatasetCache, default_cache_dir
from vizanix_atlas.sdk.dataset import Dataset
from vizanix_atlas.sdk.remote import ReleaseDownloader
from vizanix_atlas.sdk.views import AssetView, MarketView

_ASSETS_TABLE_FILENAME = "assets.parquet"


class Atlas:
    """A handle onto one dataset generation."""

    def __init__(self, dataset: Dataset) -> None:
        self._dataset = dataset

    # -- construction --------------------------------------------------------

    @classmethod
    def from_local(cls, path: Path | str) -> Atlas:
        """Open a dataset already on disk. Never touches the network.

        Args:
            path: Either a generation directory (containing ``manifest.json``
                directly), or a :class:`~vizanix_atlas.publishing.publisher.FilesystemPublisher`
                root (containing ``data-latest/latest.json`` and one
                ``atlas-data-<id>/`` directory per generation), in which case the
                pointed-to generation is opened.

        """
        path = Path(path)
        manifest_path = path / MANIFEST_FILENAME
        if manifest_path.is_file():
            manifest = GenerationManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            return cls(Dataset(manifest=manifest, root=path))

        pointer_path = path / "data-latest" / "latest.json"
        if pointer_path.is_file():
            pointer = LatestPointer.model_validate_json(pointer_path.read_text(encoding="utf-8"))
            generation_dir = path / f"atlas-data-{pointer.generation_id}"
            manifest = GenerationManifest.model_validate_json(
                (generation_dir / MANIFEST_FILENAME).read_text(encoding="utf-8")
            )
            return cls(Dataset(manifest=manifest, root=generation_dir))

        raise DatasetUnavailable(
            "no manifest.json or data-latest/latest.json found at this path", path=str(path)
        )

    @classmethod
    def latest(
        cls,
        *,
        cache: bool = True,
        cache_dir: Path | None = None,
        owner: str | None = None,
        repo: str | None = None,
    ) -> Atlas:
        """Open the current published generation, downloading it as needed.

        Args:
            cache: Whether to reuse and populate a local cache. When ``False``, files
                are downloaded to a temporary location for this call only.
            cache_dir: Overrides the default cache location (``$ATLAS_CACHE_DIR`` or
                ``~/.cache/vizanix-atlas``).
            owner: The GitHub organization the dataset is published under.
            repo: The GitHub repository the dataset is published under.

        """
        downloader = ReleaseDownloader(owner=owner, repo=repo)
        pointer = LatestPointer.model_validate_json(downloader.fetch_latest_pointer())
        return cls.from_generation(
            pointer.generation_id,
            cache=cache,
            cache_dir=cache_dir,
            owner=owner,
            repo=repo,
            _downloader=downloader,
        )

    @classmethod
    def from_generation(
        cls,
        generation_id: str,
        *,
        cache: bool = True,
        cache_dir: Path | None = None,
        owner: str | None = None,
        repo: str | None = None,
        _downloader: ReleaseDownloader | None = None,
    ) -> Atlas:
        """Open a specific, named generation by ID, downloading it as needed."""
        downloader = _downloader or ReleaseDownloader(owner=owner, repo=repo)
        manifest = GenerationManifest.model_validate_json(downloader.fetch_manifest(generation_id))
        root = (
            DatasetCache(cache_dir or default_cache_dir()).generation_dir(generation_id)
            if cache
            else Path.cwd() / ".atlas-tmp" / generation_id
        )
        return cls(Dataset(manifest=manifest, root=root, downloader=downloader))

    # -- manifest -------------------------------------------------------------

    @property
    def dataset(self) -> Dataset:
        """The underlying lazily-resolved dataset."""
        return self._dataset

    @property
    def manifest(self) -> GenerationManifest:
        """The open generation's manifest."""
        return self._dataset.manifest

    @property
    def generation_id(self) -> str:
        """The open generation's immutable identifier."""
        return self._dataset.manifest.generation_id

    # -- asset lookup -----------------------------------------------------------

    def _assets_frame(self) -> pl.DataFrame:
        path = self._dataset.ensure_file(_ASSETS_TABLE_FILENAME)
        return pl.read_parquet(path)

    def search(self, text: str) -> list[dict[str, object]]:
        """Return every canonical asset whose symbol or name matches ``text``.

        Discovery, not identity: a ticker is not a stable identifier, and this never
        picks a single "best" match on the caller's behalf (see
        ``docs/ASSET_RESOLUTION.md``).
        """
        frame = self._assets_frame()
        needle = text.strip().lower()
        matches = frame.filter(
            pl.col("symbol").str.to_lowercase().str.contains(needle, literal=True)
            | pl.col("name").fill_null("").str.to_lowercase().str.contains(needle, literal=True)
        )
        return matches.select("asset_id", "symbol", "name", "resolution_state").to_dicts()

    def asset(self, symbol_or_id: str) -> AssetView:
        """Return one asset's published market state.

        Raises:
            AssetNotFound: If nothing matches ``symbol_or_id``.
            AmbiguousAsset: If more than one distinct canonical asset shares this
                ticker. Atlas never guesses among them (see
                ``docs/ASSET_RESOLUTION.md``, "never merge on ticker alone").

        """
        frame = self._assets_frame()
        by_id = frame.filter(pl.col("asset_id") == symbol_or_id)
        if by_id.height == 1:
            asset_id = by_id["asset_id"][0]
        else:
            by_symbol = frame.filter(pl.col("symbol") == symbol_or_id)
            distinct_ids = by_symbol["asset_id"].unique().to_list()
            if not distinct_ids:
                raise AssetNotFound(
                    "no canonical asset matched this symbol or ID", query=symbol_or_id
                )
            if len(distinct_ids) > 1:
                raise AmbiguousAsset(
                    "more than one canonical asset uses this ticker",
                    distinct_ids,
                    query=symbol_or_id,
                )
            asset_id = distinct_ids[0]

        shard_filename = self._dataset.shard_filename_for(asset_id)
        shard_path = self._dataset.ensure_file(shard_filename)
        shard = pl.read_parquet(shard_path)
        row = shard.filter(pl.col("asset_id") == asset_id)
        if row.height == 0:
            raise AssetNotFound(
                "asset is canonical but has no published market state in this generation",
                query=symbol_or_id,
                asset_id=asset_id,
            )
        return AssetView(row.to_dicts()[0])

    # -- market-wide views ------------------------------------------------------

    def market(self) -> MarketView:
        """Return a summary view spanning every asset in this generation.

        Downloads every asset-market-state shard (there is no way to summarise the
        whole market from less data than that); a single-asset question should use
        :meth:`asset` instead, which downloads only the one shard it needs.
        """
        shard_paths = self._dataset.ensure_shards()
        frame = pl.concat([pl.read_parquet(p) for p in shard_paths], how="vertical_relaxed")
        return MarketView(frame)

    def query(self, mql_text: str) -> pl.DataFrame:
        """Run a Market Query Language query against this generation.

        Downloads every shard the first time it is called (see :meth:`market`); the
        query itself then runs entirely locally against the downloaded Parquet
        files - MQL never sends a query to a remote server (``docs/MQL.md``).
        """
        shard_paths = self._dataset.ensure_shards()
        return run_query(mql_text, shard_paths)
