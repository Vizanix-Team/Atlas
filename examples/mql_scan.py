"""A whole-market question in MQL, run locally.

    python examples/mql_scan.py [DATASET_DIR]

Describes structure (many venues, wide dispersion); it is not a recommendation.
"""

from __future__ import annotations

from _open import open_atlas

atlas = open_atlas()
result = atlas.query(
    """
    SELECT asset, reference_price, venue_count, price_dispersion_bps, reported_volume_24h_usd
    FROM market
    WHERE venue_count >= 8 AND reported_volume_24h_usd > 10000000
    ORDER BY price_dispersion_bps DESC
    LIMIT 10
    """
)
print(result)
