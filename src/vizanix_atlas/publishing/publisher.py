"""The publication interface.

Analytics and the dataset builder never import GitHub API code (see
``docs/GITHUB_ARCHITECTURE.md``, section 155): they produce files and manifests and
hand them to whatever implements this protocol. :class:`FilesystemPublisher` is what
local development and the test suite use; :class:`GitHubReleasePublisher` is what
production Actions workflows use. Both obey the same atomic-like publication ordering
because that ordering lives in :mod:`vizanix_atlas.publishing.publish`, not here.

GitHub endpoints used by :class:`GitHubReleasePublisher` are current as of the official
REST API documentation (docs.github.com/en/rest/releases), verified rather than
remembered:

- ``GET /repos/{owner}/{repo}/releases/tags/{tag}``
- ``POST /repos/{owner}/{repo}/releases``
- ``PATCH /repos/{owner}/{repo}/releases/{release_id}``
- ``POST https://uploads.github.com/repos/{owner}/{repo}/releases/{release_id}/assets?name=...``
  (note the distinct upload host)
- ``GET /repos/{owner}/{repo}/releases/{release_id}/assets``
- ``DELETE /repos/{owner}/{repo}/releases/assets/{asset_id}``
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from vizanix_atlas.core.errors import PublicationFailure
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.models.manifest import GenerationManifest, LatestPointer

_log = get_logger(__name__)

#: GitHub Releases tag for the current-state pointer and its generations.
DATA_LATEST_TAG = "data-latest"

#: The manifest filename inside every generation's release assets.
MANIFEST_FILENAME = "manifest.json"

#: The pointer filename inside the data-latest release's assets.
LATEST_FILENAME = "latest.json"


@runtime_checkable
class Publisher(Protocol):
    """What the publication sequence needs from a storage backend.

    Every method is named after what it does to durable storage, not after a GitHub
    concept, so a filesystem implementation reads naturally too.
    """

    async def upload_generation_file(
        self, generation_id: str, filename: str, local_path: Path
    ) -> None:
        """Upload one file belonging to a specific, immutable generation."""
        ...

    async def read_generation_manifest(self, generation_id: str) -> GenerationManifest | None:
        """Return a previously published generation's manifest, or ``None``."""
        ...

    async def publish_generation_manifest(
        self, generation_id: str, manifest: GenerationManifest
    ) -> None:
        """Publish a generation's manifest. Called only after every file it
        references has been uploaded and verified.
        """
        ...

    async def read_latest_pointer(self) -> LatestPointer | None:
        """Return the current ``latest`` pointer, or ``None`` if none has ever been
        published.
        """
        ...

    async def publish_latest_pointer(self, pointer: LatestPointer) -> None:
        """Advance the ``latest`` pointer. The last write in the publication
        sequence; see ``docs/STORAGE.md`` section 8.
        """
        ...

    async def list_generation_ids(self) -> tuple[str, ...]:
        """Return every generation with a published manifest, for housekeeping and
        recovery.
        """
        ...

    async def delete_generation_file(self, generation_id: str, filename: str) -> None:
        """Remove one file from a generation. Used by housekeeping only, and only
        under the guard rails in ``config/retention.yaml``.
        """
        ...


class FilesystemPublisher:
    """Publishes to a local directory tree, mirroring the shape of GitHub Releases.

    Layout::

        <root>/
            data-latest/
                latest.json
            atlas-data-<generation_id>/
                manifest.json
                <every other published file>

    Used by local development (``atlas collect`` / ``atlas build-dataset`` without
    GitHub Actions) and by every test that exercises the publication sequence, so that
    no test needs network access or credentials.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _generation_dir(self, generation_id: str) -> Path:
        return self.root / f"atlas-data-{generation_id}"

    def _latest_dir(self) -> Path:
        return self.root / DATA_LATEST_TAG

    async def upload_generation_file(
        self, generation_id: str, filename: str, local_path: Path
    ) -> None:
        """Copy a file into the generation's directory."""
        destination = self._generation_dir(generation_id) / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(local_path.read_bytes())  # noqa: ASYNC240 - local test publisher

    async def read_generation_manifest(self, generation_id: str) -> GenerationManifest | None:
        """Read a generation's manifest, if one was published."""
        path = self._generation_dir(generation_id) / MANIFEST_FILENAME
        if not path.is_file():
            return None
        return GenerationManifest.model_validate_json(path.read_text(encoding="utf-8"))

    async def publish_generation_manifest(
        self, generation_id: str, manifest: GenerationManifest
    ) -> None:
        """Write the manifest. Published last among a generation's own files, but
        still before the ``latest`` pointer can reference it.
        """
        path = self._generation_dir(generation_id) / MANIFEST_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    async def read_latest_pointer(self) -> LatestPointer | None:
        """Read the current pointer, if one has ever been published."""
        path = self._latest_dir() / LATEST_FILENAME
        if not path.is_file():
            return None
        return LatestPointer.model_validate_json(path.read_text(encoding="utf-8"))

    async def publish_latest_pointer(self, pointer: LatestPointer) -> None:
        """Write the pointer. The single write that makes a generation canonical."""
        path = self._latest_dir() / LATEST_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pointer.model_dump_json(indent=2), encoding="utf-8")

    async def list_generation_ids(self) -> tuple[str, ...]:
        """List every generation directory with a manifest."""
        if not self.root.is_dir():
            return ()
        prefix = "atlas-data-"
        ids = [
            child.name.removeprefix(prefix)
            for child in self.root.iterdir()
            if child.is_dir()
            and child.name.startswith(prefix)
            and (child / MANIFEST_FILENAME).is_file()
        ]
        return tuple(sorted(ids))

    async def delete_generation_file(self, generation_id: str, filename: str) -> None:
        """Delete one file from a generation's directory."""
        path = self._generation_dir(generation_id) / filename
        path.unlink(missing_ok=True)


