"""Venue WebSocket feeds.

One :class:`Feed` is one venue and one instrument class on that venue's *public* market-data
WebSocket. A feed knows three venue-specific things and nothing else: how to subscribe,
how to parse a frame into :class:`~vizanix_atlas.stream.types.LiveTick` values, and how to
keep the connection alive. Reconnection, back-off and the staleness watchdog live in the
runner, so a new venue is a parser and not a new failure-handling story.

Every frame layout below was recorded from the live endpoint (2026-10-12) and is pinned by
the fixtures in ``tests/fixtures/stream``. A frame that does not match is counted as a parse
failure by the runner and dropped; nothing here guesses.
"""

from __future__ import annotations

import abc
import json
import time
from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Any, ClassVar, Final

from vizanix_atlas.stream.types import LiveTick, Parsed, finite, positive


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _iso_ms(text: object) -> int | None:
    """Parse an ISO-8601 timestamp into epoch milliseconds."""
    if not isinstance(text, str):
        return None
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


class Feed(abc.ABC):
    """Base class for a venue WebSocket feed."""

    #: Venue slug, matching ``config/exchanges.yaml``.
    venue: ClassVar[str]
    #: The dataset ``instrument_class`` this feed covers (``spot``, ``linear-perp`` ...).
    instrument_class: ClassVar[str]
    #: Registry name used in ``config/stream.yaml``.
    name: ClassVar[str]
    url: ClassVar[str]
    #: Subscriptions per connection; larger symbol sets are spread over several.
    max_symbols: ClassVar[int] = 200
    #: Application-level keepalive frame, for venues that ignore protocol pings.
    ping_text: ClassVar[str | None] = None
    ping_interval: ClassVar[float] = 20.0
    #: Feeds that carry every market on one URL need no subscription frames.
    needs_subscription: ClassVar[bool] = True

    @abc.abstractmethod
    def subscribe_frames(self, symbols: Sequence[str]) -> list[str]:
        """Return the frames that subscribe a connection to ``symbols``."""

    @abc.abstractmethod
    def parse(self, frame: str, received_ms: int) -> Parsed:
        """Turn one text frame into ticks (and any frames that must be sent back)."""

    def ping(self) -> str | None:
        """The keepalive frame to send every ``ping_interval`` seconds, if any."""
        return self.ping_text

    def _tick(self, symbol: str, received_ms: int, **fields: Any) -> LiveTick:
        return LiveTick(
            venue=self.venue,
            instrument_class=self.instrument_class,
            symbol=symbol,
            received_ms=received_ms,
            **fields,
        )


# --------------------------------------------------------------------------- Binance


class BinanceFeed(Feed):
    """Binance spot: one stream carries a 24-hour mini ticker for every market."""

    venue = "binance"
    instrument_class = "spot"
    name = "binance"
    # Binance's documented market-data-only host, the same one the batch adapter uses.
    url = "wss://data-stream.binance.vision/ws/!miniTicker@arr"
    needs_subscription = False

    def subscribe_frames(self, symbols: Sequence[str]) -> list[str]:
        return []

    def parse(self, frame: str, received_ms: int) -> Parsed:
        payload = json.loads(frame)
        rows = payload if isinstance(payload, list) else []
        out = Parsed()
        for row in rows:
            symbol = row.get("s")
            if not isinstance(symbol, str):
                continue
            out.ticks.append(
                self._tick(
                    symbol,
                    received_ms,
                    event_ms=_int(row.get("E")),
                    last=positive(row.get("c")),
                    quote_volume_24h=positive(row.get("q")),
                    base_volume_24h=positive(row.get("v")),
                )
            )
        return out


# --------------------------------------------------------------------------- Bybit


