"""One generation's files, resolved lazily from disk or a remote release.

A :class:`Dataset` never assumes every file it lists in its manifest is present
locally: :meth:`Dataset.ensure_file` fetches (and verifies) a file the first time it
is actually needed, and does nothing for files a query never touches - the "don't
download the entire history to answer one question" requirement from
``docs/ROADMAP.md``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from vizanix_atlas.core.errors import ChecksumMismatch, DatasetUnavailable
from vizanix_atlas.models.manifest import GenerationManifest
from vizanix_atlas.sdk.remote import GenerationFileSource
from vizanix_atlas.storage.writer import sha256_file


@dataclass(slots=True)
class Dataset:
    """A generation's manifest plus a place to find (or fetch) its files."""

    manifest: GenerationManifest
    root: Path
    downloader: GenerationFileSource | None = None

    @property
    def generation_id(self) -> str:
        """The generation's immutable identifier."""
        return self.manifest.generation_id

    def ensure_file(self, filename: str) -> Path:
        """Return a local, checksum-verified path for ``filename``.

        Uses whatever copy already exists at ``root / filename`` if its hash still
        matches the manifest; otherwise downloads it (when a downloader was given)
        and verifies it before returning.

        Raises:
            DatasetUnavailable: If the manifest does not list ``filename``, or no
                downloader is available and the file is not already present.
            ChecksumMismatch: If a downloaded file does not match its manifest hash.

        """
        entry = self.manifest.file(filename)
        if entry is None:
            raise DatasetUnavailable(
                "file is not listed in this generation's manifest",
                filename=filename,
                generation_id=self.generation_id,
            )

        path = self.root / filename
        if path.is_file() and sha256_file(path) == entry.sha256:
            return path

        if self.downloader is None:
            raise DatasetUnavailable(
                "file is not available locally and no downloader is configured",
                filename=filename,
                generation_id=self.generation_id,
            )

        data = self.downloader.fetch_generation_file(self.generation_id, filename)
        actual = hashlib.sha256(data).hexdigest()
        if actual != entry.sha256:
            raise ChecksumMismatch(
                "downloaded file did not match its manifest checksum",
                filename=filename,
                generation_id=self.generation_id,
                expected=entry.sha256,
                actual=actual,
            )
        self.root.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def ensure_shards(self) -> tuple[Path, ...]:
        """Ensure every asset-market-state shard is present locally, and return their paths.

        Needed for any query spanning more than one asset (:meth:`Atlas.market`,
        :meth:`Atlas.query`); a single-asset lookup should prefer
        :meth:`ensure_file` on just that asset's own shard instead.
        """
        return tuple(self.ensure_file(f.filename) for f in self.manifest.shard_files())

    def shard_filename_for(self, asset_id: str) -> str:
        """Return the filename of the shard ``asset_id`` is stored in."""
        from vizanix_atlas.core.identifiers import shard_for

        index = shard_for(asset_id, self.manifest.shard_count)
        for entry in self.manifest.shard_files():
            if entry.shard_index == index:
                return entry.filename
        raise DatasetUnavailable(
            "no shard file covers this asset's computed shard index",
            asset_id=asset_id,
            shard_index=index,
        )