class GitHubReleasePublisher:
    """Publishes to GitHub Releases.

    Two release tags are used: ``atlas-data-<generation_id>`` for each immutable
    generation's own files, and ``data-latest`` for the small, frequently-updated
    pointer. Keeping the pointer in its own release means advancing it never touches
    the generation's own (already-verified, immutable) assets.
    """

    def __init__(
        self,
        *,
        owner: str,
        repo: str,
        token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.owner = owner
        self.repo = repo
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._api = client or httpx.AsyncClient(
            base_url="https://api.github.com", headers=headers, timeout=30.0
        )
        self._uploads = httpx.AsyncClient(
            base_url="https://uploads.github.com", headers=headers, timeout=120.0
        )
        # Injected test clients may already point at a single mock host; reuse it for
        # uploads too rather than constructing a second real client.
        if client is not None:
            self._uploads = client

    async def aclose(self) -> None:
        """Close both underlying HTTP clients."""
        await self._api.aclose()
        if self._uploads is not self._api:
            await self._uploads.aclose()

    async def _find_or_create_release(self, tag: str) -> int:
        """Return a release's ID, creating it if it does not exist.

        Raises:
            PublicationFailure: If GitHub refuses the lookup or the creation for a
                reason other than "not found".

        """
        response = await self._api.get(f"/repos/{self.owner}/{self.repo}/releases/tags/{tag}")
        if response.status_code == 200:
            return int(response.json()["id"])
        if response.status_code != 404:
            raise PublicationFailure(
                "could not look up release", tag=tag, status=response.status_code
            )
        created = await self._api.post(
            f"/repos/{self.owner}/{self.repo}/releases",
            json={
                "tag_name": tag,
                "name": tag,
                # Data releases are not software releases: never surface them as
                # GitHub's implicit "latest release" (see docs/STORAGE.md section 105).
                "make_latest": "false",
                "prerelease": tag != DATA_LATEST_TAG,
            },
        )
        if created.status_code != 201:
            raise PublicationFailure(
                "could not create release",
                tag=tag,
                status=created.status_code,
                body=created.text[:300],
            )
        return int(created.json()["id"])

    async def _release_assets(self, release_id: int) -> dict[str, int]:
        """Return existing asset names to their asset IDs, for update-in-place."""
        response = await self._api.get(
            f"/repos/{self.owner}/{self.repo}/releases/{release_id}/assets",
            params={"per_page": 100},
        )
        if response.status_code != 200:
            raise PublicationFailure(
                "could not list release assets", release_id=release_id, status=response.status_code
            )
        return {asset["name"]: asset["id"] for asset in response.json()}

    async def _upload_asset(
        self, release_id: int, filename: str, content: bytes, *, content_type: str
    ) -> None:
        """Upload one asset, replacing an existing asset of the same name first.

        GitHub's upload endpoint rejects a name collision, so a re-publish of the same
        generation (a retried workflow run) must delete the stale asset before
        uploading its replacement.
        """
        existing = await self._release_assets(release_id)
        if filename in existing:
            deletion = await self._api.delete(
                f"/repos/{self.owner}/{self.repo}/releases/assets/{existing[filename]}"
            )
            if deletion.status_code not in (204, 404):
                raise PublicationFailure(
                    "could not remove the stale asset before replacing it",
                    filename=filename,
                    status=deletion.status_code,
                )
        response = await self._uploads.post(
            f"/repos/{self.owner}/{self.repo}/releases/{release_id}/assets",
            params={"name": filename},
            headers={"Content-Type": content_type},
            content=content,
        )
        if response.status_code != 201:
            raise PublicationFailure(
                "asset upload failed",
                filename=filename,
                status=response.status_code,
                body=response.text[:300],
            )

    @staticmethod
    def _content_type(filename: str) -> str:
        """Infer an asset's content type from its extension."""
        if filename.endswith(".parquet"):
            return "application/vnd.apache.parquet"
        if filename.endswith(".json"):
            return "application/json"
        if filename.endswith((".tar.zst", ".zst")):
            return "application/zstd"
        return "application/octet-stream"

    async def upload_generation_file(
        self, generation_id: str, filename: str, local_path: Path
    ) -> None:
        """Upload one file to the generation's own release."""
        release_id = await self._find_or_create_release(f"atlas-data-{generation_id}")
        await self._upload_asset(
            release_id,
            filename,
            local_path.read_bytes(),  # noqa: ASYNC240 - bounded by max_single_asset_bytes
            content_type=self._content_type(filename),
        )

    async def _read_asset(self, tag: str, filename: str) -> bytes | None:
        """Download one named asset from a release, or ``None`` if either is absent."""
        response = await self._api.get(f"/repos/{self.owner}/{self.repo}/releases/tags/{tag}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise PublicationFailure(
                "could not look up release", tag=tag, status=response.status_code
            )
        assets = {a["name"]: a["url"] for a in response.json().get("assets", [])}
        if filename not in assets:
            return None
        download = await self._api.get(
            assets[filename], headers={"Accept": "application/octet-stream"}
        )
        if download.status_code != 200:
            raise PublicationFailure(
                "could not download asset", filename=filename, status=download.status_code
            )
        return download.content

    async def read_generation_manifest(self, generation_id: str) -> GenerationManifest | None:
        """Read and parse a generation's manifest asset, if published."""
        content = await self._read_asset(f"atlas-data-{generation_id}", MANIFEST_FILENAME)
        return GenerationManifest.model_validate_json(content) if content is not None else None

    async def publish_generation_manifest(
        self, generation_id: str, manifest: GenerationManifest
    ) -> None:
        """Upload the manifest to the generation's release, replacing any prior one."""
        release_id = await self._find_or_create_release(f"atlas-data-{generation_id}")
        await self._upload_asset(
            release_id,
            MANIFEST_FILENAME,
            manifest.model_dump_json(indent=2).encode("utf-8"),
            content_type="application/json",
        )

    async def read_latest_pointer(self) -> LatestPointer | None:
        """Read the current pointer from the ``data-latest`` release."""
        content = await self._read_asset(DATA_LATEST_TAG, LATEST_FILENAME)
        return LatestPointer.model_validate_json(content) if content is not None else None

    async def publish_latest_pointer(self, pointer: LatestPointer) -> None:
        """Advance the pointer. The last call in a publish; see
        :mod:`vizanix_atlas.publishing.publish`.
        """
        release_id = await self._find_or_create_release(DATA_LATEST_TAG)
        await self._upload_asset(
            release_id,
            LATEST_FILENAME,
            pointer.model_dump_json(indent=2).encode("utf-8"),
            content_type="application/json",
        )

    async def list_generation_ids(self) -> tuple[str, ...]:
        """List every release tagged as an Atlas data generation."""
        ids: list[str] = []
        page = 1
        prefix = "atlas-data-"
        while True:
            response = await self._api.get(
                f"/repos/{self.owner}/{self.repo}/releases",
                params={"per_page": 100, "page": page},
            )
            if response.status_code != 200:
                raise PublicationFailure("could not list releases", status=response.status_code)
            page_releases = response.json()
            if not page_releases:
                break
            ids.extend(
                r["tag_name"].removeprefix(prefix)
                for r in page_releases
                if r["tag_name"].startswith(prefix)
            )
            page += 1
        return tuple(sorted(ids))

    async def delete_generation_file(self, generation_id: str, filename: str) -> None:
        """Delete one asset from a generation's release."""
        release_id = await self._find_or_create_release(f"atlas-data-{generation_id}")
        existing = await self._release_assets(release_id)
        asset_id = existing.get(filename)
        if asset_id is None:
            return
        response = await self._api.delete(
            f"/repos/{self.owner}/{self.repo}/releases/assets/{asset_id}"
        )
        if response.status_code not in (204, 404):
            raise PublicationFailure(
                "could not delete asset",
                filename=filename,
                status=response.status_code,
            )