class _BybitFeed(Feed):
    ping_text = '{"op":"ping"}'
    ping_interval = 20.0
    max_symbols = 200

    def subscribe_frames(self, symbols: Sequence[str]) -> list[str]:
        # Bybit accepts at most ten topics per subscribe request on spot.
        return [
            json.dumps({"op": "subscribe", "args": [f"tickers.{s}" for s in group]})
            for group in _chunks(list(symbols), 10)
        ]

    def _common(self, data: dict[str, Any], received_ms: int, event_ms: int | None) -> LiveTick:
        return self._tick(
            data["symbol"],
            received_ms,
            event_ms=event_ms,
            last=positive(data.get("lastPrice")),
            bid=positive(data.get("bid1Price")),
            ask=positive(data.get("ask1Price")),
            quote_volume_24h=positive(data.get("turnover24h")),
            base_volume_24h=positive(data.get("volume24h")),
        )

    def parse(self, frame: str, received_ms: int) -> Parsed:
        payload = json.loads(frame)
        out = Parsed()
        topic = payload.get("topic")
        data = payload.get("data")
        if not (isinstance(topic, str) and topic.startswith("tickers.") and isinstance(data, dict)):
            return out
        if not isinstance(data.get("symbol"), str):
            return out
        out.ticks.append(self._common(data, received_ms, _int(payload.get("ts"))))
        return out


class BybitSpotFeed(_BybitFeed):
    """Bybit spot tickers."""

    venue = "bybit"
    instrument_class = "spot"
    name = "bybit_spot"
    url = "wss://stream.bybit.com/v5/public/spot"


class BybitLinearFeed(_BybitFeed):
    """Bybit USDT-margined perpetuals: price, mark, index, funding and open interest.

    Bybit streams deltas here, so a tick carries only what changed; the book merges.
    """

    venue = "bybit"
    instrument_class = "linear-perp"
    name = "bybit_linear"
    url = "wss://stream.bybit.com/v5/public/linear"

    def parse(self, frame: str, received_ms: int) -> Parsed:
        payload = json.loads(frame)
        out = Parsed()
        topic = payload.get("topic")
        data = payload.get("data")
        if not (isinstance(topic, str) and topic.startswith("tickers.") and isinstance(data, dict)):
            return out
        if not isinstance(data.get("symbol"), str):
            return out
        out.ticks.append(
            self._tick(
                data["symbol"],
                received_ms,
                event_ms=_int(payload.get("ts")),
                last=positive(data.get("lastPrice")),
                bid=positive(data.get("bid1Price")),
                ask=positive(data.get("ask1Price")),
                quote_volume_24h=positive(data.get("turnover24h")),
                base_volume_24h=positive(data.get("volume24h")),
                mark=positive(data.get("markPrice")),
                index=positive(data.get("indexPrice")),
                funding_rate=finite(data.get("fundingRate")),
                funding_interval_hours=positive(data.get("fundingIntervalHour")),
                open_interest_usd=positive(data.get("openInterestValue")),
            )
        )
        return out


# --------------------------------------------------------------------------- OKX


class OkxFeed(Feed):
    """OKX spot tickers."""

    venue = "okx"
    instrument_class = "spot"
    name = "okx"
    url = "wss://ws.okx.com:8443/ws/v5/public"
    ping_text = "ping"
    ping_interval = 20.0

    def subscribe_frames(self, symbols: Sequence[str]) -> list[str]:
        return [
            json.dumps(
                {"op": "subscribe", "args": [{"channel": "tickers", "instId": s} for s in group]}
            )
            for group in _chunks(list(symbols), 50)
        ]

    def parse(self, frame: str, received_ms: int) -> Parsed:
        out = Parsed()
        if frame == "pong":
            return out
        payload = json.loads(frame)
        arg = payload.get("arg")
        data = payload.get("data")
        if not (
            isinstance(arg, dict) and arg.get("channel") == "tickers" and isinstance(data, list)
        ):
            return out
        for row in data:
            symbol = row.get("instId")
            if not isinstance(symbol, str):
                continue
            out.ticks.append(
                self._tick(
                    symbol,
                    received_ms,
                    event_ms=_int(row.get("ts")),
                    last=positive(row.get("last")),
                    bid=positive(row.get("bidPx")),
                    ask=positive(row.get("askPx")),
                    # For spot, volCcy24h is the quote-currency volume and vol24h the base.
                    quote_volume_24h=positive(row.get("volCcy24h")),
                    base_volume_24h=positive(row.get("vol24h")),
                )
            )
        return out


