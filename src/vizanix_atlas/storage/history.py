"""The history store: what the windowed metrics read.

Volatility, returns, technical indicators, the funding and basis percentiles, the
pressure and crowding components and the Market Genome all need Atlas's *own past*, and a
single generation has none. This module keeps that past in one small Parquet file that is
extended after every published generation and read before the next one is built.

What it holds, per tracked asset and per snapshot: the reference price, the 8-hour
funding, the perpetual basis, open interest and reported volume, which is exactly the set
of inputs those metrics take.

Honesty rules, carried over from ``docs/METHODOLOGY.md``:

- **Nothing is invented.** A snapshot that did not happen is a gap. There is no
  back-filling and no interpolation, and the windowed metrics' own coverage gates
  (``config/collection.yaml``, ``history``) then withhold a value rather than publish one
  from a sparse window.
- **No look-ahead.** The store is read as of the generation being built; a row at or after
  that generation's time (a re-run of the same slot) is ignored.
- **Bounded.** Only the most liquid assets are tracked, and rows older than the retention
  period are dropped, so the file stays a few megabytes however long Atlas runs.
"""

from __future__ import annotations

import io
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import polars as pl

from vizanix_atlas.analytics.series import PriceSeries, SeriesPoint
from vizanix_atlas.core.config import HistoryStoreConfig
from vizanix_atlas.core.errors import SchemaMismatch

if TYPE_CHECKING:
    from vizanix_atlas.discovery.pipeline import Generation

#: Two alternating assets, so replacing one can never leave Atlas with no history: the other
#: still holds the previous complete state.
STORE_SLOTS: Final = ("history-a.parquet", "history-b.parquet")

_HOUR_MS: Final = 3_600_000
_SLOT_MS: Final = 15 * 60_000

#: The columns of the store, in order.
SCHEMA: Final = {
    "asset_id": pl.Utf8,
    "observed_at": pl.Int64,
    "reference_price": pl.Float64,
    "funding_8h": pl.Float64,
    "basis_bps": pl.Float64,
    "open_interest_usd": pl.Float64,
    "volume_usd": pl.Float64,
}

#: A 24-hour baseline needs at least this many snapshots to be a baseline at all.
_MIN_BASELINE_POINTS: Final = 16

#: How far from exactly 24 hours ago a snapshot may be and still count as "24 hours ago".
_NEAREST_TOLERANCE_MS: Final = 3 * _SLOT_MS


@dataclass(frozen=True, slots=True)
class AssetHistory:
    """One asset's history as the state builder consumes it."""

    prices: PriceSeries
    funding_8h: tuple[float, ...] = ()
    basis_bps: tuple[float, ...] = ()
    open_interest_previous_usd: float | None = None
    open_interest_24h_ago_usd: float | None = None
    volume_baseline_usd: float | None = None


