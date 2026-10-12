"""Keeps venue WebSocket connections alive and feeds the book.

The supervision rules, in one place:

- One failure is never global. Each connection is its own task with its own back-off; a
  venue that is down, refusing this network, or sending garbage costs only that venue.
- A connection that goes quiet is a dead connection. Silence beyond the idle limit forces a
  reconnect, because a half-open TCP session looks healthy until you stop trusting it.
- Back-off is exponential with jitter and resets only after a connection has held for a
  while, so a venue that accepts and immediately drops us is not hammered.
- A frame that does not parse is counted and dropped. Counts surface in ``/v1/health``,
  which is how schema drift shows up before it becomes a wrong price.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.stream.book import LiveBook
from vizanix_atlas.stream.feeds import Feed
from vizanix_atlas.stream.types import LiveTick

_log = get_logger(__name__)

_IDLE_TIMEOUT_SECONDS = 90.0
_BACKOFF_START = 1.0
_BACKOFF_CAP = 60.0
#: A connection that lasts this long counts as healthy and resets the back-off.
_STABLE_AFTER_SECONDS = 30.0
#: Handshake statuses that mean "this network is refused", not "try again soon".
_REFUSED_STATUSES = frozenset({403, 429, 451})
_REFUSED_BACKOFF_SECONDS = 300.0
_MAX_FRAME_BYTES = 16 * 1024 * 1024


def now_ms() -> int:
    """Wall-clock epoch milliseconds."""
    return int(time.time() * 1000)


@dataclass(slots=True)
class FeedStats:
    """What ``/v1/health`` reports for one feed."""

    name: str
    venue: str
    instrument_class: str
    symbols: int = 0
    connections: int = 0
    connected: int = 0
    messages: int = 0
    ticks: int = 0
    parse_failures: int = 0
    reconnects: int = 0
    last_message_ms: int | None = None
    last_error: str | None = None
    refused: bool = False

    def as_dict(self, at_ms: int) -> dict[str, Any]:
        """Serialisable form with the age of the last frame."""
        return {
            "feed": self.name,
            "venue": self.venue,
            "instrument_class": self.instrument_class,
            "symbols": self.symbols,
            "connections": self.connections,
            "connected": self.connected,
            "messages": self.messages,
            "ticks": self.ticks,
            "parse_failures": self.parse_failures,
            "reconnects": self.reconnects,
            "last_message_age_ms": None
            if self.last_message_ms is None
            else max(0, at_ms - self.last_message_ms),
            "last_error": self.last_error,
            "refused_by_venue": self.refused,
        }


def _split(symbols: Sequence[str], size: int, *, always_one: bool) -> list[list[str]]:
    if not symbols:
        return [[]] if always_one else []
    return [list(symbols[i : i + size]) for i in range(0, len(symbols), size)]


@dataclass(slots=True)
class FeedRunner:
    """Runs one feed: connections, subscriptions, reconnection and statistics."""

    feed: Feed
    book: LiveBook
    symbols: list[str]
    stats: FeedStats = field(init=False)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _clock: Callable[[], int] = now_ms
    #: Test hook: overrides the connection factory (defaults to ``websockets.connect``).
    _connect: Callable[..., Any] | None = None

    def __post_init__(self) -> None:
        self.stats = FeedStats(
            name=self.feed.name,
            venue=self.feed.venue,
            instrument_class=self.feed.instrument_class,
            symbols=len(self.symbols),
        )

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        """Start one supervised connection per symbol group."""
        groups = _split(
            self.symbols, self.feed.max_symbols, always_one=not self.feed.needs_subscription
        )
        self.stats.connections = len(groups)
        for index, group in enumerate(groups):
            self._tasks.append(
                asyncio.create_task(self._supervise(group), name=f"{self.feed.name}-{index}")
            )

    async def stop(self) -> None:
        """Cancel every connection and wait for them to finish."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.book.set_feed_connected(
            self.feed.venue, self.feed.instrument_class, False, self._clock()
        )

    # -------------------------------------------------------------- supervision

    async def _supervise(self, group: list[str]) -> None:
        delay = _BACKOFF_START
        while True:
            started = time.monotonic()
            try:
                await self._session(group)
                self.stats.last_error = "connection closed by the venue"
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a venue failure must never end the loop
                status = _handshake_status(error)
                self.stats.last_error = f"{type(error).__name__}: {error}"[:200]
                self.stats.refused = status in _REFUSED_STATUSES
                if status in _REFUSED_STATUSES:
                    delay = max(delay, _REFUSED_BACKOFF_SECONDS)
                _log.warning("stream feed %s dropped: %s", self.feed.name, self.stats.last_error)
            if time.monotonic() - started >= _STABLE_AFTER_SECONDS:
                delay = _BACKOFF_START
            self.stats.reconnects += 1
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2.0, _BACKOFF_CAP)

    async def _session(self, group: list[str]) -> None:
        connect = self._connect or _default_connect()
        async with connect(
            self.feed.url,
            open_timeout=15,
            max_size=_MAX_FRAME_BYTES,
            ping_interval=20,
            ping_timeout=20,
            user_agent_header="vizanix-atlas-stream",
        ) as socket:
            for frame in self.feed.subscribe_frames(group):
                if self.feed.venue == "cryptocom":
                    # The venue asks for a pause between connecting and subscribing.
                    await asyncio.sleep(1.0)
                await socket.send(frame)
            self.stats.connected += 1
            self.stats.refused = False
            self.book.set_feed_connected(
                self.feed.venue, self.feed.instrument_class, True, self._clock()
            )
            pinger = asyncio.create_task(self._ping_loop(socket))
            try:
                while True:
                    raw = await asyncio.wait_for(socket.recv(), timeout=_IDLE_TIMEOUT_SECONDS)
                    await self._handle(socket, raw)
            finally:
                pinger.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pinger
                self.stats.connected -= 1
                if self.stats.connected == 0:
                    self.book.set_feed_connected(
                        self.feed.venue, self.feed.instrument_class, False, self._clock()
                    )

    async def _handle(self, socket: Any, raw: str | bytes) -> None:
        received = self._clock()
        self.stats.messages += 1
        self.stats.last_message_ms = received
        try:
            text = raw if isinstance(raw, str) else raw.decode("utf-8")
            parsed = self.feed.parse(text, received)
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
            # Not JSON, or a shape this parser does not know. Counted, never fatal.
            self.stats.parse_failures += 1
            return
        if parsed.ticks:
            self.stats.ticks += len(parsed.ticks)
            self._deliver(parsed.ticks)
        for reply in parsed.replies:
            await socket.send(reply)

    def _deliver(self, ticks: list[LiveTick]) -> None:
        self.book.ingest(ticks)

    async def _ping_loop(self, socket: Any) -> None:
        while True:
            await asyncio.sleep(self.feed.ping_interval)
            frame = self.feed.ping()
            if frame is not None:
                await socket.send(frame)


def _handshake_status(error: BaseException) -> int | None:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _default_connect() -> Callable[..., Any]:
    try:
        from websockets.asyncio.client import connect
    except ImportError as error:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "the live layer needs the 'stream' extra: pip install 'vizanix-atlas[stream]'"
        ) from error
    return connect