# --------------------------------------------------------------------------- Coinbase


class CoinbaseFeed(Feed):
    """Coinbase Exchange spot ticker channel (fires on every trade)."""

    venue = "coinbase"
    instrument_class = "spot"
    name = "coinbase"
    url = "wss://ws-feed.exchange.coinbase.com"
    max_symbols = 300

    def subscribe_frames(self, symbols: Sequence[str]) -> list[str]:
        return [
            json.dumps({"type": "subscribe", "product_ids": list(group), "channels": ["ticker"]})
            for group in _chunks(list(symbols), 100)
        ]

    def parse(self, frame: str, received_ms: int) -> Parsed:
        payload = json.loads(frame)
        out = Parsed()
        if payload.get("type") != "ticker" or not isinstance(payload.get("product_id"), str):
            return out
        out.ticks.append(
            self._tick(
                payload["product_id"],
                received_ms,
                event_ms=_iso_ms(payload.get("time")),
                last=positive(payload.get("price")),
                bid=positive(payload.get("best_bid")),
                ask=positive(payload.get("best_ask")),
                # Coinbase states volume in the base asset only.
                base_volume_24h=positive(payload.get("volume_24h")),
            )
        )
        return out


# --------------------------------------------------------------------------- Gate.io


class GateFeed(Feed):
    """Gate.io spot tickers."""

    venue = "gateio"
    instrument_class = "spot"
    name = "gateio"
    url = "wss://api.gateio.ws/ws/v4/"
    ping_interval = 15.0

    def ping(self) -> str | None:
        return json.dumps({"time": int(time.time()), "channel": "spot.ping"})

    def subscribe_frames(self, symbols: Sequence[str]) -> list[str]:
        return [
            json.dumps(
                {
                    "time": int(time.time()),
                    "channel": "spot.tickers",
                    "event": "subscribe",
                    "payload": list(group),
                }
            )
            for group in _chunks(list(symbols), 100)
        ]

    def parse(self, frame: str, received_ms: int) -> Parsed:
        payload = json.loads(frame)
        out = Parsed()
        if payload.get("channel") != "spot.tickers" or payload.get("event") != "update":
            return out
        row = payload.get("result")
        if not (isinstance(row, dict) and isinstance(row.get("currency_pair"), str)):
            return out
        out.ticks.append(
            self._tick(
                row["currency_pair"],
                received_ms,
                event_ms=_int(payload.get("time_ms")),
                last=positive(row.get("last")),
                bid=positive(row.get("highest_bid")),
                ask=positive(row.get("lowest_ask")),
                quote_volume_24h=positive(row.get("quote_volume")),
                base_volume_24h=positive(row.get("base_volume")),
            )
        )
        return out


# --------------------------------------------------------------------------- Bitget


class BitgetFeed(Feed):
    """Bitget spot tickers."""

    venue = "bitget"
    instrument_class = "spot"
    name = "bitget"
    url = "wss://ws.bitget.com/v2/ws/public"
    ping_text = "ping"
    ping_interval = 25.0

    def subscribe_frames(self, symbols: Sequence[str]) -> list[str]:
        return [
            json.dumps(
                {
                    "op": "subscribe",
                    "args": [{"instType": "SPOT", "channel": "ticker", "instId": s} for s in group],
                }
            )
            for group in _chunks(list(symbols), 50)
        ]

    def parse(self, frame: str, received_ms: int) -> Parsed:
        out = Parsed()
        if frame == "pong":
            return out
        payload = json.loads(frame)
        arg = payload.get("arg")
        data = payload.get("data")
        if not (
            isinstance(arg, dict) and arg.get("channel") == "ticker" and isinstance(data, list)
        ):
            return out
        for row in data:
            symbol = row.get("instId")
            if not isinstance(symbol, str):
                continue
            out.ticks.append(
                self._tick(
                    symbol,
                    received_ms,
                    event_ms=_int(row.get("ts")),
                    last=positive(row.get("lastPr")),
                    bid=positive(row.get("bidPr")),
                    ask=positive(row.get("askPr")),
                    quote_volume_24h=positive(row.get("quoteVolume")),
                    base_volume_24h=positive(row.get("baseVolume")),
                )
            )
        return out