@dataclass(frozen=True, slots=True)
class HistoryStore:
    """An immutable snapshot of the history, with operations returning new snapshots."""

    frame: pl.DataFrame

    # ------------------------------------------------------------------ construction

    @classmethod
    def empty(cls) -> HistoryStore:
        """A store with no history, which is what Atlas starts from."""
        return cls(pl.DataFrame(schema=SCHEMA))

    @classmethod
    def from_bytes(cls, data: bytes) -> HistoryStore:
        """Parse a stored file.

        Raises:
            SchemaMismatch: If the file is not a history store, so a corrupt or foreign
                asset is refused rather than silently treated as empty history.

        """
        try:
            frame = pl.read_parquet(io.BytesIO(data))
        except Exception as error:
            raise SchemaMismatch("the history file is not valid Parquet") from error
        missing = set(SCHEMA) - set(frame.columns)
        if missing:
            raise SchemaMismatch("the history file lacks columns", missing=sorted(missing))
        return cls(frame.select(list(SCHEMA)).cast(SCHEMA))  # type: ignore[arg-type]

    def to_bytes(self) -> bytes:
        """Serialise the store, sorted so equal histories produce equal bytes."""
        buffer = io.BytesIO()
        self.frame.sort(["asset_id", "observed_at"]).write_parquet(
            buffer, compression="zstd", compression_level=9, statistics=False
        )
        return buffer.getvalue()

    # ---------------------------------------------------------------------- queries

    @property
    def row_count(self) -> int:
        """Total rows held."""
        return self.frame.height

    @property
    def last_observed_at(self) -> int | None:
        """The newest snapshot time held, or ``None`` for an empty store."""
        value = self.frame["observed_at"].max() if self.frame.height else None
        return value if isinstance(value, int) else None

    @property
    def asset_count(self) -> int:
        """Distinct assets tracked."""
        return int(self.frame["asset_id"].n_unique()) if self.frame.height else 0

    def view(self, *, as_of: int) -> dict[str, AssetHistory]:
        """Return each asset's history strictly before ``as_of``, ready for the state builder."""
        if not self.frame.height:
            return {}
        past = self.frame.filter(pl.col("observed_at") < as_of).sort(["asset_id", "observed_at"])
        views: dict[str, AssetHistory] = {}
        for key, rows in past.partition_by("asset_id", as_dict=True).items():
            asset_id = str(key[0] if isinstance(key, tuple) else key)
            views[asset_id] = _asset_history(rows, as_of=as_of)
        return views

    # ------------------------------------------------------------------- extension

    def extended_with(
        self,
        generation: Generation,
        *,
        config: HistoryStoreConfig,
    ) -> HistoryStore:
        """Return the store extended with one published generation, then trimmed.

        Assets are tracked while they rank among the ``max_assets`` most liquid, plus a
        grace period after dropping out. Rows older than the retention are dropped.
        """
        observed_at = generation.snapshot_effective_time
        rows = _rows_from_generation(generation, observed_at)
        incoming = pl.DataFrame(rows, schema=SCHEMA, orient="row")

        # Rank this snapshot's assets by reported volume and keep the top ones.
        ranked = (
            incoming.filter(pl.col("reference_price").is_not_null())
            .sort("volume_usd", descending=True, nulls_last=True)
            .head(config.max_assets)["asset_id"]
        )
        top = set(ranked.to_list())

        combined = pl.concat(
            [self.frame, incoming.filter(pl.col("asset_id").is_in(list(top)))]
        ).unique(subset=["asset_id", "observed_at"], keep="last", maintain_order=True)
        grace_cutoff = observed_at - config.grace_hours * _HOUR_MS
        recent = set(
            combined.filter(pl.col("observed_at") >= grace_cutoff)["asset_id"].unique().to_list()
        )
        retention_cutoff = observed_at - config.retention_hours * _HOUR_MS
        trimmed = combined.filter(
            (pl.col("observed_at") >= retention_cutoff) & pl.col("asset_id").is_in(list(recent))
        )
        return HistoryStore(trimmed)


def _rows_from_generation(generation: Generation, observed_at: int) -> list[tuple[object, ...]]:
    """Flatten a generation's market states into history rows."""
    rows: list[tuple[object, ...]] = []
    for bundle in generation.bundles:
        state = bundle.state
        price = state.reference_price.value
        if price is None:
            continue
        rows.append(
            (
                state.asset_id,
                observed_at,
                price,
                state.funding.rate_8h_median,
                state.basis.perp_basis_bps_median,
                state.open_interest.total_usd,
                state.volume.reported_volume_24h_usd,
            )
        )
    return rows


def _asset_history(rows: pl.DataFrame, *, as_of: int) -> AssetHistory:
    """Build one asset's :class:`AssetHistory` from its rows before ``as_of``."""
    times = rows["observed_at"].to_list()
    prices = rows["reference_price"].to_list()
    points = tuple(
        SeriesPoint(observed_at=int(t), reference_price=float(p))
        for t, p in zip(times, prices, strict=True)
        if p is not None and p > 0
    )

    thirty_days = as_of - 720 * _HOUR_MS
    window = rows.filter(pl.col("observed_at") >= thirty_days)
    funding = tuple(float(v) for v in window["funding_8h"].drop_nulls().to_list())
    basis = tuple(float(v) for v in window["basis_bps"].drop_nulls().to_list())

    oi = rows["open_interest_usd"].to_list()
    previous_oi = oi[-1] if oi and (as_of - times[-1]) <= 2 * _SLOT_MS else None

    return AssetHistory(
        prices=PriceSeries(points=points),
        funding_8h=funding,
        basis_bps=basis,
        open_interest_previous_usd=previous_oi,
        open_interest_24h_ago_usd=_nearest(times, oi, target=as_of - 24 * _HOUR_MS),
        volume_baseline_usd=_median_since(
            times, rows["volume_usd"].to_list(), since=as_of - 24 * _HOUR_MS
        ),
    )


def _nearest(times: list[int], values: list[float | None], *, target: int) -> float | None:
    """Return the value whose time is closest to ``target``, if within tolerance."""
    best: tuple[int, float] | None = None
    for t, value in zip(times, values, strict=True):
        if value is None:
            continue
        distance = abs(t - target)
        if distance <= _NEAREST_TOLERANCE_MS and (best is None or distance < best[0]):
            best = (distance, float(value))
    return best[1] if best else None


def _median_since(times: list[int], values: list[float | None], *, since: int) -> float | None:
    """Return the median of the non-null values since ``since``, or ``None`` if too few."""
    window = [float(v) for t, v in zip(times, values, strict=True) if t >= since and v is not None]
    return statistics.median(window) if len(window) >= _MIN_BASELINE_POINTS else None
