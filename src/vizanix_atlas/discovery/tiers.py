"""Coverage tier selection.

Tier A covers every instrument on every venue, using bulk endpoints, so it costs a few
dozen requests for the whole market. Tiers B and C sample order books, which cost one
request per instrument, so they must be selective.

Selection is **deterministic and documented**: the same market produces the same universe
every run, and the rule is in ``config/collection.yaml`` rather than in this code. A
consumer can therefore tell whether an asset's missing depth metric means "thin market" or
"not selected", because the tier reached is published on every state row.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from vizanix_atlas.core.config import TierSelection
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.models.enums import InstrumentType
from vizanix_atlas.models.instrument import Instrument

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SelectionCandidate:
    """An instrument eligible for order-book sampling."""

    instrument: Instrument
    #: Reported USD volume, used to rank. ``None`` ranks last rather than excluding.
    reported_volume_usd: float | None
    #: Venues quoting this instrument's base asset, used as a liquidity proxy.
    asset_venue_count: int

    @property
    def rank_key(self) -> tuple[float, str]:
        """Descending volume, then instrument ID so ties break deterministically."""
        return (-(self.reported_volume_usd or 0.0), self.instrument.instrument_id)


def select_for_books(
    candidates: Sequence[SelectionCandidate], selection: TierSelection
) -> dict[str, tuple[str, ...]]:
    """Choose which instruments to sample order books for, grouped by venue.

    Four budgets apply in order, and each exists for a reason:

    ``min_reported_volume_24h_usd`` and ``min_venue_count``
        Exclude markets where a book snapshot would measure nothing useful.
    ``max_books_per_asset``
        Stop the budget being spent on a tenth BTC book when a thousand other assets have
        none. Breadth is worth more than a marginal venue on an already well-covered asset.
    ``max_instruments_per_venue``
        Keep any one venue from consuming the whole budget, and keep per-venue request
        counts inside their rate limits.
    ``max_instruments_total``
        Bound the whole tier so the job finishes inside its timeout.

    Returns:
        A mapping of venue slug to the venue's selected native symbols, both sorted.

    """
    eligible = [
        candidate
        for candidate in candidates
        if candidate.asset_venue_count >= selection.min_venue_count
        and (candidate.reported_volume_usd or 0.0) >= selection.min_reported_volume_24h_usd
    ]

    always = {a.upper() for a in selection.always_include_assets}
    if always:
        eligible.extend(
            candidate
            for candidate in candidates
            if candidate.instrument.base_asset_id.upper() in always and candidate not in eligible
        )

    eligible.sort(key=lambda candidate: candidate.rank_key)

    per_asset: dict[str, int] = {}
    per_venue: dict[str, list[str]] = {}
    chosen = 0

    for candidate in eligible:
        if chosen >= selection.max_instruments_total > 0:
            break
        asset_id = candidate.instrument.base_asset_id
        venue = candidate.instrument.venue_slug

        if 0 < selection.max_books_per_asset <= per_asset.get(asset_id, 0):
            continue
        if 0 < selection.max_instruments_per_venue <= len(per_venue.get(venue, ())):
            continue

        per_venue.setdefault(venue, []).append(candidate.instrument.symbol_native)
        per_asset[asset_id] = per_asset.get(asset_id, 0) + 1
        chosen += 1

    _log.info(
        "order-book universe selected",
        extra={
            "eligible": len(eligible),
            "selected": chosen,
            "venues": len(per_venue),
            "assets": len(per_asset),
        },
    )
    return {venue: tuple(sorted(symbols)) for venue, symbols in sorted(per_venue.items())}


def build_candidates(
    instruments: Sequence[Instrument],
    *,
    volume_by_instrument: dict[str, float | None],
    venues_by_asset: dict[str, int],
) -> list[SelectionCandidate]:
    """Build selection candidates from normalised instruments.

    Options are excluded: a single strike's book says little about the underlying's
    liquidity, and there are thousands of them. Option analytics come from the venue's own
    bulk book summaries instead, which cost no per-strike requests.
    """
    return [
        SelectionCandidate(
            instrument=instrument,
            reported_volume_usd=volume_by_instrument.get(instrument.instrument_id),
            asset_venue_count=venues_by_asset.get(instrument.base_asset_id, 0),
        )
        for instrument in instruments
        if instrument.active and instrument.instrument_type is not InstrumentType.OPTION
    ]
