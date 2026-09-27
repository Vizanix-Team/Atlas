"""The Vizanix Atlas Python SDK.

``Atlas.latest()`` (or ``Atlas.from_local()`` for a dataset already on disk) is the
entry point; see ``docs/DATA_MODEL.md`` and the project README for the intended usage.
Nothing in this package requires a GitHub token or any other credential: published
datasets are ordinary public GitHub Release assets, downloaded over plain HTTPS (see
``docs/GITHUB_ARCHITECTURE.md``, "no secret requirement").
"""

from __future__ import annotations

from vizanix_atlas.sdk.client import Atlas
from vizanix_atlas.sdk.views import AssetView, MarketView

__all__ = ["AssetView", "Atlas", "MarketView"]
