"""Which markets the live layer follows, and what each one means.

The live layer does not resolve identities itself. It borrows the latest published
generation's answer: an instrument is followed only if that generation mapped it to a
resolved asset, so a ticker that two different tokens share can never leak into a live
price (see ``docs/ASSET_RESOLUTION.md``). Quote currencies are handled the same way: a quote
is usable only when it is USD itself, or a stablecoin the generation says tracks USD and
whose observed rate is within parity bounds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import polars as pl

from vizanix_atlas.sdk.dataset import Dataset
from vizanix_atlas.stream.config import StreamConfig

#: The instrument classes the live layer understands, with the role each plays.
SPOT: str = "spot"
PERP_CLASSES: frozenset[str] = frozenset({"linear-perp", "inverse-perp"})

USD_ASSET_ID = "asset:fiat:usd"

_FIRM_STATES = frozenset({"resolved", "manual_override"})
#: Stablecoin parity bound. Matches ``quality.max_stablecoin_deviation_bps`` in
#: ``config/collection.yaml``: a rate further from 1.0 is more likely a broken market than
#: a real depeg, and pricing assets through it would corrupt every live figure.
_MAX_STABLE_DEVIATION_BPS = 500.0

InstrumentKey = tuple[str, str, str]
"""``(venue_slug, instrument_class, symbol_native)``."""


@dataclass(frozen=True, slots=True)
class AssetInfo:
    """What the live layer knows about one followed asset."""

    asset_id: str
    symbol: str
    name: str | None
    generation_price: float | None
    generation_volume_usd: float | None
    rank: int
    #: The generation's resolution state for this asset (`resolved` or `probable`).
    identity: str = "resolved"


@dataclass(frozen=True, slots=True)
class InstrumentRef:
    """A followed instrument: which asset it prices and in which quote currency."""

    asset_id: str
    quote_asset_id: str


@dataclass(slots=True)
class Universe:
    """The live layer's view of the world, rebuilt from each new generation."""

    generation_id: str
    assets: dict[str, AssetInfo] = field(default_factory=dict)
    instruments: dict[InstrumentKey, InstrumentRef] = field(default_factory=dict)
    #: USD value of one unit of each usable quote asset, at generation time.
    quote_rates: dict[str, float] = field(default_factory=dict)
    #: Instruments whose live price is a quote asset's USD rate (for example USDT-USD).
    rate_probes: dict[InstrumentKey, str] = field(default_factory=dict)

    def symbols(self, venue: str, instrument_class: str) -> list[str]:
        """Native symbols to subscribe for one feed, sorted for a stable subscription."""
        wanted = {
            key[2]
            for key in (*self.instruments, *self.rate_probes)
            if key[0] == venue and key[1] == instrument_class
        }
        return sorted(wanted)

    def symbol_signature(self) -> frozenset[InstrumentKey]:
        """The set of followed keys, to detect whether a refresh changes subscriptions."""
        return frozenset((*self.instruments, *self.rate_probes))


def _usable_rate(value: object) -> float | None:
    if not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        return None
    if abs(value - 1.0) * 10_000.0 > _MAX_STABLE_DEVIATION_BPS:
        return None
    return float(value)


def build_universe(dataset: Dataset, config: StreamConfig) -> Universe:
    """Build the universe from one published generation.

    Raises:
        DatasetUnavailable: If the generation cannot be read.

    """
    assets = pl.read_parquet(dataset.ensure_file("assets.parquet"))
    instruments = pl.read_parquet(
        dataset.ensure_file("instruments.parquet"),
        columns=[
            "venue_slug",
            "instrument_class",
            "symbol_native",
            "base_asset_id",
            "quote_asset_id",
            "active",
        ],
    )
    state = pl.concat(
        [
            pl.read_parquet(
                path,
                columns=[
                    "asset_id",
                    "reference_price__value",
                    "reported_volume_24h_usd",
                    "venue_count",
                ],
            )
            for path in dataset.ensure_shards()
        ],
        how="vertical_relaxed",
    )

    meta = assets.select(
        "asset_id", "symbol", "name", "resolution_state", "is_stablecoin", "tracks_asset_id"
    )
    joined = state.join(meta, on="asset_id", how="inner")

    # Quote currencies: USD itself, plus stablecoins the generation says track USD.
    quote_rates: dict[str, float] = {USD_ASSET_ID: 1.0}
    stable_ids: set[str] = set()
    for row in joined.filter(
        pl.col("is_stablecoin") & (pl.col("tracks_asset_id") == USD_ASSET_ID)
    ).iter_rows(named=True):
        rate = _usable_rate(row["reference_price__value"])
        if rate is not None:
            quote_rates[row["asset_id"]] = rate
            stable_ids.add(row["asset_id"])

    allowed = set(_FIRM_STATES)
    if config.universe.include_probable:
        allowed.add("probable")
    ranked = (
        joined.filter(
            pl.col("resolution_state").is_in(sorted(allowed))
            & ~pl.col("is_stablecoin")
            & (pl.col("venue_count") >= config.universe.min_venue_count)
            & pl.col("reference_price__value").is_not_null()
        )
        .sort(["reported_volume_24h_usd", "asset_id"], descending=[True, False], nulls_last=True)
        .head(config.universe.assets)
    )
    universe = Universe(generation_id=dataset.generation_id, quote_rates=quote_rates)
    for rank, row in enumerate(ranked.iter_rows(named=True), start=1):
        universe.assets[row["asset_id"]] = AssetInfo(
            asset_id=row["asset_id"],
            symbol=row["symbol"],
            name=row["name"],
            generation_price=row["reference_price__value"],
            generation_volume_usd=row["reported_volume_24h_usd"],
            rank=rank,
            identity="resolved"
            if row["resolution_state"] in _FIRM_STATES
            else row["resolution_state"],
        )

    live_classes = [SPOT, *sorted(PERP_CLASSES)]
    for row in instruments.filter(
        pl.col("active")
        & pl.col("instrument_class").is_in(live_classes)
        & pl.col("quote_asset_id").is_in(list(quote_rates))
    ).iter_rows(named=True):
        key = (row["venue_slug"], row["instrument_class"], row["symbol_native"])
        base = row["base_asset_id"]
        if base in universe.assets:
            universe.instruments[key] = InstrumentRef(base, row["quote_asset_id"])
        elif (
            base in stable_ids
            and row["instrument_class"] == SPOT
            and row["quote_asset_id"] == USD_ASSET_ID
        ):
            # A stablecoin quoted in USD: its live price is the live USD value of that quote.
            universe.rate_probes[key] = base
    return universe
