"""Regenerate the tiny fixture the website ships with for local development.

The fixture is built from the same hand-constructed observations the test suite uses,
marked ``"fixture": true`` so the site shows a banner instead of pretending it is market
data. The Pages workflow deletes this directory and replaces it with a real export.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.unit.test_publishing import _permissive_config  # noqa: E402

from vizanix_atlas.models.manifest import GenerationManifest  # noqa: E402
from vizanix_atlas.publishing.publish import publish_generation  # noqa: E402
from vizanix_atlas.publishing.publisher import FilesystemPublisher  # noqa: E402
from vizanix_atlas.publishing.webexport import export_web_data  # noqa: E402
from vizanix_atlas.sdk.dataset import Dataset  # noqa: E402
from vizanix_atlas.storage.dataset_builder import build_dataset_files  # noqa: E402
from vizanix_atlas.util.sample import build_sample_generation  # noqa: E402

OUT = ROOT / "web" / "public" / "data"


async def main() -> None:
    config = _permissive_config()
    generation = build_sample_generation(extra_asset=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "stage"
        built = build_dataset_files(generation, stage, config=config)
        publisher = FilesystemPublisher(Path(tmp) / "releases")
        outcome = await publish_generation(
            generation,
            dataset_dir=stage,
            written_files=list(built.written_files),
            publisher=publisher,
            config=config,
            release_tag=f"atlas-data-{generation.generation_id}",
            shard_indices=built.shard_indices,
        )
        assert outcome.published, outcome.reason
        root = Path(tmp) / "releases" / f"atlas-data-{generation.generation_id}"
        manifest = GenerationManifest.model_validate_json((root / "manifest.json").read_text())
        if OUT.exists():
            shutil.rmtree(OUT)
        export_web_data(Dataset(manifest=manifest, root=root), OUT)

    overview_path = OUT / "market-overview.json"
    overview = json.loads(overview_path.read_text())
    overview["fixture"] = True
    overview_path.write_text(json.dumps(overview, separators=(",", ":")))


asyncio.run(main())
