"""A deliberately small HTTP server for the live layer.

Three read-only JSON endpoints and one Server-Sent Events stream. It is meant to sit behind a
reverse proxy that terminates TLS and applies per-address limits; it still enforces its own
bounds (request size, header count, read and write timeouts, client count) so that a bad
client costs a connection and nothing more.

    GET /v1/health               operational state of every feed
    GET /v1/live                 current live price of every followed asset
    GET /v1/live/{SYMBOL}        one asset, with every venue's contribution
    GET /v1/stream               Server-Sent Events: a snapshot, then updates

``?assets=BTC,ETH`` narrows ``/v1/live`` and ``/v1/stream``; ``?limit=N`` caps ``/v1/live``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.stream.config import ServerSettings
from vizanix_atlas.stream.service import Client, LiveService

_log = get_logger(__name__)

_MAX_REQUEST_LINE = 4096
_MAX_HEADER_BYTES = 16 * 1024
_MAX_HEADERS = 64
_READ_TIMEOUT = 10.0
_WRITE_TIMEOUT = 15.0
_MAX_SYMBOLS_FILTER = 200
_SYMBOL = re.compile(r"^[A-Za-z0-9._-]{1,24}$")

_REASONS = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    414: "URI Too Long",
    431: "Request Header Fields Too Large",
    503: "Service Unavailable",
}


class _BadRequest(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class LiveServer:
    """Serves a :class:`LiveService` over HTTP."""

    def __init__(self, service: LiveService, settings: ServerSettings) -> None:
        self.service = service
        self.settings = settings
        self._server: asyncio.AbstractServer | None = None

    # ------------------------------------------------------------- lifecycle

    async def start(self, host: str | None = None, port: int | None = None) -> int:
        """Start listening and return the bound port (useful with port 0 in tests)."""
        self._server = await asyncio.start_server(
            self._handle,
            host or self.settings.host,
            self.settings.port if port is None else port,
            limit=_MAX_HEADER_BYTES,
        )
        sockets = list(self._server.sockets)
        return int(sockets[0].getsockname()[1]) if sockets else 0

    async def close(self) -> None:
        """Stop accepting connections and close the open ones."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # -------------------------------------------------------------- plumbing

    def _headers(
        self, status: int, content_type: str, extra: dict[str, str] | None = None
    ) -> bytes:
        lines = [
            f"HTTP/1.1 {status} {_REASONS.get(status, 'Status')}",
            f"Content-Type: {content_type}",
            f"Access-Control-Allow-Origin: {self.settings.cors_origin}",
            "Access-Control-Allow-Methods: GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers: Accept, Cache-Control, Last-Event-ID",
            "Access-Control-Max-Age: 86400",
            "X-Content-Type-Options: nosniff",
            "Cache-Control: no-store",
            "Connection: close",
        ]
        lines.extend(f"{k}: {v}" for k, v in (extra or {}).items())
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

    async def _respond(
        self, writer: asyncio.StreamWriter, status: int, body: Any, *, head_only: bool = False
    ) -> None:
        payload = json.dumps(body, separators=(",", ":"), allow_nan=False).encode("utf-8")
        head = self._headers(
            status, "application/json; charset=utf-8", {"Content-Length": str(len(payload))}
        )
        writer.write(head if head_only else head + payload)
        await asyncio.wait_for(writer.drain(), _WRITE_TIMEOUT)

    async def _read_request(self, reader: asyncio.StreamReader) -> tuple[str, str]:
        try:
            line = await reader.readline()
        except (asyncio.LimitOverrunError, ValueError) as error:
            raise _BadRequest(414, "request line too long") from error
        if not line:
            raise ConnectionError("closed before a request")
        if len(line) > _MAX_REQUEST_LINE:
            raise _BadRequest(414, "request line too long")
        parts = line.decode("latin-1").strip().split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise _BadRequest(400, "malformed request line")
        method, target, _ = parts
        total = len(line)
        for count in range(_MAX_HEADERS + 1):
            header = await reader.readline()
            total += len(header)
            if total > _MAX_HEADER_BYTES:
                raise _BadRequest(431, "headers too large")
            if header in (b"\r\n", b"\n", b""):
                break
            if count == _MAX_HEADERS:
                raise _BadRequest(431, "too many headers")
        return method, target

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method, target = await asyncio.wait_for(self._read_request(reader), _READ_TIMEOUT)
            await self._route(method, target, writer)
        except _BadRequest as error:
            with contextlib.suppress(Exception):
                await self._respond(writer, error.status, {"error": error.message})
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        except Exception:  # noqa: BLE001 - never let one request take the server down
            _log.exception("unhandled error serving a live request")
            with contextlib.suppress(Exception):
                await self._respond(writer, 503, {"error": "internal error"})
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _route(self, method: str, target: str, writer: asyncio.StreamWriter) -> None:
        if method == "OPTIONS":
            writer.write(self._headers(204, "text/plain", {"Content-Length": "0"}))
            await writer.drain()
            return
        if method not in ("GET", "HEAD"):
            await self._respond(writer, 405, {"error": "method not allowed"})
            return
        head_only = method == "HEAD"
        split = urlsplit(target)
        path = unquote(split.path)
        query = parse_qs(split.query, max_num_fields=10)
        symbols = _symbol_filter(query)

        if path == "/v1/health":
            await self._respond(writer, 200, self.service.health(), head_only=head_only)
        elif path == "/v1/live":
            limit = _int(query.get("limit", [None])[0], default=None, low=1, high=1000)
            await self._respond(
                writer, 200, self.service.snapshot(symbols, limit), head_only=head_only
            )
        elif path.startswith("/v1/live/"):
            symbol = path.removeprefix("/v1/live/")
            if not _SYMBOL.match(symbol):
                raise _BadRequest(400, "invalid symbol")
            detail = self.service.asset_detail(symbol)
            if detail is None:
                await self._respond(
                    writer, 404, {"error": "no live price for this symbol"}, head_only=head_only
                )
            else:
                await self._respond(writer, 200, detail, head_only=head_only)
        elif path == "/v1/stream":
            if head_only:
                writer.write(self._headers(200, "text/event-stream"))
                await writer.drain()
            else:
                await self._stream(writer, symbols)
        else:
            await self._respond(writer, 404, {"error": "not found"}, head_only=head_only)

    # ------------------------------------------------------------------- SSE

    async def _stream(self, writer: asyncio.StreamWriter, symbols: frozenset[str] | None) -> None:
        hub = self.service.hub
        if len(hub.clients) >= self.settings.max_clients:
            await self._respond(writer, 503, {"error": "too many clients"})
            return
        client = Client(symbols=symbols)
        hub.clients.add(client)
        keepalive = self.settings.keepalive_seconds
        try:
            writer.write(
                self._headers(
                    200,
                    "text/event-stream; charset=utf-8",
                    {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
                + b"retry: 3000\n\n"
            )
            snapshot = self.service.snapshot(symbols)
            writer.write(_event("snapshot", snapshot, snapshot["seq"]))
            await asyncio.wait_for(writer.drain(), _WRITE_TIMEOUT)
            while not writer.is_closing():
                try:
                    await asyncio.wait_for(client.wake.wait(), timeout=keepalive)
                except TimeoutError:
                    writer.write(b": keepalive\n\n")
                    await asyncio.wait_for(writer.drain(), _WRITE_TIMEOUT)
                    continue
                items = client.drain()
                if items:
                    writer.write(_event("update", {"seq": client.seq, "assets": items}, client.seq))
                    await asyncio.wait_for(writer.drain(), _WRITE_TIMEOUT)
        finally:
            hub.clients.discard(client)


def _event(name: str, payload: dict[str, Any], seq: int) -> bytes:
    data = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    return f"event: {name}\nid: {seq}\ndata: {data}\n\n".encode()


def _symbol_filter(query: dict[str, list[str]]) -> frozenset[str] | None:
    raw = query.get("assets", [""])[0]
    if not raw:
        return None
    wanted = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if len(wanted) > _MAX_SYMBOLS_FILTER:
        raise _BadRequest(400, "too many symbols in the filter")
    if not all(_SYMBOL.match(s) for s in wanted):
        raise _BadRequest(400, "invalid symbol in the filter")
    return frozenset(wanted) or None


def _int(value: str | None, *, default: int | None, low: int, high: int) -> int | None:
    if value is None:
        return default
    try:
        number = int(value)
    except ValueError as error:
        raise _BadRequest(400, "expected an integer") from error
    return max(low, min(high, number))
