"""Coverage and provenance: what a number is made of.

python examples/data_quality.py [DATASET_DIR] [SYMBOL]
"""

from __future__ import annotations

import sys

import polars as pl
from _open import open_atlas

atlas = open_atlas()
symbol = sys.argv[2] if len(sys.argv) > 2 else "BTC"
asset = atlas.asset(symbol)

print(
    f"{asset.symbol}: {asset.venue_count} venues in the reference price, "
    f"coverage {asset.coverage_ratio}, partial={asset.partial_data}"
)

# The per-venue decomposition is a published table: every venue observation is either
# included or excluded with a reason.
venues = pl.read_parquet(atlas.dataset.ensure_file("venue_asset_state.parquet")).filter(
    pl.col("asset_id") == asset.asset_id
)
print(
    venues.select(
        "venue_slug",
        "price_usd",
        "deviation_bps",
        "included_in_reference_price",
        "exclusion_reason",
    ).sort("venue_slug")
)

health = pl.read_parquet(atlas.dataset.ensure_file("adapter_health.parquet"))
print(health.select("venue_slug", "status", "instrument_count", "ticker_count", "parse_failures"))
