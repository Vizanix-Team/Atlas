"""A client for the live layer.

Needs no extra dependency: Server-Sent Events are plain HTTP, read with ``httpx``.

    from vizanix_atlas.sdk.live import LiveClient

    live = LiveClient()
    print(live.price("BTC"))                    # one-shot snapshot
    for update in live.stream(["BTC", "ETH"]):  # blocks, reconnects on its own
        print(update["symbol"], update["price"])
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx

from vizanix_atlas.core.errors import DatasetUnavailable

#: Where the project's own live service answers. Override with ``ATLAS_LIVE_URL``.
DEFAULT_LIVE_URL = "https://vizanix.tech/atlas-stream"

_RECONNECT_START = 1.0
_RECONNECT_CAP = 30.0


def default_live_url() -> str:
    """The live service base URL (``ATLAS_LIVE_URL`` or the project default)."""
    return os.environ.get("ATLAS_LIVE_URL", DEFAULT_LIVE_URL).rstrip("/")


class _SseDecoder:
    """Accumulates SSE lines and yields ``(event, payload)`` at each blank line."""

    def __init__(self) -> None:
        self._event = "message"
        self._data: list[str] = []

    def feed(self, line: str) -> tuple[str, dict[str, Any]] | None:
        """Consume one line; return a completed event, if this line completed one."""
        if line == "":
            result = None
            if self._data:
                with contextlib.suppress(ValueError):
                    payload = json.loads("\n".join(self._data))
                    if isinstance(payload, dict):
                        result = (self._event, payload)
            self._event, self._data = "message", []
            return result
        if line.startswith("event:"):
            self._event = line[6:].strip()
        elif line.startswith("data:"):
            self._data.append(line[5:].lstrip())
        return None


def _parse_events(lines: Iterator[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Parse an SSE line stream into ``(event, payload)`` pairs."""
    decoder = _SseDecoder()
    for line in lines:
        if (event := decoder.feed(line)) is not None:
            yield event


async def _aparse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    decoder = _SseDecoder()
    async for line in lines:
        if (event := decoder.feed(line)) is not None:
            yield event


class LiveClient:
    """Reads live prices from an Atlas live service."""

    def __init__(self, base_url: str | None = None, *, timeout: float = 15.0) -> None:
        self.base_url = (base_url or default_live_url()).rstrip("/")
        self._timeout = timeout

    # ------------------------------------------------------------------ one-shot

    def _get(self, path: str, **params: str) -> dict[str, Any]:
        try:
            response = httpx.get(
                f"{self.base_url}{path}", params=params or None, timeout=self._timeout
            )
        except httpx.HTTPError as error:
            raise DatasetUnavailable(
                "could not reach the live service", url=f"{self.base_url}{path}"
            ) from error
        if response.status_code == 404:
            raise DatasetUnavailable("no live price for this symbol", path=path)
        if response.status_code != 200:
            raise DatasetUnavailable(
                "unexpected response from the live service",
                path=path,
                status=response.status_code,
            )
        payload: dict[str, Any] = response.json()
        return payload

    def snapshot(self, assets: list[str] | None = None, limit: int | None = None) -> dict[str, Any]:
        """Current live prices, optionally narrowed to some symbols."""
        params: dict[str, str] = {}
        if assets:
            params["assets"] = ",".join(assets)
        if limit:
            params["limit"] = str(limit)
        return self._get("/v1/live", **params)

    def asset(self, symbol: str) -> dict[str, Any]:
        """One asset's live price with every venue's contribution."""
        return self._get(f"/v1/live/{symbol.upper()}")

    def price(self, symbol: str) -> float:
        """One asset's live reference price in USD."""
        return float(self.asset(symbol)["price"])

    def health(self) -> dict[str, Any]:
        """The service's operational state."""
        return self._get("/v1/health")

    # ----------------------------------------------------------------- streaming

    def _stream_url(self, assets: list[str] | None) -> tuple[str, dict[str, str]]:
        params = {"assets": ",".join(assets)} if assets else {}
        return f"{self.base_url}/v1/stream", params

    def stream(self, assets: list[str] | None = None) -> Iterator[dict[str, Any]]:
        """Yield one record per price change, forever, reconnecting as needed.

        The first records are the current snapshot; every later record is an update.
        """
        url, params = self._stream_url(assets)
        delay = _RECONNECT_START
        while True:
            try:
                with httpx.stream(
                    "GET",
                    url,
                    params=params,
                    headers={"Accept": "text/event-stream"},
                    timeout=httpx.Timeout(self._timeout, read=60.0),
                ) as response:
                    response.raise_for_status()
                    delay = _RECONNECT_START
                    for _event, payload in _parse_events(response.iter_lines()):
                        yield from payload.get("assets", [])
            except httpx.HTTPError:
                pass
            time.sleep(delay)
            delay = min(delay * 2.0, _RECONNECT_CAP)

    async def astream(self, assets: list[str] | None = None) -> AsyncIterator[dict[str, Any]]:
        """Asynchronous form of :meth:`stream`."""
        import asyncio

        url, params = self._stream_url(assets)
        delay = _RECONNECT_START
        while True:
            try:
                async with (
                    httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, read=60.0)) as client,
                    client.stream(
                        "GET", url, params=params, headers={"Accept": "text/event-stream"}
                    ) as response,
                ):
                    response.raise_for_status()
                    delay = _RECONNECT_START
                    async for _event, payload in _aparse_events(response.aiter_lines()):
                        for record in payload.get("assets", []):
                            yield record
            except httpx.HTTPError:
                pass
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, _RECONNECT_CAP)
