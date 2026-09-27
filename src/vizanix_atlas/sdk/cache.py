"""Local cache for downloaded dataset files.

Every cached file lives under ``<cache_dir>/<generation_id>/<filename>`` and is
verified against the manifest's SHA-256 before being trusted, both right after
download and on every cache hit - a corrupted or truncated local file must never be
silently reused (see ``docs/LIMITATIONS.md``).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from vizanix_atlas.core.errors import ChecksumMismatch
from vizanix_atlas.storage.writer import sha256_file

#: Overridable via the ``ATLAS_CACHE_DIR`` environment variable.
_DEFAULT_CACHE_DIRNAME = ".cache/vizanix-atlas"


def default_cache_dir() -> Path:
    """Return the default local cache directory, creating nothing yet."""
    override = os.environ.get("ATLAS_CACHE_DIR")
    if override:
        return Path(override)
    return Path.home() / _DEFAULT_CACHE_DIRNAME


class DatasetCache:
    """Verified local storage for one or more generations' files."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_cache_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    def generation_dir(self, generation_id: str) -> Path:
        """Return (creating if needed) the directory for one generation's files."""
        path = self.root / generation_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cached_path(
        self, generation_id: str, filename: str, *, expected_sha256: str
    ) -> Path | None:
        """Return the local path for ``filename`` if it is already cached and intact.

        A file whose hash no longer matches is treated as absent (not repaired in
        place), so the caller re-downloads it rather than trusting a value this
        function already knows is wrong.
        """
        path = self.generation_dir(generation_id) / filename
        if not path.is_file():
            return None
        if sha256_file(path) != expected_sha256:
            return None
        return path

    def store(
        self, generation_id: str, filename: str, data: bytes, *, expected_sha256: str
    ) -> Path:
        """Write ``data`` to the cache and verify it before returning its path.

        Raises:
            ChecksumMismatch: If the downloaded bytes do not match the manifest.

        """
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_sha256:
            raise ChecksumMismatch(
                "downloaded file did not match its manifest checksum",
                filename=filename,
                generation_id=generation_id,
                expected=expected_sha256,
                actual=actual,
            )
        path = self.generation_dir(generation_id) / filename
        path.write_bytes(data)
        return path
