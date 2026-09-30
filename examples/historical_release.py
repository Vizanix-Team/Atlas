"""Open a specific past generation by ID (each is immutable).

    python examples/historical_release.py GENERATION_ID

Note: a generation is a snapshot. Windowed metrics (returns, percentiles) need
accumulated history and are empty until Atlas has collected enough of it.
"""

from __future__ import annotations

import sys

from vizanix_atlas import Atlas

if len(sys.argv) < 2:
    raise SystemExit("usage: historical_release.py GENERATION_ID")

atlas = Atlas.from_generation(sys.argv[1])
manifest = atlas.manifest
print(f"generation {manifest.generation_id} published {manifest.published_at}")
print(f"previous generation: {manifest.previous_generation_id}")
print(
    f"{manifest.asset_count} assets, {manifest.instrument_count} instruments, "
    f"{manifest.venues.successful}/{manifest.venues.attempted} venues succeeded"
)
