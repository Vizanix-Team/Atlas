"""The live book: merged venue quotes and the reference price derived from them.

The live reference price follows the same method as the dataset's (docs/METHODOLOGY.md): a
**weighted median**, with each venue's weight the square root of its reported USD volume,
scaled by freshness and by whether the quote is a live two-sided mid or a last trade, and
capped so no venue holds more than the configured share. Two things differ, on purpose:

- Time is continuous. A venue's quote ages from the moment it last moved; there is no
  slot, so there is no "stale by two slots".
- Outliers are rejected against the live centre with a tighter bound, because a live view
  has no later generation to correct a bad instant before someone reads it.

The book never invents a price. An asset with no usable venue quote has no live price, and
says so by being absent from the snapshot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from vizanix_atlas.analytics.robust import cap_weights, weighted_mad, weighted_median
from vizanix_atlas.core.config import ReferencePriceConfig
from vizanix_atlas.stream.config import ReferenceSettings
from vizanix_atlas.stream.types import LiveTick
from vizanix_atlas.stream.universe import PERP_CLASSES, SPOT, InstrumentKey, Universe

#: A mid built from a spread wider than this is not a live two-sided quote worth trusting.
_MAX_MID_SPREAD_BPS = 100.0
#: Weight of a mid-price quote against a last-trade quote (as in the dataset method).
_MID_QUALITY = 1.0
_LAST_QUALITY = 0.6
_MIN_WEIGHT = 1e-3


@dataclass(slots=True)
class QuoteState:
    """The merged latest state of one followed instrument."""

    key: InstrumentKey
    asset_id: str
    quote_asset_id: str
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    quote_volume_24h: float | None = None
    base_volume_24h: float | None = None
    mark: float | None = None
    index: float | None = None
    funding_rate: float | None = None
    funding_interval_hours: float | None = None
    open_interest_usd: float | None = None
    #: When a price-bearing field last changed on this instrument (collector clock, ms).
    price_ms: int | None = None
    event_ms: int | None = None


@dataclass(frozen=True, slots=True)
class VenueQuote:
    """One venue's contribution to (or exclusion from) a live reference price."""

    venue: str
    symbol: str
    price_usd: float
    source: str
    age_ms: int
    volume_usd: float | None
    weight: float
    deviation_bps: float
    included: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class PerpQuote:
    """Live derivative context for one perpetual."""

    venue: str
    symbol: str
    mark: float | None
    index: float | None
    funding_8h: float | None
    open_interest_usd: float | None
    basis_bps: float | None


@dataclass(frozen=True, slots=True)
class LiveAsset:
    """The live view of one asset at one instant."""

    asset_id: str
    symbol: str
    name: str | None
    price: float
    method: str
    venues_used: int
    venues_seen: int
    dispersion_bps: float | None
    volume_24h_usd: float | None
    generation_price: float | None
    vs_generation_bps: float | None
    as_of_ms: int
    identity: str = "resolved"
    quotes: tuple[VenueQuote, ...] = ()
    perps: tuple[PerpQuote, ...] = ()

    def summary(self) -> dict[str, Any]:
        """The compact record sent for every update."""
        return {
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "price": self.price,
            "venues": self.venues_used,
            "dispersion_bps": _round(self.dispersion_bps, 2),
            "volume_24h_usd": _round(self.volume_24h_usd, 0),
            "vs_generation_bps": _round(self.vs_generation_bps, 1),
            "identity": self.identity,
            "as_of": self.as_of_ms,
        }

    def detail(self) -> dict[str, Any]:
        """The full record: every venue considered, and how each one entered the price.

        Deliberately explains rather than republishes: a venue appears with its weight, its
        deviation from the reference price, its quote source and age, and whether and why it
        was excluded. Its own quoted price, volume and mark are not passed on, so the live
        service redistributes derived state and never a venue's raw market data
        (docs/DATA_POLICY.md).
        """
        return {
            **self.summary(),
            "name": self.name,
            "method": self.method,
            "venues_seen": self.venues_seen,
            "generation_price": self.generation_price,
            "quotes": [
                {
                    "venue": q.venue,
                    "symbol": q.symbol,
                    "source": q.source,
                    "age_ms": q.age_ms,
                    "weight": _round(q.weight, 4),
                    "deviation_bps": _round(q.deviation_bps, 2),
                    "included": q.included,
                    "reason": q.reason,
                }
                for q in self.quotes
            ],
            "perpetuals": [
                {
                    "venue": p.venue,
                    "symbol": p.symbol,
                    "funding_8h": p.funding_8h,
                    "open_interest_usd": _round(p.open_interest_usd, 0),
                    "basis_bps": _round(p.basis_bps, 2),
                }
                for p in self.perps
            ],
        }


