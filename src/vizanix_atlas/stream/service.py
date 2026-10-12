"""The live service: feeds, book, announcements and the universe refresh loop."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vizanix_atlas.core.config import load_collection_config
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.core.versions import SOFTWARE_VERSION
from vizanix_atlas.sdk.client import Atlas
from vizanix_atlas.stream.book import LiveAsset, LiveBook
from vizanix_atlas.stream.config import StreamConfig
from vizanix_atlas.stream.feeds import FEEDS, Feed
from vizanix_atlas.stream.runner import FeedRunner, now_ms
from vizanix_atlas.stream.universe import Universe, build_universe

_log = get_logger(__name__)

#: How often every asset is re-evaluated, so quotes that stop updating expire on time.
_SWEEP_SECONDS = 2.0

UniverseLoader = Callable[[], Universe]


def default_universe_loader(config: StreamConfig) -> UniverseLoader:
    """Load the universe from the newest published generation."""

    def load() -> Universe:
        return build_universe(Atlas.latest().dataset, config)

    return load


class Hub:
    """Fans announcements out to subscribed clients without ever blocking on one.

    A client holds at most one pending record per asset. If it reads slowly it receives the
    latest state of each asset instead of a growing backlog, so a stalled reader costs a
    few kilobytes, not the process.
    """

    def __init__(self) -> None:
        self.clients: set[Client] = set()

    def publish(self, assets: list[LiveAsset], seq: int) -> None:
        """Offer an update batch to every client whose filter matches."""
        for client in self.clients:
            client.offer(assets, seq)


@dataclass(slots=True, eq=False)
class Client:
    """One connected stream consumer."""

    symbols: frozenset[str] | None
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    seq: int = 0

    def offer(self, assets: list[LiveAsset], seq: int) -> None:
        """Queue the latest state of each matching asset."""
        for asset in assets:
            if self.symbols is None or asset.symbol.upper() in self.symbols:
                self.pending[asset.asset_id] = asset.summary()
        self.seq = seq
        if self.pending:
            self.wake.set()

    def drain(self) -> list[dict[str, Any]]:
        """Take everything pending."""
        items = list(self.pending.values())
        self.pending.clear()
        self.wake.clear()
        return items


@dataclass(slots=True)
class LiveService:
    """Owns the feeds and the book, and announces changes."""

    config: StreamConfig
    loader: UniverseLoader
    book: LiveBook = field(init=False)
    hub: Hub = field(default_factory=Hub)
    runners: dict[str, FeedRunner] = field(default_factory=dict)
    latest: dict[str, LiveAsset] = field(default_factory=dict)
    seq: int = 0
    started_ms: int = field(default_factory=now_ms)
    universe_loaded_ms: int | None = None
    universe_error: str | None = None
    _announced: dict[str, tuple[float, int, int]] = field(default_factory=dict)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _feed_factory: Callable[[type[Feed]], Feed] = lambda cls: cls()
    _connect: Callable[..., Any] | None = None

    def __post_init__(self) -> None:
        self.book = LiveBook(
            reference=self.config.reference, weighting=load_collection_config().reference_price
        )

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Load the universe, start every feed and the background loops."""
        await self._refresh_universe(initial=True)
        self._tasks = [
            asyncio.create_task(self._emit_loop(), name="stream-emit"),
            asyncio.create_task(self._sweep_loop(), name="stream-sweep"),
            asyncio.create_task(self._refresh_loop(), name="stream-refresh"),
        ]

    async def stop(self) -> None:
        """Stop the loops and every feed."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await asyncio.gather(*(runner.stop() for runner in self.runners.values()))
        self.runners.clear()

    # --------------------------------------------------------------- universe

    async def _refresh_universe(self, *, initial: bool = False) -> None:
        try:
            universe = await asyncio.to_thread(self.loader)
        except Exception as error:
            self.universe_error = f"{type(error).__name__}: {error}"[:300]
            _log.warning("live universe refresh failed: %s", self.universe_error)
            if initial:
                raise
            return
        self.universe_error = None
        self.universe_loaded_ms = now_ms()
        changed = universe.symbol_signature() != self.book.universe.symbol_signature()
        self.book.set_universe(universe)
        if initial or changed:
            await self._sync_feeds(universe)
        _log.info(
            "live universe: generation %s, %d assets, %d instruments",
            universe.generation_id,
            len(universe.assets),
            len(universe.instruments),
        )

    async def _sync_feeds(self, universe: Universe) -> None:
        wanted: dict[str, tuple[Feed, list[str]]] = {}
        for name in self.config.feeds:
            cls = FEEDS.get(name)
            if cls is None:
                _log.warning("unknown live feed %r in configuration; skipped", name)
                continue
            feed = self._feed_factory(cls)
            symbols = universe.symbols(feed.venue, feed.instrument_class)
            if symbols:
                wanted[name] = (feed, symbols)
        for name, runner in list(self.runners.items()):
            entry = wanted.get(name)
            if entry is None or sorted(runner.symbols) != entry[1]:
                await runner.stop()
                del self.runners[name]
        for name, (feed, symbols) in wanted.items():
            if name in self.runners:
                continue
            runner = FeedRunner(feed=feed, book=self.book, symbols=symbols)
            runner._connect = self._connect
            runner.start()
            self.runners[name] = runner

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.universe.refresh_seconds)
            await self._refresh_universe()

    # ------------------------------------------------------------ announcements

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(_SWEEP_SECONDS)
            self.book.sweep()

    async def _emit_loop(self) -> None:
        interval = self.config.emit.interval_ms / 1000.0
        while True:
            await asyncio.sleep(interval)
            self.tick()

    def tick(self, at_ms: int | None = None) -> list[LiveAsset]:
        """Evaluate every asset whose inputs changed and announce the ones worth announcing."""
        at = now_ms() if at_ms is None else at_ms
        announce: list[LiveAsset] = []
        threshold = self.config.emit.min_change_bps
        heartbeat_ms = self.config.emit.heartbeat_seconds * 1000.0
        for asset_id in self.book.take_dirty():
            live = self.book.compute(asset_id, at)
            if live is None:
                self.latest.pop(asset_id, None)
                self._announced.pop(asset_id, None)
                continue
            self.latest[asset_id] = live
            previous = self._announced.get(asset_id)
            if previous is not None:
                price, announced_at, venues = previous
                moved = abs(live.price - price) / price * 10_000.0
                news = moved > threshold if threshold > 0 else live.price != price
                if not (news or live.venues_used != venues or at - announced_at >= heartbeat_ms):
                    continue
            self._announced[asset_id] = (live.price, at, live.venues_used)
            announce.append(live)
        if announce:
            self.seq += 1
            self.hub.publish(announce, self.seq)
        return announce

    # ------------------------------------------------------------------ views

    def snapshot(
        self, symbols: frozenset[str] | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """The current live state of every followed asset that has a price."""
        at = now_ms()
        assets = sorted(
            self.latest.values(),
            key=lambda a: self.book.universe.assets[a.asset_id].rank,
        )
        if symbols is not None:
            assets = [a for a in assets if a.symbol.upper() in symbols]
        if limit is not None:
            assets = assets[:limit]
        return {
            "as_of": at,
            "seq": self.seq,
            "generation_id": self.book.universe.generation_id,
            "assets": [a.summary() for a in assets],
        }

    def asset_detail(self, symbol: str) -> dict[str, Any] | None:
        """The full evidence behind one asset's live price, or ``None`` if unknown."""
        wanted = symbol.upper()
        matches = [a for a in self.book.universe.assets.values() if a.symbol.upper() == wanted]
        if len(matches) != 1:
            return None
        live = self.book.compute(matches[0].asset_id, now_ms())
        if live is None:
            return None
        return {"generation_id": self.book.universe.generation_id, **live.detail()}

    def health(self) -> dict[str, Any]:
        """Operational state: feeds, counts, ages. Everything an operator needs at a glance."""
        at = now_ms()
        feeds = [runner.stats.as_dict(at) for runner in self.runners.values()]
        connected = sum(1 for f in feeds if f["connected"] > 0)
        live_assets = len(self.latest)
        if not feeds or connected == 0 or live_assets == 0:
            status = "down"
        elif connected < len(feeds) or self.universe_error:
            status = "degraded"
        else:
            status = "ok"
        return {
            "status": status,
            "software_version": SOFTWARE_VERSION,
            "uptime_seconds": round((at - self.started_ms) / 1000.0, 1),
            "generation_id": self.book.universe.generation_id,
            "universe_assets": len(self.book.universe.assets),
            "assets_live": live_assets,
            "feeds_connected": connected,
            "feeds_total": len(feeds),
            "ticks_seen": self.book.ticks_seen,
            "ticks_ignored": self.book.ticks_ignored,
            "clients": len(self.hub.clients),
            "universe_error": self.universe_error,
            "monotonic": round(time.monotonic(), 1),
            "feeds": feeds,
        }


async def run_forever(service: LiveService) -> None:
    """Start the service and wait until cancelled."""
    await service.start()
    try:
        await asyncio.Event().wait()
    finally:
        with contextlib.suppress(Exception):
            await service.stop()
