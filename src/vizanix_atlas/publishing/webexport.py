"""Compact JSON exports for the static website.

The website has no backend, and browsers cannot fetch GitHub release assets directly
(release downloads redirect to a host without CORS headers), so the Pages workflow
downloads the published dataset and bundles a small, size-bounded slice of it with the
site. This module builds that slice. It never invents values: a metric that is absent
in the dataset is exported as ``null`` and the site renders it as "not available".
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

import polars as pl

from vizanix_atlas.core.atlas_time import to_iso, utc_now
from vizanix_atlas.core.config import load_exchange_registry
from vizanix_atlas.core.errors import ValidationFailure
from vizanix_atlas.sdk.dataset import Dataset

#: Hard ceiling for the bundled data directory, so a schema change cannot silently ship
#: hundreds of megabytes to every visitor.
MAX_EXPORT_BYTES = 25 * 1024 * 1024

_ASSET_LIST_COLUMNS = {
    "id": "asset_id",
    "symbol": "symbol",
    "name": "name",
    "resolution": "resolution_state",
    "price": "reference_price__value",
    "price_method": "reference_price__method",
    "venues": "venue_count",
    "volume_usd": "reported_volume_24h_usd",
    "spot_volume_usd": "volume__reported_spot_volume_24h_usd",
    "perp_volume_usd": "volume__reported_perp_volume_24h_usd",
    "dispersion_bps": "dispersion__price_dispersion_bps",
    "change_24h": "returns__change_24h",
    "funding_8h": "funding__rate_8h_median",
    "oi_usd": "open_interest__total_usd",
    "basis_bps": "basis__perp_basis_bps_median",
    "spread_bps": "liquidity__best_spread_bps",
    "coverage": "quality__coverage__coverage_ratio",
    "partial": "is_partial",
}


def _clean(value: Any) -> Any:
    """Make a value strict-JSON safe: non-finite floats become ``None``."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_clean(payload), allow_nan=False, separators=(",", ":")), "utf-8")


def asset_file_name(asset_id: str, symbol: str) -> str:
    """Return a stable, filesystem-safe file name for an asset's detail document."""
    safe = "".join(c for c in symbol.lower() if c.isalnum())[:12] or "asset"
    return f"{safe}-{hashlib.sha256(asset_id.encode()).hexdigest()[:8]}.json"


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _top(frame: pl.DataFrame, column: str, n: int, *, min_venues: int = 1) -> list[dict[str, Any]]:
    subset = frame.filter(pl.col(column).is_not_null() & (pl.col("venue_count") >= min_venues))
    ranked = subset.sort([column, "asset_id"], descending=[True, False]).head(n)
    return [
        {"id": r["asset_id"], "symbol": r["symbol"], "value": r[column], "venues": r["venue_count"]}
        for r in ranked.iter_rows(named=True)
    ]


