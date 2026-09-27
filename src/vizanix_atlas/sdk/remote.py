"""Downloads published dataset files from public GitHub Release assets.

Deliberately does not use the GitHub REST API or any token: a released generation's
files are ordinary public release assets, reachable at a stable,
predictable URL (``docs/STORAGE.md``, "release naming"). Avoiding the API here means
the SDK also avoids GitHub's per-token rate limits, and means a consumer never needs
so much as a read-only token to use Atlas - only the GitHub Actions publisher itself
needs credentials (see ``docs/GITHUB_ARCHITECTURE.md``, "no secret requirement").
"""

from __future__ import annotations

import httpx

from vizanix_atlas.core.errors import DatasetUnavailable
from vizanix_atlas.publishing.publisher import DATA_LATEST_TAG, LATEST_FILENAME, MANIFEST_FILENAME

#: Default upstream repository the SDK downloads published datasets from.
DEFAULT_OWNER = "Vizanix-Team"
DEFAULT_REPO = "Atlas"

_RELEASE_DOWNLOAD_BASE = "https://github.com/{owner}/{repo}/releases/download/{tag}/{filename}"


def release_asset_url(owner: str, repo: str, tag: str, filename: str) -> str:
    """Return the stable public download URL for one release asset."""
    return _RELEASE_DOWNLOAD_BASE.format(owner=owner, repo=repo, tag=tag, filename=filename)


class ReleaseDownloader:
    """Fetches published files over plain HTTPS, with no authentication."""

    def __init__(
        self, *, owner: str = DEFAULT_OWNER, repo: str = DEFAULT_REPO, timeout: float = 60.0
    ) -> None:
        self.owner = owner
        self.repo = repo
        self._client = httpx.Client(follow_redirects=True, timeout=timeout)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> ReleaseDownloader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch(self, tag: str, filename: str) -> bytes:
        """Download one release asset's bytes.

        Raises:
            DatasetUnavailable: If the asset does not exist or GitHub could not be
                reached, so a caller sees one clear, typed reason rather than a raw
                ``httpx`` exception.

        """
        url = release_asset_url(self.owner, self.repo, tag, filename)
        try:
            response = self._client.get(url)
        except httpx.HTTPError as error:
            raise DatasetUnavailable(
                "could not reach GitHub to download a dataset file", url=url
            ) from error
        if response.status_code == 404:
            raise DatasetUnavailable("dataset file not found", url=url, status=404)
        if response.status_code != 200:
            raise DatasetUnavailable(
                "unexpected response downloading a dataset file",
                url=url,
                status=response.status_code,
            )
        return response.content

    def fetch_latest_pointer(self) -> bytes:
        """Download the ``data-latest`` pointer document's raw bytes."""
        return self.fetch(DATA_LATEST_TAG, LATEST_FILENAME)

    def fetch_manifest(self, generation_id: str) -> bytes:
        """Download one generation's manifest's raw bytes."""
        return self.fetch(f"atlas-data-{generation_id}", MANIFEST_FILENAME)

    def fetch_generation_file(self, generation_id: str, filename: str) -> bytes:
        """Download one file belonging to a specific generation."""
        return self.fetch(f"atlas-data-{generation_id}", filename)
