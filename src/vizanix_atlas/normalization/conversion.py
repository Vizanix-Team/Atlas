"""Quote-currency conversion.

Atlas does not assume ``USDT == USD``. The difference is small most of the time and
occasionally very large, and a system that hardcodes parity reports a depeg as a
market-wide price move.

Instead Atlas builds a graph from *observed* markets. Nodes are assets; edges are rates
a venue actually quoted. A rate to USD is found by shortest path, and every
USD-normalised figure carries the path, the rate, the source venue, the observation
time and whether the conversion was direct or derived (see ``docs/METHODOLOGY.md``).

Two guard rails:

- A stablecoin rate further from parity than the configured tolerance is refused. Beyond
  a certain distance the market is more likely broken than depegging, and converting
  through it would corrupt every downstream total. The deviation is published either
  way, so a real depeg is visible.
- No conversion means no USD figure. Atlas publishes null rather than a number resting
  on an unobserved rate.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from vizanix_atlas.core.atlas_time import to_epoch_ms, utc_now
from vizanix_atlas.core.config import QualityConfig
from vizanix_atlas.core.identifiers import fiat_asset_id
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.identity.resolver import AssetResolver
from vizanix_atlas.models.enums import ConversionMethod
from vizanix_atlas.models.observations import RawFxObservation
from vizanix_atlas.models.quality import QuoteConversion

_log = get_logger(__name__)

#: Everything is normalised to this.
USD_ASSET_ID: Final = fiat_asset_id("USD")

#: A conversion path longer than this is not published. Each hop multiplies the error
#: and widens the window over which the rate was sampled, so a long chain is not a
#: measurement worth standing behind.
_MAX_PATH_HOPS: Final = 3


@dataclass(frozen=True, slots=True)
class RateEdge:
    """One observed rate between two assets.

    Attributes:
        rate: Units of ``to_asset_id`` per one unit of ``from_asset_id``.
        observed_at: When the source observation was made, in epoch milliseconds.
        inverted: Whether this edge was derived by inverting an observed rate. An
            inverse is arithmetically exact, so it is not a weaker measurement, but it
            is recorded so provenance says what was quoted.

    """

    from_asset_id: str
    to_asset_id: str
    rate: float
    venue_slug: str
    observed_at: int
    inverted: bool = False

    @property
    def cost(self) -> float:
        """Path cost, used to prefer a better edge when several exist.

        A directly quoted edge is preferred over an inverted one, and among equals the
        shorter path wins. Cost is per hop rather than per rate, so the search finds the
        fewest, best-quoted hops rather than the numerically smallest product.
        """
        return 1.0 if not self.inverted else 1.05


@dataclass(slots=True)
class ConversionGraph:
    """Observed exchange rates between quote and settlement currencies.

    Built once per generation from every venue's FX observations.
    """

    quality: QualityConfig
    edges: dict[str, list[RateEdge]] = field(default_factory=dict)
    #: Rates for assets declared as stablecoins, so their deviation can be reported.
    _stablecoins: frozenset[str] = frozenset()
    _cache: dict[str, QuoteConversion | None] = field(default_factory=dict)

    def add_edge(self, edge: RateEdge) -> None:
        """Add an observed rate and its exact inverse.

        Both directions are stored because a venue quoting ``USDT/USD`` has told us the
        ``USD/USDT`` rate too, and requiring a separately quoted market for the reverse
        direction would leave most of the graph unreachable.
        """
        self.edges.setdefault(edge.from_asset_id, []).append(edge)
        if not edge.inverted:
            self.edges.setdefault(edge.to_asset_id, []).append(
                RateEdge(
                    from_asset_id=edge.to_asset_id,
                    to_asset_id=edge.from_asset_id,
                    rate=1.0 / edge.rate,
                    venue_slug=edge.venue_slug,
                    observed_at=edge.observed_at,
                    inverted=True,
                )
            )
        self._cache.clear()

    def to_usd(self, asset_id: str) -> QuoteConversion | None:
        """Return a conversion from ``asset_id`` to USD, or ``None`` if unavailable.

        ``None`` means no USD-normalised figure may be published for anything quoted in
        this currency. That is the intended outcome: a missing total is honest, a
        fabricated one is not.
        """
        if asset_id in self._cache:
            return self._cache[asset_id]
        conversion = self._compute(asset_id)
        self._cache[asset_id] = conversion
        return conversion

    def _compute(self, asset_id: str) -> QuoteConversion | None:
        """Find the best path from ``asset_id`` to USD."""
        if asset_id == USD_ASSET_ID:
            # USD is USD. This is the only place a rate of 1.0 is assumed, and it is
            # not an assumption about a stablecoin.
            return QuoteConversion(
                from_asset_id=asset_id,
                to_asset_id=USD_ASSET_ID,
                rate=1.0,
                method=ConversionMethod.UNIT_ASSUMED,
                observed_at=to_epoch_ms(utc_now()),
                path=(asset_id,),
                deviation_from_parity_bps=0.0,
            )

        path = self._shortest_path(asset_id)
        if path is None:
            return QuoteConversion(
                from_asset_id=asset_id,
                to_asset_id=USD_ASSET_ID,
                rate=None,
                method=ConversionMethod.UNAVAILABLE,
            )

        rate = 1.0
        oldest = None
        venues: list[str] = []
        for edge in path:
            rate *= edge.rate
            oldest = edge.observed_at if oldest is None else min(oldest, edge.observed_at)
            venues.append(edge.venue_slug)

        deviation_bps = (rate - 1.0) * 10_000.0
        # A stablecoin whose observed rate is far from parity is refused rather than
        # used. The deviation is still reported so a genuine depeg is visible.
        if (
            asset_id in self._stablecoins
            and abs(deviation_bps) > self.quality.max_stablecoin_deviation_bps
        ):
            _log.warning(
                "refusing a stablecoin conversion that is too far from parity",
                extra={
                    "asset_id": asset_id,
                    "rate": rate,
                    "deviation_bps": round(deviation_bps, 1),
                    "tolerance_bps": self.quality.max_stablecoin_deviation_bps,
                },
            )
            return QuoteConversion(
                from_asset_id=asset_id,
                to_asset_id=USD_ASSET_ID,
                rate=None,
                method=ConversionMethod.UNAVAILABLE,
                deviation_from_parity_bps=deviation_bps,
            )

        method = (
            ConversionMethod.DIRECT_OBSERVED
            if len(path) == 1
            else ConversionMethod.DERIVED_VIA_INTERMEDIARY
        )
        return QuoteConversion(
            from_asset_id=asset_id,
            to_asset_id=USD_ASSET_ID,
            rate=rate,
            method=method,
            observed_at=oldest,
            source_venue_slug=venues[0] if venues else None,
            path=(asset_id, *[edge.to_asset_id for edge in path]),
            deviation_from_parity_bps=deviation_bps if asset_id in self._stablecoins else None,
        )

    def _shortest_path(self, source: str) -> list[RateEdge] | None:
        """Dijkstra from ``source`` to USD over the observed rate graph.

        Deterministic: ties are broken by the edge's venue and target, so the same
        observations always produce the same path and therefore the same published rate.
        """
        best: dict[str, float] = {source: 0.0}
        queue: list[tuple[float, int, str, list[RateEdge]]] = [(0.0, 0, source, [])]
        counter = 0

        while queue:
            cost, _, node, path = heapq.heappop(queue)
            if node == USD_ASSET_ID:
                return path
            if len(path) >= _MAX_PATH_HOPS:
                continue
            if cost > best.get(node, float("inf")):
                continue
            for edge in sorted(
                self.edges.get(node, ()), key=lambda e: (e.cost, e.venue_slug, e.to_asset_id)
            ):
                next_cost = cost + edge.cost
                if next_cost < best.get(edge.to_asset_id, float("inf")):
                    best[edge.to_asset_id] = next_cost
                    counter += 1
                    heapq.heappush(queue, (next_cost, counter, edge.to_asset_id, [*path, edge]))
        return None

    def edge_count(self) -> int:
        """Total edges, including the derived inverses."""
        return sum(len(edges) for edges in self.edges.values())

    def reachable_assets(self) -> tuple[str, ...]:
        """Return every asset that has a usable USD conversion, sorted."""
        return tuple(
            sorted(
                asset_id
                for asset_id in self.edges
                if (conversion := self.to_usd(asset_id)) is not None and conversion.usable
            )
        )


def build_conversion_graph(
    observations: Iterable[RawFxObservation],
    resolver: AssetResolver,
    quality: QualityConfig,
) -> ConversionGraph:
    """Build the conversion graph from every venue's FX observations.

    Observations are resolved through the same identity layer as everything else, so
    that a venue's own code for Tether lands on the same asset as every other venue's.
    """
    graph = ConversionGraph(quality=quality)
    object.__setattr__(graph, "_stablecoins", frozenset(resolver.stablecoin_asset_ids()))

    added = 0
    for observation in observations:
        from_id = resolver.asset_id_for(observation.venue_slug, observation.from_symbol_native)
        to_id = resolver.asset_id_for(observation.venue_slug, observation.to_symbol_native)
        if from_id == to_id:
            continue
        graph.add_edge(
            RateEdge(
                from_asset_id=from_id,
                to_asset_id=to_id,
                rate=observation.rate,
                venue_slug=observation.venue_slug,
                observed_at=observation.timing.best_effective_time,
            )
        )
        added += 1

    _log.info(
        "conversion graph built",
        extra={
            "observations": added,
            "edges": graph.edge_count(),
            "nodes": len(graph.edges),
        },
    )
    return graph