# --------------------------------------------------------------------------- Crypto.com


class CryptoComFeed(Feed):
    """Crypto.com Exchange spot tickers."""

    venue = "cryptocom"
    instrument_class = "spot"
    name = "cryptocom"
    url = "wss://stream.crypto.com/exchange/v1/market"
    max_symbols = 200

    def subscribe_frames(self, symbols: Sequence[str]) -> list[str]:
        return [
            json.dumps(
                {
                    "id": index + 1,
                    "method": "subscribe",
                    "params": {"channels": [f"ticker.{s}" for s in group]},
                }
            )
            for index, group in enumerate(_chunks(list(symbols), 100))
        ]

    def parse(self, frame: str, received_ms: int) -> Parsed:
        payload = json.loads(frame)
        out = Parsed()
        if payload.get("method") == "public/heartbeat":
            # The venue drops a connection that does not answer its heartbeat.
            out.replies.append(
                json.dumps({"id": payload.get("id"), "method": "public/respond-heartbeat"})
            )
            return out
        result = payload.get("result")
        if not (isinstance(result, dict) and result.get("channel") == "ticker"):
            return out
        for row in result.get("data") or []:
            symbol = row.get("i")
            if not isinstance(symbol, str):
                continue
            out.ticks.append(
                self._tick(
                    symbol,
                    received_ms,
                    event_ms=_int(row.get("t")),
                    last=positive(row.get("a")),
                    bid=positive(row.get("b")),
                    ask=positive(row.get("k")),
                    base_volume_24h=positive(row.get("v")),
                )
            )
        return out


# --------------------------------------------------------------------------- Deribit


class DeribitFeed(Feed):
    """Deribit inverse perpetuals (BTC-PERPETUAL, ETH-PERPETUAL ...).

    Open interest on an inverse perpetual is stated in USD contracts, so it is already a
    USD figure. Funding is published as an 8-hour rate.
    """

    venue = "deribit"
    instrument_class = "inverse-perp"
    name = "deribit"
    url = "wss://www.deribit.com/ws/api/v2"
    max_symbols = 50

    def subscribe_frames(self, symbols: Sequence[str]) -> list[str]:
        return [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": index + 1,
                    "method": "public/subscribe",
                    "params": {"channels": [f"ticker.{s}.100ms" for s in group]},
                }
            )
            for index, group in enumerate(_chunks(list(symbols), 20))
        ]

    def parse(self, frame: str, received_ms: int) -> Parsed:
        payload = json.loads(frame)
        out = Parsed()
        if payload.get("method") != "subscription":
            return out
        data = (payload.get("params") or {}).get("data")
        if not (isinstance(data, dict) and isinstance(data.get("instrument_name"), str)):
            return out
        out.ticks.append(
            self._tick(
                data["instrument_name"],
                received_ms,
                event_ms=_int(data.get("timestamp")),
                last=positive(data.get("last_price")),
                bid=positive(data.get("best_bid_price")),
                ask=positive(data.get("best_ask_price")),
                mark=positive(data.get("mark_price")),
                index=positive(data.get("index_price")),
                funding_rate=finite(data.get("funding_8h")),
                funding_interval_hours=8.0,
                open_interest_usd=positive(data.get("open_interest")),
            )
        )
        return out


#: Every feed the live layer can run, by the name used in ``config/stream.yaml``.
FEEDS: Final[dict[str, type[Feed]]] = {
    cls.name: cls
    for cls in (
        BinanceFeed,
        BybitSpotFeed,
        BybitLinearFeed,
        OkxFeed,
        CoinbaseFeed,
        GateFeed,
        BitgetFeed,
        CryptoComFeed,
        DeribitFeed,
    )
}