def export_web_data(dataset: Dataset, out_dir: Path, *, detail_assets: int = 200) -> dict[str, int]:
    """Write the website's JSON files and return their sizes in bytes.

    Raises:
        ValidationFailure: If the export exceeds :data:`MAX_EXPORT_BYTES`.

    """
    manifest = dataset.manifest
    market = pl.concat(
        [pl.read_parquet(p) for p in dataset.ensure_shards()], how="vertical_relaxed"
    ).sort("asset_id")
    health = pl.read_parquet(dataset.ensure_file("adapter_health.parquet"))
    venue_state = pl.read_parquet(dataset.ensure_file("venue_asset_state.parquet"))

    def numeric(column: str, min_venues: int = 1) -> list[float]:
        rows = market.filter(pl.col(column).is_not_null() & (pl.col("venue_count") >= min_venues))
        return [float(v) for v in rows[column].to_list() if math.isfinite(v)]

    by_status: dict[str, list[str]] = {}
    for row in health.iter_rows(named=True):
        by_status.setdefault(row["status"], []).append(row["venue_slug"])

    overview = {
        "generated_at": to_iso(utc_now()),
        "generation_id": manifest.generation_id,
        "snapshot_effective_time": manifest.snapshot_effective_time,
        "published_at": manifest.published_at,
        "counts": {
            "assets": manifest.asset_count,
            "instruments": manifest.instrument_count,
            "venues_contributing": manifest.venue_count,
        },
        "qualified_assets": {
            "multi_venue": market.filter(pl.col("venue_count") >= 2).height,
            "single_venue": market.filter(pl.col("venue_count") == 1).height,
        },
        "reported_volume_24h_usd": {
            "total": sum(numeric("reported_volume_24h_usd")) or None,
            "spot": sum(numeric("volume__reported_spot_volume_24h_usd")) or None,
            "perpetual": sum(numeric("volume__reported_perp_volume_24h_usd")) or None,
            "note": "Reported by venues; not independently verified.",
        },
        "median_dispersion_bps_multi_venue": _median(
            numeric("dispersion__price_dispersion_bps", min_venues=2)
        ),
        "top_reported_volume": _top(market, "reported_volume_24h_usd", 10, min_venues=2),
        "top_price_dispersion": _top(market, "dispersion__price_dispersion_bps", 10, min_venues=3),
        "top_venue_count": _top(market, "venue_count", 10),
        "system": {
            "venues_by_status": by_status,
            "history_note": (
                "Windowed metrics (percentiles, changes, volatility) need accumulated "
                "history and stay empty until enough generations exist."
            ),
        },
    }
    _write(out_dir / "market-overview.json", overview)

    listed = market.filter(pl.col("venue_count") >= 1)
    rows: list[dict[str, Any]] = []
    detail_ids = {
        r["asset_id"]
        for r in market.sort(
            ["reported_volume_24h_usd", "asset_id"], descending=[True, False], nulls_last=True
        )
        .head(detail_assets)
        .iter_rows(named=True)
    }
    for record in listed.iter_rows(named=True):
        row = {key: record.get(col) for key, col in _ASSET_LIST_COLUMNS.items()}
        row["file"] = (
            asset_file_name(record["asset_id"], record["symbol"])
            if record["asset_id"] in detail_ids
            else None
        )
        rows.append(row)
    _write(out_dir / "assets.json", {"generation_id": manifest.generation_id, "assets": rows})

    for record in market.filter(pl.col("asset_id").is_in(list(detail_ids))).iter_rows(named=True):
        venues = venue_state.filter(pl.col("asset_id") == record["asset_id"]).sort("venue_slug")
        detail = {
            "generation_id": manifest.generation_id,
            "state": {k: v for k, v in record.items() if v is not None},
            "venues": venues.to_dicts(),
        }
        _write(out_dir / "asset" / asset_file_name(record["asset_id"], record["symbol"]), detail)

    registry = {v.slug: v for v in load_exchange_registry().venues}
    venues_payload = []
    for row in health.iter_rows(named=True):
        venue = registry.get(row["venue_slug"])
        venues_payload.append(
            {
                **{
                    k: row[k]
                    for k in (
                        "venue_slug",
                        "status",
                        "duration_ms",
                        "instrument_count",
                        "ticker_count",
                        "derivative_observation_count",
                        "order_book_count",
                        "parse_failures",
                        "http_errors",
                        "timeouts",
                        "rate_limit_responses",
                    )
                },
                "display_name": venue.display_name if venue else row["venue_slug"],
                "terms_url": venue.terms_url if venue else None,
                "attribution": venue.attribution if venue else None,
            }
        )
    disabled = [
        {"venue_slug": v.slug, "display_name": v.display_name, "reason": v.disabled_reason}
        for v in load_exchange_registry().disabled()
    ]
    _write(out_dir / "venues.json", {"venues": venues_payload, "disabled": disabled})

    _write(
        out_dir / "dataset.json",
        {
            "generation_id": manifest.generation_id,
            "schema_version": manifest.schema_version,
            "methodology_version": manifest.methodology_version,
            "software_version": manifest.software_version,
            "dataset_format_version": manifest.dataset_format_version,
            "published_at": manifest.published_at,
            "total_bytes": manifest.total_bytes,
            "files": [
                {
                    "filename": f.filename,
                    "size_bytes": f.size_bytes,
                    "sha256": f.sha256,
                    "rows": f.row_count,
                    "table": f.table,
                }
                for f in manifest.files
            ],
        },
    )

    sizes = {p.relative_to(out_dir).as_posix(): p.stat().st_size for p in out_dir.rglob("*.json")}
    if sum(sizes.values()) > MAX_EXPORT_BYTES:
        raise ValidationFailure(
            "web export exceeds its size budget",
            total_bytes=sum(sizes.values()),
            budget_bytes=MAX_EXPORT_BYTES,
        )
    return sizes
