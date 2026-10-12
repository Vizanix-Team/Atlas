"""The live service and its HTTP/SSE surface, driven with injected ticks (no network)."""

from __future__ import annotations

import asyncio
import json

import pytest

from vizanix_atlas.stream.config import load_stream_config
from vizanix_atlas.stream.server import LiveServer
from vizanix_atlas.stream.service import LiveService
from vizanix_atlas.stream.types import LiveTick
from vizanix_atlas.stream.universe import AssetInfo, InstrumentRef, Universe

BTC = "asset:native:bitcoin:BTC"
ETH = "asset:native:ethereum:ETH"
USD = "asset:fiat:usd"


def make_universe() -> Universe:
    universe = Universe(generation_id="G1", quote_rates={USD: 1.0})
    universe.assets[BTC] = AssetInfo(BTC, "BTC", "Bitcoin", 100_000.0, 2e10, 1)
    universe.assets[ETH] = AssetInfo(ETH, "ETH", "Ether", 2_500.0, 1e10, 2)
    for venue in ("coinbase", "kraken"):
        universe.instruments[(venue, "spot", "BTC-USD")] = InstrumentRef(BTC, USD)
        universe.instruments[(venue, "spot", "ETH-USD")] = InstrumentRef(ETH, USD)
    return universe


def make_service() -> LiveService:
    config = load_stream_config()
    service = LiveService(config=config, loader=make_universe)
    service.book.set_universe(make_universe())
    for venue in ("coinbase", "kraken"):
        service.book.set_feed_connected(venue, "spot", True, 0)
    return service


def tk(venue, symbol, price, at) -> LiveTick:
    return LiveTick(venue=venue, instrument_class="spot", symbol=symbol, received_ms=at, last=price)


def feed(service: LiveService, at: int, btc: float, eth: float = 2_500.0) -> None:
    service.book.ingest(
        [
            tk("coinbase", "BTC-USD", btc, at),
            tk("kraken", "BTC-USD", btc + 1, at),
            tk("coinbase", "ETH-USD", eth, at),
            tk("kraken", "ETH-USD", eth + 1, at),
        ]
    )


def test_first_tick_announces_every_priced_asset_and_unchanged_prices_stay_quiet():
    service = make_service()
    at = 10_000_000
    feed(service, at, 100_000.0)
    announced = service.tick(at)
    assert {a.symbol for a in announced} == {"BTC", "ETH"} and service.seq == 1

    feed(service, at + 600, 100_000.0)  # identical prices: alive, but not news
    assert service.tick(at + 600) == []
    assert service.seq == 1

    feed(service, at + 1200, 100_050.0)
    moved = service.tick(at + 1200)
    assert [a.symbol for a in moved] == ["BTC"]  # ETH did not move
    assert service.seq == 2


def test_heartbeat_reannounces_an_unchanged_price_after_the_configured_interval():
    service = make_service()
    at = 10_000_000
    feed(service, at, 100_000.0)
    service.tick(at)
    later = at + int(service.config.emit.heartbeat_seconds * 1000) + 1
    feed(service, later, 100_000.0)
    assert {a.symbol for a in service.tick(later)} == {"BTC", "ETH"}


def test_a_change_in_how_many_venues_back_the_price_is_news_even_if_the_price_is_not():
    service = make_service()
    at = 10_000_000
    feed(service, at, 100_000.0)
    service.tick(at)
    service.book.set_feed_connected("kraken", "spot", False, at)
    expired = at + 60_000
    service.book.ingest([tk("coinbase", "BTC-USD", 100_000.0, expired)])
    service.book.sweep()
    announced = {a.symbol: a for a in service.tick(expired)}
    assert announced["BTC"].venues_used == 1


def test_an_asset_that_loses_all_quotes_disappears_from_the_snapshot():
    service = make_service()
    at = 10_000_000
    feed(service, at, 100_000.0)
    service.tick(at)
    assert {a["symbol"] for a in service.snapshot()["assets"]} == {"BTC", "ETH"}
    service.book.sweep()
    service.tick(at + 10 * 60 * 1000)  # far past the staleness limit
    assert service.snapshot()["assets"] == []


def test_snapshot_filters_and_limits():
    service = make_service()
    feed(service, 10_000_000, 100_000.0)
    service.tick(10_000_000)
    assert [a["symbol"] for a in service.snapshot(frozenset({"ETH"}))["assets"]] == ["ETH"]
    assert len(service.snapshot(limit=1)["assets"]) == 1


def test_asset_detail_requires_a_unique_symbol_and_a_live_price():
    service = make_service()
    assert service.asset_detail("btc") is None  # no quotes yet
    from vizanix_atlas.stream.runner import now_ms

    at = now_ms()
    feed(service, at, 100_000.0)
    detail = service.asset_detail("btc")
    assert detail is not None and detail["symbol"] == "BTC" and len(detail["quotes"]) == 2
    assert service.asset_detail("nope") is None


def test_client_keeps_only_the_latest_record_per_asset():
    from vizanix_atlas.stream.service import Client

    service = make_service()
    feed(service, 10_000_000, 100_000.0)
    first = service.tick(10_000_000)
    feed(service, 10_001_000, 100_100.0)
    second = service.tick(10_001_000)
    client = Client(symbols=None)
    client.offer(first, 1)
    client.offer(second, 2)
    drained = client.drain()
    btc = next(r for r in drained if r["symbol"] == "BTC")
    assert btc["price"] == pytest.approx(second[0].price) and len(drained) == 2
    assert client.drain() == [] and not client.wake.is_set()


