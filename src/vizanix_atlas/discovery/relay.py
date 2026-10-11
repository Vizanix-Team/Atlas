"""The relay: collecting venues that refuse the workflow runners' network.

Some venues (Bybit, Binance derivatives) answer HTTP 403 or 451 to the United States
addresses GitHub-hosted runners use. Atlas does not route around that. Instead those venues
are collected, with exactly the same adapters and the same ``CollectionResult`` format, from a
network where they answer normally, and published as plain files over HTTPS. The scheduled
workflow then *pulls* them, so GitHub never needs a credential for the collector and the
collector never needs one for GitHub.

Pulling a file from another host is trust, so it is bounded:

- the venue must be declared ``egress: relay`` in ``config/exchanges.yaml``, so a relay can
  never supply (or overwrite) a venue the runners collect themselves;
- the URL must be ``https``, the response size is capped, and the body must match the
  SHA-256 in the relay's manifest (which catches a torn or cached download);
- the body must parse as a :class:`CollectionResult` for the requested venue;
- it must be fresh. A stale relay result is replaced by an explicit ``failed`` result, never
  re-published as if it were current, because a snapshot published late is worse than a gap
  that is recorded honestly.

The validity gate and the anomaly guards downstream apply to relayed data exactly as they do
to direct data.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from vizanix_atlas.core.atlas_time import to_epoch_ms, utc_now
from vizanix_atlas.core.errors import ConfigurationError
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.core.versions import SOFTWARE_VERSION
from vizanix_atlas.models.base import AtlasModel
from vizanix_atlas.models.enums import CollectionStatus
from vizanix_atlas.models.observations import AdapterHealth, CollectionResult
from vizanix_atlas.models.venue import Venue

_log = get_logger(__name__)

#: Where the project's relay publishes. Overridable with ``--url`` or ``ATLAS_RELAY_URL``.
DEFAULT_RELAY_URL: Final = "https://vizanix.tech/atlas-collector"

MANIFEST_FILENAME: Final = "manifest.json"

#: A relay result older than this is stale. The collector runs every 15 minutes a few minutes
#: before the workflow, so a healthy result is well under 10 minutes old at aggregation.
DEFAULT_MAX_AGE_SECONDS: Final = 1200

#: Hard cap on one downloaded file. A real result is a few MiB.
MAX_FILE_BYTES: Final = 32 * 1024 * 1024

_FETCH_ATTEMPTS: Final = 2
_RETRY_DELAY_SECONDS: Final = 3.0


class RelayFile(AtlasModel):
    """One published result, as the manifest describes it."""

    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    status: str
    collection_finished_at: int


class RelayManifest(AtlasModel):
    """Index of the results a relay currently serves."""

    generated_at: int
    software_version: str
    files: dict[str, RelayFile]


# --------------------------------------------------------------------------- publishing


def publish_relay(input_dir: Path, output_dir: Path, *, venues: list[str]) -> RelayManifest:
    """Validate collected results and publish them atomically, with a manifest.

    Each file is written to a temporary name and renamed into place, then the manifest is
    written the same way, so a reader never sees a half-written file under its final name.
    A venue whose result is missing or does not validate is left out of the manifest rather
    than published broken, and the previous file for it is removed so it cannot go stale
    silently under a fresh manifest.

    Args:
        input_dir: Where ``atlas collect`` wrote ``<slug>.json`` files.
        output_dir: The directory the web server serves.
        venues: The venue slugs this relay is responsible for.

    """
    output_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, RelayFile] = {}
    for slug in venues:
        source = input_dir / f"{slug}.json"
        target = output_dir / f"{slug}.json"
        try:
            body, result = _read_result(source, slug)
        except (OSError, ValueError) as error:
            _log.warning(
                "relay result not published",
                extra={"venue": slug, "status": "skipped", "detail": str(error)[:200]},
            )
            target.unlink(missing_ok=True)
            continue
        _atomic_write(target, body)
        files[slug] = RelayFile(
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            status=result.health.status,
            collection_finished_at=result.collection_finished_at,
        )
    manifest = RelayManifest(
        generated_at=to_epoch_ms(utc_now()), software_version=SOFTWARE_VERSION, files=files
    )
    _atomic_write(output_dir / MANIFEST_FILENAME, manifest.model_dump_json(indent=2).encode())
    return manifest


def _read_result(source: Path, slug: str) -> tuple[bytes, CollectionResult]:
    """Read and validate one collected result, checking it belongs to ``slug``.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If it is not a valid result, or holds a different venue.

    """
    body = source.read_bytes()
    result = CollectionResult.model_validate_json(body)
    if result.venue_slug != slug:
        raise ValueError(f"file holds venue {result.venue_slug!r}, expected {slug!r}")
    return body, result


def _atomic_write(path: Path, body: bytes) -> None:
    """Write ``body`` to ``path`` so a concurrent reader sees the old or new file, never half."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(body)
    temporary.chmod(0o644)
    temporary.replace(path)


