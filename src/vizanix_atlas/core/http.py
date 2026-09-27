"""The HTTP client every adapter uses.

Every exchange payload is untrusted input. The client therefore enforces, for all
adapters at once:

- connect, read, write and pool deadlines
- a hard response body budget, applied while streaming so that an oversized body is
  abandoned rather than buffered
- a JSON nesting-depth ceiling, so a pathological document cannot exhaust the
  interpreter's stack during parsing
- bounded concurrency and a token bucket per venue
- retries only for errors the taxonomy marks retryable, with jittered backoff and
  the venue's own ``Retry-After`` respected
- a circuit breaker, so a degraded venue is left alone

Remote text is never evaluated. Responses are parsed with :mod:`json` only.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import TracebackType
from typing import Any, Final

import httpx

from vizanix_atlas.core.errors import (
    AtlasError,
    ConnectionFailure,
    DnsFailure,
    GeoRestricted,
    HttpError,
    NetworkTimeout,
    RateLimited,
    ResponseTooLarge,
    SchemaMismatch,
)
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.core.ratelimit import (
    CircuitBreaker,
    ConcurrencyGate,
    RequestMetrics,
    RetryPolicy,
    TokenBucket,
)
from vizanix_atlas.models.venue import RateLimitPolicy

_log = get_logger(__name__)

#: Identifies Atlas to venues. A venue that wants to contact or throttle Atlas
#: specifically can do so; Atlas never imitates a browser to defeat a control.
USER_AGENT: Final = (
    "VizanixAtlas/0.1.0 (+https://github.com/Vizanix/Atlas; open-source market-data collector)"
)

#: Statuses venues use for jurisdiction refusals. Separated from generic HTTP
#: errors because they are a property of the collector's network, not the venue's
#: health, and because retrying from the same address cannot succeed.
_GEO_STATUSES: Final = frozenset({403, 451})

#: Nesting deeper than this is not a market-data document.
_MAX_JSON_DEPTH: Final = 40

#: Streaming chunk size. Large enough to be efficient, small enough that the body
#: budget is enforced long before a runaway response is fully received.
_CHUNK_BYTES: Final = 256 * 1024


def _json_depth(value: Any, *, limit: int) -> int:
    """Return the nesting depth of ``value``, stopping once ``limit`` is exceeded.

    Iterative rather than recursive: a recursive walk over a deliberately deep
    document would hit the interpreter's own recursion limit, which is the thing
    this check exists to prevent.
    """
    deepest = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        deepest = max(deepest, depth)
        if depth > limit:
            return depth
        if isinstance(node, dict):
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)
    return deepest


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Read a ``Retry-After`` header as seconds, if present and numeric.

    Only the delta-seconds form is honoured. The HTTP-date form is left to the
    backoff schedule rather than parsed, because misreading it would be worse than
    ignoring it.
    """
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class ExchangeHttpClient:
    """A rate-limited, bounded HTTP client scoped to one venue.

    One instance per venue per run. Constructed by the adapter runtime rather than
    by adapters themselves, so that every adapter inherits the same guarantees.
    """

    def __init__(
        self,
        *,
        venue_slug: str,
        base_url: str,
        policy: RateLimitPolicy,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create a client for ``venue_slug``.

        Args:
            venue_slug: Used in logs and metrics.
            base_url: Prefix for relative request paths.
            policy: The venue's rate-limit and timeout settings.
            transport: Injected for tests, so the suite never needs a real socket.
        """
        self.venue_slug = venue_slug
        self.base_url = base_url.rstrip("/")
        self.policy = policy
        self.metrics = RequestMetrics()
        self.circuit = CircuitBreaker(reset_after_seconds=45.0)

        self._bucket = TokenBucket(rate=policy.requests_per_second, capacity=policy.burst)
        self._gate = ConcurrencyGate(policy.max_concurrency)
        self._retry = RetryPolicy(max_retries=policy.max_retries)

        timeout = httpx.Timeout(
            connect=min(10.0, policy.request_timeout_seconds),
            read=policy.request_timeout_seconds,
            write=policy.request_timeout_seconds,
            pool=policy.request_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=False,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                # Compression is accepted, but the decompressed body is still
                # measured against the budget, so a compression bomb is bounded.
                "Accept-Encoding": "gzip, deflate",
            },
            limits=httpx.Limits(
                max_connections=policy.max_concurrency,
                max_keepalive_connections=policy.max_concurrency,
            ),
            transport=transport,
            # Environment proxy settings are respected; Atlas never rotates or
            # overrides them to reach a venue that has refused it.
            trust_env=True,
        )

    async def __aenter__(self) -> ExchangeHttpClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._client.aclose()

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        operation: str,
        max_bytes: int | None = None,
    ) -> Any:
        """Issue a ``GET`` and return the parsed JSON body.

        Raises:
            AtlasError: A subclass describing what went wrong. Never a bare
                exception, so callers can classify without inspecting strings.
        """
        return await self._request_json(
            "GET", path, params=params, json_body=None, operation=operation, max_bytes=max_bytes
        )

    async def post_json(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
        operation: str,
        max_bytes: int | None = None,
    ) -> Any:
        """Issue a ``POST`` with a JSON body and return the parsed response.

        Needed because Hyperliquid's public info endpoint is POST-only.
        """
        return await self._request_json(
            "POST", path, params=None, json_body=json_body, operation=operation, max_bytes=max_bytes
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        operation: str,
        max_bytes: int | None,
    ) -> Any:
        """Send one logical request, retrying retryable failures.

        Attempt 0 is the first try; attempts 1..max_retries are retries.
        """
        budget = max_bytes or self.policy.max_response_bytes
        attempt = 0
        while True:
            self.circuit.check()
            await self._bucket.acquire()
            started = time.monotonic()
            try:
                async with self._gate:
                    payload = await self._send_once(
                        method, path, params=params, json_body=json_body, budget=budget
                    )
            except AtlasError as error:
                duration_ms = int((time.monotonic() - started) * 1000)
                self.metrics.record_error(error)
                if error.counts_against_health:
                    self.circuit.record_failure()
                attempt += 1
                if not self._retry.should_retry(error, attempt):
                    _log.warning(
                        "request failed",
                        extra={
                            "venue": self.venue_slug,
                            "operation": operation,
                            "status": "failed",
                            "error_type": error.error_type,
                            "duration_ms": duration_ms,
                            "attempts": attempt,
                        },
                    )
                    raise
                delay = self._retry.delay_for(error, attempt)
                self.metrics.retries += 1
                _log.info(
                    "retrying request",
                    extra={
                        "venue": self.venue_slug,
                        "operation": operation,
                        "status": "retry",
                        "error_type": error.error_type,
                        "attempt": attempt,
                        "delay_seconds": round(delay, 2),
                    },
                )
                await asyncio.sleep(delay)
                continue

            self.metrics.successful += 1
            self.circuit.record_success()
            _log.debug(
                "request succeeded",
                extra={
                    "venue": self.venue_slug,
                    "operation": operation,
                    "status": "success",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
            return payload

    async def _send_once(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        budget: int,
    ) -> Any:
        """Send exactly one HTTP request and parse its body.

        Raises:
            AtlasError: Classified from the transport failure or the response.
        """
        self.metrics.attempted += 1
        request = self._client.build_request(method, path, params=params, json=json_body)
        try:
            response = await self._client.send(request, stream=True)
        except httpx.ConnectTimeout as exc:
            raise NetworkTimeout("connect timed out", venue=self.venue_slug, path=path) from exc
        except httpx.ReadTimeout as exc:
            raise NetworkTimeout("read timed out", venue=self.venue_slug, path=path) from exc
        except httpx.TimeoutException as exc:
            raise NetworkTimeout("request timed out", venue=self.venue_slug, path=path) from exc
        except httpx.ConnectError as exc:
            # httpx surfaces DNS failures as ConnectError; the distinction matters
            # because a persistent DNS failure usually means a blocked host.
            message = str(exc).lower()
            if "name" in message or "resolve" in message or "nodename" in message:
                raise DnsFailure(
                    "hostname could not be resolved", venue=self.venue_slug, path=path
                ) from exc
            raise ConnectionFailure(
                "connection failed", venue=self.venue_slug, path=path
            ) from exc
        except httpx.HTTPError as exc:
            raise ConnectionFailure(
                "transport error", venue=self.venue_slug, path=path, detail=str(exc)
            ) from exc

        try:
            body = await self._read_bounded(response, budget=budget, path=path)
        finally:
            await response.aclose()

        self.metrics.bytes_received += len(body)
        self._raise_for_status(response, body, path=path)

        try:
            document = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SchemaMismatch(
                "response body was not valid JSON",
                venue=self.venue_slug,
                path=path,
                body_bytes=len(body),
                preview=body[:160].decode("utf-8", errors="replace"),
            ) from exc

        depth = _json_depth(document, limit=_MAX_JSON_DEPTH)
        if depth > _MAX_JSON_DEPTH:
            raise SchemaMismatch(
                "response JSON nests more deeply than any market-data document should",
                venue=self.venue_slug,
                path=path,
                depth=depth,
            )
        return document

    async def _read_bounded(self, response: httpx.Response, *, budget: int, path: str) -> bytes:
        """Stream a response body, abandoning it once ``budget`` is exceeded.

        Streaming rather than reading whole is what makes the budget meaningful: a
        buffered read would have already consumed the memory by the time the size
        could be checked.

        Raises:
            ResponseTooLarge: If the body exceeds ``budget``.
            ConnectionFailure: If the stream is cut off mid-transfer.
        """
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in response.aiter_bytes(_CHUNK_BYTES):
                total += len(chunk)
                if total > budget:
                    raise ResponseTooLarge(
                        "response exceeded the configured body budget",
                        venue=self.venue_slug,
                        path=path,
                        budget_bytes=budget,
                        received_bytes=total,
                    )
                chunks.append(chunk)
        except httpx.TimeoutException as exc:
            raise NetworkTimeout(
                "timed out while reading the response body", venue=self.venue_slug, path=path
            ) from exc
        except httpx.HTTPError as exc:
            raise ConnectionFailure(
                "response stream ended unexpectedly", venue=self.venue_slug, path=path
            ) from exc
        return b"".join(chunks)

    def _raise_for_status(self, response: httpx.Response, body: bytes, *, path: str) -> None:
        """Convert an unsuccessful status into the right error class.

        Raises:
            RateLimited: On ``429``.
            GeoRestricted: On ``403`` or ``451``, which venues use for jurisdiction
                refusals.
            HttpError: On any other unsuccessful status.
        """
        status = response.status_code
        if status < 400:
            return
        preview = body[:200].decode("utf-8", errors="replace")
        if status == 429:
            raise RateLimited(
                "venue reported too many requests",
                retry_after_seconds=_parse_retry_after(response),
                venue=self.venue_slug,
                path=path,
                preview=preview,
            )
        if status in _GEO_STATUSES:
            raise GeoRestricted(
                "venue refused the request from this network",
                venue=self.venue_slug,
                path=path,
                status_code=status,
                preview=preview,
            )
        raise HttpError(
            "venue returned an unsuccessful status",
            status_code=status,
            venue=self.venue_slug,
            path=path,
            preview=preview,
        )

    @property
    def peak_concurrency(self) -> int:
        """Greatest number of simultaneous requests observed, for diagnostics."""
        return self._gate.peak_in_flight
