"""Parquet writing, hashing and size accounting.

Every file Atlas publishes is described in its manifest by size, row count and SHA-256
digest (see ``docs/STORAGE.md``), so writing a file and describing it are one
operation here rather than two that could drift apart.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from vizanix_atlas.core.config import PublishingConfig
from vizanix_atlas.core.errors import ValidationFailure

#: Chunk size for hashing, matched to the HTTP client's read chunk size so both use one
#: well-understood buffer size rather than two arbitrary ones.
_HASH_CHUNK_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class WrittenFile:
    """A file written to local disk, described well enough to add to a manifest."""

    path: Path
    filename: str
    size_bytes: int
    sha256: str
    row_count: int | None
    table: str | None


def sha256_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 digest of a file's bytes.

    Streams the file rather than reading it whole, so hashing a large shard does not
    require holding it in memory a second time alongside whatever produced it.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def write_parquet(
    frame: pl.DataFrame,
    path: Path,
    *,
    table: str,
    config: PublishingConfig,
) -> WrittenFile:
    """Write one Parquet file and describe it.

    Raises:
        ValidationFailure: If the resulting file exceeds
            ``config.max_single_asset_bytes``. Atlas would rather refuse a single
            oversized file than publish something a venue schema change quietly
            inflated to gigabytes.

    """
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(
        path,
        compression=config.compression,  # type: ignore[arg-type]
        compression_level=config.compression_level,
        row_group_size=config.parquet_row_group_size,
        statistics=True,
    )
    size = path.stat().st_size
    if size > config.max_single_asset_bytes:
        raise ValidationFailure(
            "a single published file exceeded its size budget",
            path=str(path),
            size_bytes=size,
            budget_bytes=config.max_single_asset_bytes,
        )
    return WrittenFile(
        path=path,
        filename=path.name,
        size_bytes=size,
        sha256=sha256_file(path),
        row_count=frame.height,
        table=table,
    )


def write_json(payload: str, path: Path) -> WrittenFile:
    """Write a small JSON document (a manifest, a pointer, an overview) and describe it.

    Kept separate from :func:`write_parquet` because a JSON document has no row count
    and no table name in the same sense a Parquet file does.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    size = path.stat().st_size
    return WrittenFile(
        path=path,
        filename=path.name,
        size_bytes=size,
        sha256=sha256_file(path),
        row_count=None,
        table=None,
    )