# --------------------------------------------------------------------------- fetching


def failure_result(venue: Venue, reason: str) -> CollectionResult:
    """Build the explicit ``failed`` result recorded when a relay result cannot be used."""
    now = to_epoch_ms(utc_now())
    return CollectionResult(
        venue_slug=venue.slug,
        run_id=f"relay-{now}",
        collection_started_at=now,
        collection_finished_at=now,
        health=AdapterHealth(
            venue_slug=venue.slug,
            status=CollectionStatus.FAILED.value,
            duration_ms=0,
            notes=(f"relay result unusable: {reason}",),
        ),
    )


def _require_https(url: str) -> str:
    """Return ``url`` without a trailing slash, or raise if it is not an https URL."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise ConfigurationError("the relay URL must be an https URL", url=url)
    return url.rstrip("/")


async def fetch_relay(
    venue: Venue,
    *,
    base_url: str = DEFAULT_RELAY_URL,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    client: httpx.AsyncClient | None = None,
) -> CollectionResult:
    """Pull one relay venue's result, validating it, or return an explicit failure.

    Never raises for a relay-side problem (unreachable, stale, corrupt): the workflow must
    still upload an artifact so that the aggregation records the venue as failed. It raises
    :class:`ConfigurationError` only for a caller mistake: a venue that is not a relay venue,
    or a URL that is not https.

    Args:
        venue: The venue to pull. Must be declared ``egress: relay``.
        base_url: The directory the relay serves, ``https`` only.
        max_age_seconds: Results older than this are rejected as stale.
        client: Injected for tests.

    """
    if venue.egress != "relay":
        raise ConfigurationError(
            "only a venue declared egress: relay can be pulled from the relay", venue=venue.slug
        )
    root = _require_https(base_url)

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    try:
        last_reason = "unknown"
        for attempt in range(_FETCH_ATTEMPTS):
            try:
                result = await _fetch_once(http, root, venue, max_age_seconds)
            except _RelayRejected as rejected:
                last_reason = str(rejected)
                # A torn read can happen when the relay updates the file between the
                # manifest request and the file request, so one retry is worth a few seconds.
                if attempt + 1 < _FETCH_ATTEMPTS and rejected.retryable:
                    await asyncio.sleep(_RETRY_DELAY_SECONDS)
                    continue
                break
            else:
                return result
        _log.warning(
            "relay result rejected",
            extra={"venue": venue.slug, "status": "failed", "detail": last_reason},
        )
        return failure_result(venue, last_reason)
    finally:
        if owns_client:
            await http.aclose()


class _RelayRejected(Exception):
    """A relay result that must not be used, and whether trying again could help."""

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.retryable = retryable


async def _get_bounded(http: httpx.AsyncClient, url: str) -> bytes:
    """GET ``url`` over https and return the body, refusing anything oversized."""
    try:
        async with http.stream("GET", url) as response:
            if response.url.scheme != "https":
                raise _RelayRejected("the relay redirected away from https")
            if response.status_code != 200:
                raise _RelayRejected(f"HTTP {response.status_code} for {url.rsplit('/', 1)[-1]}")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise _RelayRejected("the relay file exceeds the size limit")
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError as error:
        raise _RelayRejected(f"the relay is unreachable ({type(error).__name__})") from error


async def _fetch_once(
    http: httpx.AsyncClient, root: str, venue: Venue, max_age_seconds: int
) -> CollectionResult:
    """One attempt: manifest, file, then every check."""
    manifest_body = await _get_bounded(http, f"{root}/{MANIFEST_FILENAME}")
    try:
        manifest = RelayManifest.model_validate_json(manifest_body)
    except ValueError as error:
        raise _RelayRejected("the relay manifest is not valid") from error

    entry = manifest.files.get(venue.slug)
    if entry is None:
        raise _RelayRejected("the relay publishes no result for this venue")

    body = await _get_bounded(http, f"{root}/{venue.slug}.json")
    if hashlib.sha256(body).hexdigest() != entry.sha256:
        raise _RelayRejected("the file does not match the manifest checksum", retryable=True)

    try:
        result = CollectionResult.model_validate_json(body)
    except ValueError as error:
        raise _RelayRejected("the relay file is not a valid collection result") from error
    if result.venue_slug != venue.slug:
        raise _RelayRejected(f"the relay file holds venue {result.venue_slug!r}")

    age_seconds = (to_epoch_ms(utc_now()) - result.collection_finished_at) / 1000.0
    if age_seconds > max_age_seconds:
        raise _RelayRejected(f"the result is {int(age_seconds)} seconds old, over the limit")
    if age_seconds < -300:
        raise _RelayRejected("the result is dated in the future: the relay clock is wrong")

    health = result.health.model_copy(
        update={
            "notes": (*result.health.notes, f"collected by the relay at {urlsplit(root).netloc}")
        }
    )
    return result.model_copy(update={"health": health})
