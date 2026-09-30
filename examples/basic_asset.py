"""One asset as a cross-venue object.

python examples/basic_asset.py [DATASET_DIR] [SYMBOL]
"""

from __future__ import annotations

import sys

from _open import open_atlas

from vizanix_atlas.core.errors import AmbiguousAsset, AssetNotFound

atlas = open_atlas()
symbol = sys.argv[2] if len(sys.argv) > 2 else "BTC"

try:
    asset = atlas.asset(symbol)
except AmbiguousAsset as error:
    # Atlas refuses to guess which of several tokens sharing a ticker you meant.
    print(f"{symbol} is ambiguous; candidates:")
    for candidate in error.candidates[:10]:
        print("  ", candidate)
    raise SystemExit(1) from error
except AssetNotFound as error:
    raise SystemExit(f"no such asset: {error}") from error

state = asset.state()
print(f"{asset.symbol}  ({asset.asset_id})")
print(f"  reference price  {asset.reference_price}   method={asset.reference_price_method}")
print(f"  venues           {asset.venue_count}   coverage={asset.coverage_ratio}")
print(f"  dispersion       {asset.price_dispersion_bps} bps")
print(f"  reported volume  {asset.reported_volume_24h_usd} USD (as reported by venues)")
print(
    f"  spot / perp      {state.volume.reported_spot_volume_24h_usd} / {state.volume.reported_perp_volume_24h_usd}"
)
print(f"  funding (8h)     {asset.funding_rate_8h_median}")
print(f"  open interest    {asset.open_interest_usd} USD")
