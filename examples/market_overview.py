"""A market-wide summary.

python examples/market_overview.py [DATASET_DIR]
"""

from __future__ import annotations

from _open import open_atlas

atlas = open_atlas()
market = atlas.market()

print(f"generation {atlas.generation_id}")
print(f"assets: {market.asset_count}")
print(f"on 2+ venues: {market.qualified_asset_count(min_venue_count=2)}")
print()
print("Largest reported 24h volume, assets on 2+ venues:")
print(
    market.top_by_volume(10, min_venues=2).select(
        "symbol", "reported_volume_24h_usd", "venue_count"
    )
)
