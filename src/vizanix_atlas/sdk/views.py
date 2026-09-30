"""Read-only views over a downloaded asset-market-state row.

Atlas's schema is nested (``MarketState.reference_price.value``) but Parquet rows are
flat (``reference_price__value``, see ``vizanix_atlas.storage.flatten``). This module
is the SDK's side of that same bridge: :class:`AssetView` exposes a flat row both by
its MQL metric name (``asset.reference_price``) and, via :meth:`AssetView.state`, as a
nested read-only tree that mirrors the published schema's shape, without re-validating
it as a full :class:`~vizanix_atlas.models.state.MarketState` (a downloaded row may
legitimately be missing fields a strict model would require, and the SDK's job here is
to show a client what was published, not to re-derive it).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import polars as pl

from vizanix_atlas.mql.columns import ASSET_TABLE_COLUMNS

_SEPARATOR = "__"


def _unflatten(row: dict[str, Any]) -> SimpleNamespace:
    """Turn a flat ``parent__child`` row into a nested :class:`SimpleNamespace` tree."""
    tree: dict[str, Any] = {}
    for column, value in row.items():
        parts = column.split(_SEPARATOR)
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def _build(node: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            **{k: _build(v) if isinstance(v, dict) else v for k, v in node.items()}
        )

    return _build(tree)


class AssetView:
    """One asset's published market state, as downloaded.

    Attributes are readable both by MQL metric name (``view.reference_price``,
    ``view.venue_count``) and via :meth:`state` for the nested, schema-shaped view.
    """

    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    @property
    def asset_id(self) -> str:
        """The asset's canonical identifier."""
        return str(self._row["asset_id"])

    @property
    def symbol(self) -> str:
        """The asset's ticker symbol, as published."""
        return str(self._row.get("symbol", ""))

    def raw(self) -> dict[str, Any]:
        """Return the complete underlying flat row, unmodified."""
        return dict(self._row)

    def state(self) -> SimpleNamespace:
        """Return the row unflattened into a nested tree mirroring the published schema.

        For example ``asset.state().reference_price.value`` for the column
        ``reference_price__value``.
        """
        return _unflatten(self._row)

    def __getattr__(self, name: str) -> Any:
        physical = ASSET_TABLE_COLUMNS.get(name)
        if physical is None or physical not in self._row:
            raise AttributeError(f"{name!r} is not a published metric on this asset")
        return self._row[physical]

    def __repr__(self) -> str:
        return f"AssetView(asset_id={self.asset_id!r}, symbol={self.symbol!r})"


class MarketView:
    """A summary over every asset in a generation.

    Built from an already-loaded Polars frame spanning every shard; see
    :meth:`vizanix_atlas.sdk.client.Atlas.market`.
    """

    def __init__(self, frame: pl.DataFrame) -> None:
        self._frame = frame

    @property
    def asset_count(self) -> int:
        """The number of assets in this view."""
        return int(self._frame.height)

    def qualified_asset_count(self, *, min_venue_count: int = 1) -> int:
        """Assets observed on at least ``min_venue_count`` venues."""
        return int(self._frame.filter(self._frame["venue_count"] >= min_venue_count).height)

    def top_by_volume(self, n: int = 10, *, min_venues: int = 1) -> pl.DataFrame:
        """Return the ``n`` assets with the highest reported 24h USD volume.

        ``min_venues`` keeps a single venue's self-reported volume from defining the top
        of the list; the website and CLI default to 2.
        """
        eligible = self._frame.filter(pl.col("venue_count") >= min_venues)
        return eligible.sort("reported_volume_24h_usd", descending=True, nulls_last=True).head(n)

    def frame(self) -> pl.DataFrame:
        """Return the underlying Polars frame, for anything this view does not wrap."""
        return self._frame