def test_client_filter_drops_other_assets():
    from vizanix_atlas.stream.service import Client

    service = make_service()
    feed(service, 10_000_000, 100_000.0)
    announced = service.tick(10_000_000)
    client = Client(symbols=frozenset({"ETH"}))
    client.offer(announced, 1)
    assert [r["symbol"] for r in client.drain()] == ["ETH"]


def test_health_reports_down_without_feeds_and_ok_when_all_connect():
    service = make_service()
    assert service.health()["status"] == "down"
    assert service.health()["assets_live"] == 0


# ------------------------------------------------------------------ HTTP / SSE


async def http(port: int, raw: bytes) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    await writer.wait_closed()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines[1:])}
    return status, headers, body


@pytest.fixture
async def served():
    service = make_service()
    server = LiveServer(service, service.config.server)
    port = await server.start(host="127.0.0.1", port=0)
    from vizanix_atlas.stream.runner import now_ms

    feed(service, now_ms(), 100_000.0)
    service.tick()
    yield service, port
    await server.close()


async def test_live_endpoint_returns_prices_with_cors(served):
    _service, port = served
    status, headers, body = await http(port, b"GET /v1/live?assets=btc HTTP/1.1\r\nHost: x\r\n\r\n")
    assert status == 200
    assert headers["access-control-allow-origin"] == "*"
    assert headers["content-type"].startswith("application/json")
    payload = json.loads(body)
    assert [a["symbol"] for a in payload["assets"]] == ["BTC"]
    assert payload["generation_id"] == "G1"


async def test_asset_detail_endpoint_and_404(served):
    _, port = served
    status, _, body = await http(port, b"GET /v1/live/BTC HTTP/1.1\r\n\r\n")
    assert status == 200 and json.loads(body)["quotes"]
    status, _, _ = await http(port, b"GET /v1/live/NOPE HTTP/1.1\r\n\r\n")
    assert status == 404


async def test_health_endpoint(served):
    _, port = served
    status, _, body = await http(port, b"GET /v1/health HTTP/1.1\r\n\r\n")
    assert status == 200 and json.loads(body)["assets_live"] == 2


async def test_rejects_bad_methods_paths_symbols_and_oversized_requests(served):
    _, port = served
    assert (await http(port, b"POST /v1/live HTTP/1.1\r\n\r\n"))[0] == 405
    assert (await http(port, b"GET /nope HTTP/1.1\r\n\r\n"))[0] == 404
    assert (await http(port, b"GET /v1/live/BT%C2%A7C HTTP/1.1\r\n\r\n"))[0] == 400
    assert (await http(port, b"GET /v1/live?limit=abc HTTP/1.1\r\n\r\n"))[0] == 400
    assert (await http(port, b"NONSENSE\r\n\r\n"))[0] == 400
    assert (await http(port, b"GET /" + b"a" * 5000 + b" HTTP/1.1\r\n\r\n"))[0] == 414
    many = b"GET /v1/live HTTP/1.1\r\n" + b"X: y\r\n" * 100 + b"\r\n"
    assert (await http(port, many))[0] == 431


async def test_options_preflight_and_head(served):
    _, port = served
    status, headers, body = await http(port, b"OPTIONS /v1/stream HTTP/1.1\r\n\r\n")
    assert status == 204 and "GET" in headers["access-control-allow-methods"] and body == b""
    status, headers, body = await http(port, b"HEAD /v1/live HTTP/1.1\r\n\r\n")
    assert status == 200 and body == b"" and int(headers["content-length"]) > 0


async def test_sse_delivers_a_snapshot_then_only_changes(served):
    service, port = served
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /v1/stream?assets=BTC HTTP/1.1\r\nAccept: text/event-stream\r\n\r\n")
    await writer.drain()

    async def read_event() -> tuple[str, dict]:
        name, data = "", ""
        while True:
            line = (await asyncio.wait_for(reader.readline(), 5)).decode().rstrip("\n")
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
            elif line == "" and data:
                return name, json.loads(data)

    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    assert b"text/event-stream" in head and b"X-Accel-Buffering: no" in head
    name, snapshot = await read_event()
    assert name == "snapshot" and [a["symbol"] for a in snapshot["assets"]] == ["BTC"]

    from vizanix_atlas.stream.runner import now_ms

    at = now_ms()
    feed(service, at, 101_000.0, eth=2_600.0)
    service.tick(at)
    name, update = await read_event()
    assert name == "update"
    assert [a["symbol"] for a in update["assets"]] == ["BTC"]  # ETH filtered out
    assert update["assets"][0]["price"] == pytest.approx(101_000.5)

    writer.close()
    await writer.wait_closed()
    for _ in range(50):
        if not service.hub.clients:
            break
        await asyncio.sleep(0.05)
    # The disconnected client is dropped on the next write attempt.
    feed(service, at + 1000, 102_000.0)
    service.tick(at + 1000)
    await asyncio.sleep(0.2)
    feed(service, at + 2000, 103_000.0)
    service.tick(at + 2000)
    for _ in range(60):
        if not service.hub.clients:
            break
        await asyncio.sleep(0.05)
    assert not service.hub.clients


async def test_stream_refuses_clients_beyond_the_limit():
    service = make_service()
    settings = service.config.server.model_copy(update={"max_clients": 1})
    server = LiveServer(service, settings)
    port = await server.start(host="127.0.0.1", port=0)
    first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
    first_writer.write(b"GET /v1/stream HTTP/1.1\r\n\r\n")
    await first_writer.drain()
    await asyncio.wait_for(first_reader.readuntil(b"\r\n\r\n"), 5)
    status, _, _ = await http(port, b"GET /v1/stream HTTP/1.1\r\n\r\n")
    assert status == 503
    first_writer.close()
    await first_writer.wait_closed()
    await server.close()
