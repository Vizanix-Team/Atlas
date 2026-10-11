"""The order-book plan: which markets get a book snapshot, decided from the last generation.

Tier B (see ``config/collection.yaml``) samples order books for a deterministic, liquid
universe. Choosing that universe needs a view across every venue, which exists only after a
generation is built, while the books themselves must be fetched by each venue's collector
(from its own network) during the *next* run. The plan bridges the two:

1. After a generation is published, :func:`plan_order_books` ranks (venue, asset) markets
   by reported volume and applies the selection rules from configuration.
2. The plan is published as a small ``books-plan.json`` asset next to ``latest.json``.
3. The next run's collectors read it (:func:`load_books_plan`) and fetch those books in the
   same pass as their tier A data, whether a runner or the relay host does the collecting.

A plan is therefore one generation old. That is deliberate: it is stable, needs no
coordination between collectors, and a missing or unreadable plan degrades to tier A only
rather than failing a run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from vizanix_atlas.core.atlas_time import to_epoch_ms, utc_now
from vizanix_atlas.core.config import CollectionConfig
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.discovery.tiers import SelectionCandidate, select_for_books
from vizanix_atlas.models.base import AtlasModel
from vizanix_atlas.models.enums import InstrumentType
from vizanix_atlas.models.instrument import Instrument

if TYPE_CHECKING:
    from collections.abc import Collection

    from vizanix_atlas.discovery.pipeline import Generation

_log = get_logger(__name__)

#: The asset name inside the ``data-latest`` release.
PLAN_FILENAME: Final = "books-plan.json"

#: Quote currencies in the order a book is preferred when a venue lists several markets.
_QUOTE_PREFERENCE: Final = ("USDT", "USD", "USDC")

#: Hard cap on a downloaded plan. A real plan is a few tens of kilobytes.
_MAX_PLAN_BYTES: Final = 2 * 1024 * 1024


class BooksPlan(AtlasModel):
    """Native symbols to snapshot, per venue, for one run."""

    generation_id: str = Field(description="The generation this plan was derived from.")
    generated_at: int
    depth: int = Field(gt=0, description="Order-book levels to request.")
    venues: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @property
    def total(self) -> int:
        """Total books requested across venues."""
        return sum(len(symbols) for symbols in self.venues.values())

    def symbols_for(self, venue_slug: str) -> tuple[str, ...]:
        """Return the venue's native symbols, or an empty tuple."""
        return self.venues.get(venue_slug, ())


def plan_order_books(
    generation: Generation,
    *,
    config: CollectionConfig,
    venues_with_books: Collection[str],
) -> BooksPlan:
    """Choose the markets to snapshot next run, from a built generation.

    For every (venue, asset) the venue quotes, the venue's most representative market is
    chosen: a spot market if it has one (preferring USDT, then USD, then USDC quotes), else
    a perpetual. Those candidates are ranked by the venue's reported volume for the asset and
    passed through the configured selection budgets.

    Args:
        generation: A generation that has just been built.
        config: Collection policy, which holds the budgets.
        venues_with_books: Venue slugs whose adapters can fetch an order book.

    """
    allowed = set(venues_with_books)
    markets: dict[tuple[str, str], list[Instrument]] = {}
    for instrument in generation.instruments:
        if (
            instrument.active
            and instrument.venue_slug in allowed
            and instrument.instrument_type in (InstrumentType.SPOT, InstrumentType.PERPETUAL)
        ):
            markets.setdefault((instrument.venue_slug, instrument.base_asset_id), []).append(
                instrument
            )

    candidates: list[SelectionCandidate] = []
    for bundle in generation.bundles:
        priced_venues = {v.venue_slug for v in bundle.venues if v.price_usd is not None}
        asset_volume = bundle.state.volume.reported_volume_24h_usd
        for venue_row in bundle.venues:
            options = markets.get((venue_row.venue_slug, venue_row.asset_id))
            if not options:
                continue
            candidates.append(
                SelectionCandidate(
                    instrument=min(options, key=_preference),
                    # A venue whose bulk ticker has no quote volume (Coinbase, Kraken and
                    # Bitstamp among them) would otherwise never be sampled, although it is
                    # among the most-watched. For *selection only*, such a venue is ranked by
                    # the asset's total reported volume. The figure is not published.
                    reported_volume_usd=(
                        venue_row.reported_volume_24h_usd
                        if venue_row.reported_volume_24h_usd is not None
                        else asset_volume
                    ),
                    asset_venue_count=len(priced_venues),
                )
            )

    selected = select_for_books(candidates, config.tiers.b_liquidity.selection)
    return BooksPlan(
        generation_id=generation.generation_id,
        generated_at=to_epoch_ms(utc_now()),
        depth=config.tiers.b_liquidity.order_book_depth_levels,
        venues=selected,
    )


def _preference(instrument: Instrument) -> tuple[int, int, str]:
    """Rank a venue's markets for one asset: spot first, then preferred quote, then symbol."""
    quote = instrument.quote_asset_id.rsplit(":", 1)[-1].upper()
    quote_rank = (
        _QUOTE_PREFERENCE.index(quote) if quote in _QUOTE_PREFERENCE else len(_QUOTE_PREFERENCE)
    )
    return (
        0 if instrument.instrument_type is InstrumentType.SPOT else 1,
        quote_rank,
        instrument.symbol_native,
    )


def load_books_plan(
    location: str | None, *, client: httpx.Client | None = None
) -> BooksPlan | None:
    """Read a plan from a local file or an https URL, or ``None`` if there is no usable one.

    Never raises: a missing, unreachable or malformed plan means "no books this run", which
    is the correct fallback on the first run and whenever the plan asset is unavailable.
    """
    if not location:
        return None
    try:
        if urlsplit(location).scheme in ("http", "https"):
            return _load_remote(location, client)
        return BooksPlan.model_validate_json(Path(location).read_text(encoding="utf-8"))
    except (OSError, ValueError, httpx.HTTPError) as error:
        _log.info(
            "no usable order-book plan",
            extra={"location": location, "reason": type(error).__name__},
        )
        return None


def _load_remote(url: str, client: httpx.Client | None) -> BooksPlan | None:
    """Download a plan over https, with a size cap."""
    if urlsplit(url).scheme != "https":
        raise ValueError("the plan URL must be https")
    http = client or httpx.Client(timeout=15.0, follow_redirects=True)
    try:
        response = http.get(url)
    finally:
        if client is None:
            http.close()
    if response.status_code == 404:
        return None
    response.raise_for_status()
    if len(response.content) > _MAX_PLAN_BYTES:
        raise ValueError("the plan exceeds the size limit")
    return BooksPlan.model_validate(json.loads(response.content))
