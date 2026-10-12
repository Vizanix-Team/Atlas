"""The feed runner against a real local WebSocket server: subscribe, parse, reply, reconnect."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import ClassVar

import pytest
from websockets.asyncio.server import serve

from vizanix_atlas.core.config import load_collection_config
from vizanix_atlas.stream import runner as runner_module
from vizanix_atlas.stream.book import LiveBook
from vizanix_atlas.stream.config import ReferenceSettings
from vizanix_atlas.stream.feeds import Feed
from vizanix_atlas.stream.runner import FeedRunner
from vizanix_atlas.stream.types import LiveTick, Parsed, positive
from vizanix_atlas.stream.universe import AssetInfo, InstrumentRef, Universe

BTC = "asset:native:bitcoin:BTC"
USD = "asset:fiat:usd"


class FakeFeed(Feed):
    venue = "fake"
    instrument_class = "spot"
    name = "fake"
    url = "ws://127.0.0.1:0"
    ping_interval = 0.05
    ping_text: ClassVar[str | None] = "ping!"

    def subscribe_frames(self, symbols: Sequence[str]) -> list[str]:
        return [json.dumps({"subscribe": list(symbols)})]

    def parse(self, frame: str, received_ms: int) -> Parsed:
        payload = json.loads(frame)
        out = Parsed()
        if "heartbeat" in payload:
            out.replies.append(json.dumps({"pong": payload["heartbeat"]}))
        if "price" in payload:
            out.ticks.append(self._tick("XBT-USD", received_ms, last=positive(payload["price"])))
        return out


def make_book() -> LiveBook:
    universe = Universe(generation_id="G", quote_rates={USD: 1.0})
    universe.assets[BTC] = AssetInfo(BTC, "BTC", "Bitcoin", None, None, 1)
    universe.instruments[("fake", "spot", "XBT-USD")] = InstrumentRef(BTC, USD)
    book = LiveBook(
        reference=ReferenceSettings(
            stale_after_seconds=300,
            max_deviation_bps=300,
            min_venues_for_rejection=3,
            disconnect_grace_seconds=20,
        ),
        weighting=load_collection_config().reference_price,
    )
    book.set_universe(universe)
    return book


async def wait_for(predicate, limit=5.0):
    deadline = asyncio.get_running_loop().time() + limit
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(runner_module, "_BACKOFF_START", 0.05)


async def test_subscribes_parses_ticks_and_answers_heartbeats():
    received: list[dict] = []
    heartbeat_sent = asyncio.Event()

    async def handler(ws):
        async for message in ws:
            received.append(json.loads(message) if message != "ping!" else {"ping": True})
            if "subscribe" in received[-1]:
                await ws.send(json.dumps({"price": "100000.5"}))
                await ws.send(json.dumps({"heartbeat": 7}))
                heartbeat_sent.set()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]

        class Feed1(FakeFeed):
            url = f"ws://127.0.0.1:{port}"

        book = make_book()
        runner = FeedRunner(feed=Feed1(), book=book, symbols=["XBT-USD"])
        runner.start()
        try:
            await wait_for(lambda: book.states[("fake", "spot", "XBT-USD")].last == 100000.5)
            await wait_for(lambda: {"pong": 7} in received)
            await wait_for(lambda: {"ping": True} in received)  # the application keepalive
            assert {"subscribe": ["XBT-USD"]} in received
            assert runner.stats.connected == 1 and runner.stats.ticks == 1
            assert book.feeds[("fake", "spot")].connected
            assert book.compute(BTC, runner_module.now_ms()).price == 100000.5
        finally:
            await runner.stop()
    assert not book.feeds[("fake", "spot")].connected


async def test_reconnects_after_the_venue_drops_the_connection_and_resubscribes():
    connections = 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        await ws.recv()
        await ws.send(json.dumps({"price": str(100 + connections)}))
        if connections == 1:
            await ws.close()
            return
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]

        class Feed2(FakeFeed):
            url = f"ws://127.0.0.1:{port}"

        book = make_book()
        runner = FeedRunner(feed=Feed2(), book=book, symbols=["XBT-USD"])
        runner.start()
        try:
            await wait_for(lambda: book.states[("fake", "spot", "XBT-USD")].last == 102.0)
            assert connections == 2 and runner.stats.reconnects >= 1
        finally:
            await runner.stop()


async def test_garbage_is_counted_as_a_parse_failure_and_never_fatal():
    async def handler(ws):
        await ws.recv()
        await ws.send("<html>502</html>")
        await ws.send(json.dumps({"price": "5"}))
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]

        class Feed3(FakeFeed):
            url = f"ws://127.0.0.1:{port}"

        book = make_book()
        runner = FeedRunner(feed=Feed3(), book=book, symbols=["XBT-USD"])
        runner.start()
        try:
            await wait_for(lambda: runner.stats.ticks == 1)
            assert runner.stats.parse_failures == 1 and runner.stats.messages == 2
        finally:
            await runner.stop()


async def test_unreachable_venue_is_retried_and_reported_not_raised():
    # Port 9 (discard) on localhost refuses the connection.
    class Feed4(FakeFeed):
        url = "ws://127.0.0.1:9"

    book = make_book()
    runner = FeedRunner(feed=Feed4(), book=book, symbols=["XBT-USD"])
    runner.start()
    try:
        await wait_for(lambda: runner.stats.reconnects >= 1)
        assert runner.stats.connected == 0 and runner.stats.last_error
        assert book.compute(BTC, runner_module.now_ms()) is None
    finally:
        await runner.stop()


async def test_symbols_are_spread_over_connections_by_the_feed_limit():
    seen: list[list[str]] = []

    async def handler(ws):
        message = json.loads(await ws.recv())
        seen.append(message["subscribe"])
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]

        class Feed5(FakeFeed):
            url = f"ws://127.0.0.1:{port}"
            max_symbols = 2

        runner = FeedRunner(feed=Feed5(), book=make_book(), symbols=["A", "B", "C", "D", "E"])
        runner.start()
        try:
            await wait_for(lambda: len(seen) == 3)
            assert sorted(map(len, seen)) == [1, 2, 2]
            assert runner.stats.connections == 3
        finally:
            await runner.stop()


def test_tick_type_is_a_plain_value_object():
    tick = LiveTick(venue="v", instrument_class="spot", symbol="S", received_ms=1, last=2.0)
    assert tick.bid is None and tick.funding_rate is None