def _round(value: float | None, digits: int) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits) if digits else float(round(value))


@dataclass(slots=True)
class _FeedState:
    connected: bool = False
    changed_ms: int = 0


@dataclass(slots=True)
class LiveBook:
    """Merges ticks and answers "what is the price of X right now"."""

    reference: ReferenceSettings
    weighting: ReferencePriceConfig
    universe: Universe = field(default_factory=lambda: Universe(generation_id=""))
    states: dict[InstrumentKey, QuoteState] = field(default_factory=dict)
    by_asset: dict[str, list[QuoteState]] = field(default_factory=dict)
    quote_rates: dict[str, float] = field(default_factory=dict)
    feeds: dict[tuple[str, str], _FeedState] = field(default_factory=dict)
    dirty: set[str] = field(default_factory=set)
    ticks_seen: int = 0
    ticks_ignored: int = 0

    # ------------------------------------------------------------------ setup

    def set_universe(self, universe: Universe) -> None:
        """Adopt a (new) universe, keeping the state of instruments that remain."""
        kept: dict[InstrumentKey, QuoteState] = {}
        by_asset: dict[str, list[QuoteState]] = {}
        for key, ref in universe.instruments.items():
            state = self.states.get(key) or QuoteState(
                key=key, asset_id=ref.asset_id, quote_asset_id=ref.quote_asset_id
            )
            state.asset_id, state.quote_asset_id = ref.asset_id, ref.quote_asset_id
            kept[key] = state
            by_asset.setdefault(ref.asset_id, []).append(state)
        self.universe = universe
        self.states = kept
        self.by_asset = by_asset
        # Live stablecoin rates survive a refresh; new quote assets start at the generation's.
        merged = dict(universe.quote_rates)
        merged.update(
            {k: v for k, v in self.quote_rates.items() if k in merged and k != "asset:fiat:usd"}
        )
        self.quote_rates = merged
        self.dirty.update(by_asset)

    def set_feed_connected(
        self, venue: str, instrument_class: str, connected: bool, now_ms: int
    ) -> None:
        """Record a connection change; the venue's quotes expire after the grace period."""
        state = self.feeds.setdefault((venue, instrument_class), _FeedState())
        if state.connected != connected:
            state.connected = connected
            state.changed_ms = now_ms
        elif not state.changed_ms:
            state.changed_ms = now_ms
        self.dirty.update(self.by_asset)

    # ----------------------------------------------------------------- ingest

    def ingest(self, ticks: list[LiveTick]) -> None:
        """Merge ticks into the per-instrument state."""
        for tick in ticks:
            self.ticks_seen += 1
            key = (tick.venue, tick.instrument_class, tick.symbol)
            probe = self.universe.rate_probes.get(key)
            if probe is not None:
                self._update_rate(probe, tick)
                continue
            state = self.states.get(key)
            if state is None:
                self.ticks_ignored += 1
                continue
            for name in ("last", "bid", "ask", "mark", "index"):
                value = getattr(tick, name)
                if value is not None:
                    setattr(state, name, value)
            for name in (
                "quote_volume_24h",
                "base_volume_24h",
                "funding_rate",
                "funding_interval_hours",
                "open_interest_usd",
            ):
                value = getattr(tick, name)
                if value is not None:
                    setattr(state, name, value)
            # A repeated identical quote still proves the connection and the market are
            # alive, so it refreshes the clock; the emitter decides whether it is news.
            if any(getattr(tick, n) is not None for n in ("last", "bid", "ask", "mark")):
                state.price_ms = tick.received_ms
                state.event_ms = tick.event_ms or state.event_ms
                self.dirty.add(state.asset_id)

    def _update_rate(self, stable_asset_id: str, tick: LiveTick) -> None:
        """Update a stablecoin's live USD value from its USD pair, within parity bounds."""
        price = None
        if tick.bid is not None and tick.ask is not None and tick.bid <= tick.ask:
            price = (tick.bid + tick.ask) / 2.0
        elif tick.last is not None:
            price = tick.last
        if price is None or abs(price - 1.0) * 10_000.0 > 500.0:
            return
        if self.quote_rates.get(stable_asset_id) != price:
            self.quote_rates[stable_asset_id] = price
            self.dirty.update(
                a
                for a, states in self.by_asset.items()
                if any(s.quote_asset_id == stable_asset_id for s in states)
            )

    # ---------------------------------------------------------------- compute

    def _feed_ok(self, venue: str, instrument_class: str, now_ms: int) -> bool:
        state = self.feeds.get((venue, instrument_class))
        if state is None:
            return False
        if state.connected:
            return True
        return (now_ms - state.changed_ms) <= self.reference.disconnect_grace_seconds * 1000.0

    def _candidate(self, state: QuoteState, now_ms: int) -> VenueQuote | None:
        """Turn one spot instrument into a priced candidate, or ``None`` if unusable."""
        rate = self.quote_rates.get(state.quote_asset_id)
        if rate is None or state.price_ms is None:
            return None
        source = "last"
        price = state.last
        if state.bid is not None and state.ask is not None and state.bid <= state.ask:
            mid = (state.bid + state.ask) / 2.0
            if (state.ask - state.bid) * 10_000.0 / mid <= _MAX_MID_SPREAD_BPS:
                price, source = mid, "mid"
        if price is None:
            return None
        price_usd = price * rate
        volume_usd: float | None = None
        if state.quote_volume_24h is not None:
            volume_usd = state.quote_volume_24h * rate
        elif state.base_volume_24h is not None:
            volume_usd = state.base_volume_24h * price_usd
        age_ms = max(0, now_ms - state.price_ms)
        return VenueQuote(
            venue=state.key[0],
            symbol=state.key[2],
            price_usd=price_usd,
            source=source,
            age_ms=age_ms,
            volume_usd=volume_usd,
            weight=0.0,
            deviation_bps=0.0,
            included=False,
            reason=None,
        )

    def compute(self, asset_id: str, now_ms: int) -> LiveAsset | None:
        """Compute the live reference price of one asset, or ``None`` without a usable quote."""
        info = self.universe.assets.get(asset_id)
        states = self.by_asset.get(asset_id)
        if info is None or not states:
            return None

        # One candidate per venue: its most liquid usable spot market, so a venue that lists
        # BTC against both USD and USDT is not counted twice.
        best: dict[str, VenueQuote] = {}
        stale_limit = self.reference.stale_after_seconds * 1000.0
        rejected: list[VenueQuote] = []
        for state in states:
            if state.key[1] != SPOT:
                continue
            candidate = self._candidate(state, now_ms)
            if candidate is None:
                continue
            if not self._feed_ok(state.key[0], state.key[1], now_ms):
                rejected.append(_with(candidate, reason="feed_disconnected"))
                continue
            if candidate.age_ms > stale_limit:
                rejected.append(_with(candidate, reason="stale"))
                continue
            current = best.get(candidate.venue)
            if current is None or (candidate.volume_usd or 0.0) > (current.volume_usd or 0.0):
                best[candidate.venue] = candidate
        candidates = list(best.values())
        if not candidates:
            return None

        weights = self._weights(candidates, stale_limit)
        prices = [c.price_usd for c in candidates]
        centre = weighted_median(prices, weights)
        if centre is None:
            return None

        included = [True] * len(candidates)
        reasons: list[str | None] = [None] * len(candidates)
        if len(candidates) >= self.reference.min_venues_for_rejection:
            for index, candidate in enumerate(candidates):
                if (
                    abs(candidate.price_usd - centre) / centre * 10_000.0
                    > self.reference.max_deviation_bps
                ):
                    included[index], reasons[index] = False, "extreme_deviation"
        kept = [c for c, ok in zip(candidates, included, strict=True) if ok]
        kept_weights = self._weights(kept, stale_limit)
        capped = cap_weights(kept_weights, maximum_share=self.weighting.max_venue_weight)
        price = weighted_median([c.price_usd for c in kept], capped)
        if price is None:
            return None

        dispersion = None
        if len(kept) >= 2:
            mad = weighted_mad([c.price_usd for c in kept], capped, centre=price)
            dispersion = None if mad is None else mad / price * 10_000.0

        quotes: list[VenueQuote] = []
        weight_by_venue = {c.venue: w for c, w in zip(kept, capped, strict=True)}
        for candidate, ok, reason in zip(candidates, included, reasons, strict=True):
            quotes.append(
                VenueQuote(
                    venue=candidate.venue,
                    symbol=candidate.symbol,
                    price_usd=candidate.price_usd,
                    source=candidate.source,
                    age_ms=candidate.age_ms,
                    volume_usd=candidate.volume_usd,
                    weight=weight_by_venue.get(candidate.venue, 0.0),
                    deviation_bps=(candidate.price_usd - price) / price * 10_000.0,
                    included=ok,
                    reason=reason,
                )
            )
        for candidate in rejected:
            quotes.append(
                _with(candidate, deviation=(candidate.price_usd - price) / price * 10_000.0)
            )
        quotes.sort(key=lambda q: (not q.included, q.venue))

        volumes = [c.volume_usd for c in kept if c.volume_usd is not None]
        generation_price = info.generation_price
        vs_generation = None
        if generation_price:
            vs_generation = (price - generation_price) / generation_price * 10_000.0
        return LiveAsset(
            asset_id=asset_id,
            symbol=info.symbol,
            name=info.name,
            price=price,
            method="robust_weighted_median" if len(kept) > 1 else "single_venue",
            venues_used=len(kept),
            venues_seen=len(candidates) + len(rejected),
            dispersion_bps=dispersion,
            volume_24h_usd=sum(volumes) if volumes else None,
            generation_price=generation_price,
            vs_generation_bps=vs_generation,
            as_of_ms=now_ms,
            identity=info.identity,
            quotes=tuple(quotes),
            perps=self._perps(states, price, now_ms),
        )

    def _weights(self, candidates: list[VenueQuote], stale_limit_ms: float) -> list[float]:
        weights: list[float] = []
        floor = self.weighting.min_freshness_weight
        exponent = self.weighting.volume_weight_exponent
        for candidate in candidates:
            volume = (candidate.volume_usd or 0.0) ** exponent if candidate.volume_usd else 0.0
            freshness = max(
                floor, 1.0 - (1.0 - floor) * min(1.0, candidate.age_ms / stale_limit_ms)
            )
            quality = _MID_QUALITY if candidate.source == "mid" else _LAST_QUALITY
            weights.append(max(volume, _MIN_WEIGHT) * freshness * quality)
        return weights

    def _perps(
        self, states: list[QuoteState], reference: float, now_ms: int
    ) -> tuple[PerpQuote, ...]:
        out: list[PerpQuote] = []
        for state in states:
            if state.key[1] not in PERP_CLASSES or state.price_ms is None:
                continue
            if not self._feed_ok(state.key[0], state.key[1], now_ms):
                continue
            if now_ms - state.price_ms > self.reference.stale_after_seconds * 1000.0:
                continue
            funding = None
            if state.funding_rate is not None and state.funding_interval_hours:
                funding = state.funding_rate * 8.0 / state.funding_interval_hours
            price = state.mark or state.last
            rate = self.quote_rates.get(state.quote_asset_id, 1.0)
            basis = None
            if price is not None:
                basis = (price * rate - reference) / reference * 10_000.0
            out.append(
                PerpQuote(
                    venue=state.key[0],
                    symbol=state.key[2],
                    mark=state.mark,
                    index=state.index,
                    funding_8h=funding,
                    open_interest_usd=state.open_interest_usd,
                    basis_bps=basis,
                )
            )
        out.sort(key=lambda p: (p.venue, p.symbol))
        return tuple(out)

    # -------------------------------------------------------------- snapshots

    def sweep(self) -> None:
        """Mark every asset for re-evaluation, so time-based expiry takes effect."""
        self.dirty.update(self.by_asset)

    def take_dirty(self) -> list[str]:
        """Return and clear the assets whose inputs changed since the last call."""
        ids = sorted(self.dirty)
        self.dirty.clear()
        return ids

    def snapshot(self, now_ms: int) -> list[LiveAsset]:
        """Every asset that currently has a live price, in rank order."""
        out: list[LiveAsset] = []
        for asset_id, _info in sorted(self.universe.assets.items(), key=lambda kv: kv[1].rank):
            live = self.compute(asset_id, now_ms)
            if live is not None:
                out.append(live)
        return out


def _with(
    quote: VenueQuote, *, reason: str | None = None, deviation: float | None = None
) -> VenueQuote:
    return VenueQuote(
        venue=quote.venue,
        symbol=quote.symbol,
        price_usd=quote.price_usd,
        source=quote.source,
        age_ms=quote.age_ms,
        volume_usd=quote.volume_usd,
        weight=0.0,
        deviation_bps=quote.deviation_bps if deviation is None else deviation,
        included=False,
        reason=reason if reason is not None else quote.reason,
    )
