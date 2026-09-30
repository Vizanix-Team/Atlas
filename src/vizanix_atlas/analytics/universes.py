"""Market universes.

Breadth over "every asset" is misleading when most listings are inactive: a market where
three thousand microcaps drifted down and BTC rose is not a market where 99% of assets
fell in any meaningful sense.

So every market-wide figure Atlas publishes names the universe it was computed over, and
each universe is defined by a deterministic rule in ``config/collection.yaml``. The rules
are applied here, and the criteria text travels with the published figures.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Final

from vizanix_atlas.core.config import CollectionConfig, UniverseConfig
from vizanix_atlas.core.errors import ConfigurationError
from vizanix_atlas.models.enums import Universe
from vizanix_atlas.models.state import MarketState

#: Ranking metrics a universe may order by, mapped to a reader on ``MarketState``.
_RANKERS: Final[dict[str, Callable[[MarketState], float | None]]] = {
    "observable_depth_50bps_usd": lambda state: (
        band.total_depth_usd if (band := state.liquidity.band(50)) is not None else None
    ),
    "reported_volume_24h_usd": lambda state: state.volume.reported_volume_24h_usd,
}


def _meets_thresholds(state: MarketState, config: UniverseConfig) -> bool:
    """Return whether a state satisfies a universe's numeric thresholds."""
    if state.venue_count < config.min_venue_count:
        return False
    if config.min_reported_volume_24h_usd > 0:
        volume = state.volume.reported_volume_24h_usd
        if volume is None or volume < config.min_reported_volume_24h_usd:
            return False
    return True


def select_universe(
    states: Sequence[MarketState], universe: Universe, config: CollectionConfig
) -> tuple[MarketState, ...]:
    """Return the states belonging to ``universe``.

    Selection is deterministic: filters are applied in a fixed order and ranked universes
    break ties by ``asset_id``, so the same generation always produces the same membership.

    Raises:
        ConfigurationError: If the universe is not declared in configuration.

    """
    declared = config.universes.get(universe.value)
    if declared is None:
        raise ConfigurationError(
            "universe is not declared in configuration",
            universe=universe.value,
            declared=sorted(config.universes),
        )

    # Only confidently resolved identities may enter a universe. An ambiguous asset is
    # not aggregated, so including it in a market-wide figure would contradict that.
    eligible = [s for s in states if s.resolution_state.value in ("resolved", "manual_override")]

    match universe:
        case Universe.ALL_RESOLVED:
            selected = eligible
        case Universe.MULTI_VENUE | Universe.LIQUID:
            selected = [s for s in eligible if _meets_thresholds(s, declared)]
        case Universe.SPOT_ONLY:
            selected = [
                s
                for s in eligible
                if s.structure.spot_venue_count > 0
                and s.structure.perp_venue_count == 0
                and s.structure.futures_venue_count == 0
            ]
        case Universe.DERIVATIVE_ACTIVE:
            selected = [
                s
                for s in eligible
                if s.structure.perp_venue_count > 0 or s.structure.futures_venue_count > 0
            ]
        case Universe.TOP_100_LIQUIDITY | Universe.TOP_500_LIQUIDITY:
            selected = _rank(eligible, declared)
        case _:  # pragma: no cover - the enum is exhaustive
            raise ConfigurationError("unhandled universe", universe=universe.value)

    return tuple(sorted(selected, key=lambda s: s.asset_id))


def _rank(states: Sequence[MarketState], config: UniverseConfig) -> list[MarketState]:
    """Return the top ``limit`` states by the configured ranking metric.

    Raises:
        ConfigurationError: If the ranking metric is not one Atlas can read.

    """
    if config.rank_by is None or config.limit is None:
        raise ConfigurationError(
            "a ranked universe requires both rank_by and limit", rank_by=config.rank_by
        )
    reader = _RANKERS.get(config.rank_by)
    if reader is None:
        raise ConfigurationError(
            "unsupported ranking metric",
            rank_by=config.rank_by,
            supported=sorted(_RANKERS),
        )

    scored = [(reader(state), state) for state in states]
    ranked = [(value, state) for value, state in scored if value is not None]
    # Descending by score, then by asset_id so ties are broken deterministically.
    ranked.sort(key=lambda pair: (-pair[0], pair[1].asset_id))
    return [state for _, state in ranked[: config.limit]]


def universe_criteria(universe: Universe, config: CollectionConfig) -> str:
    """Return the human-readable rule that defines ``universe``.

    Published alongside every market-wide figure, so a reader never has to guess what
    population a breadth number describes.
    """
    declared = config.universes.get(universe.value)
    return declared.criteria if declared else "undeclared universe"
