"""Rate limiting, retry and circuit breaking.

Atlas collects from public APIs it does not own, from a shared runner address. The
policy here is deliberately conservative: bounded concurrency, a token bucket well
below any documented ceiling, the venue's own ``Retry-After`` honoured in
preference to Atlas's schedule, jittered backoff, a hard retry ceiling, and a
circuit breaker so that a degraded venue is left alone rather than hammered.

Nothing here attempts to work around a venue's controls. A refusal is recorded and
respected (see ``docs/DATA_POLICY.md``).
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from types import TracebackType

from vizanix_atlas.core.errors import AtlasError, CircuitOpen, RateLimited
from vizanix_atlas.core.logging import get_logger

_log = get_logger(__name__)

# Backoff is seeded from a dedicated generator so that jitter never depends on,
# nor perturbs, the global random state. Analytics must stay deterministic; only
# retry timing is randomised, and it never influences published values.
_jitter = random.Random(0x41544C4153)  # noqa: S311 - timing jitter, not cryptography


class TokenBucket:
    """An asyncio token bucket.

    Tokens accrue continuously at ``rate`` up to ``capacity``. A caller awaits
    :meth:`acquire` before each request, so a burst is bounded by the capacity and
    the sustained rate is bounded by ``rate`` regardless of how many coroutines are
    waiting.

    Uses a monotonic clock, so a system clock adjustment mid-run cannot grant a
    windfall of tokens.
    """

    def __init__(self, *, rate: float, capacity: int) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._rate = rate
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """Wait until ``tokens`` are available, then consume them.

        Raises:
            ValueError: If more tokens are requested than the bucket can ever hold,
                which would otherwise deadlock.
        """
        if tokens > self._capacity:
            raise ValueError(
                f"requested {tokens} tokens from a bucket with capacity {self._capacity}"
            )
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self._rate
            # Sleep outside the lock so that other holders keep making progress.
            await asyncio.sleep(wait)

    def _refill(self) -> None:
        """Add the tokens that have accrued since the last update."""
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._updated = now

    @property
    def available(self) -> float:
        """Tokens currently available, for diagnostics."""
        self._refill()
        return self._tokens


@dataclass(slots=True)
class CircuitBreaker:
    """Stops sending to a venue that is consistently failing.

    Opens after ``failure_threshold`` consecutive failures and stays open for
    ``reset_after_seconds``. While open, calls raise :class:`CircuitOpen` without a
    request being made, which both respects the venue and stops the adapter from
    spending its whole job timeout on a dead endpoint.

    Deliberately simple: the breaker lives for one collection run, so there is no
    half-open probing state to manage. A new run starts with a closed breaker.
    """

    failure_threshold: int = 5
    reset_after_seconds: float = 45.0
    consecutive_failures: int = 0
    opened_at: float | None = None
    open_count: int = 0

    def check(self) -> None:
        """Raise if the circuit is currently open.

        Raises:
            CircuitOpen: While the breaker is open and the cooldown has not elapsed.
        """
        if self.opened_at is None:
            return
        elapsed = time.monotonic() - self.opened_at
        if elapsed < self.reset_after_seconds:
            raise CircuitOpen(
                "circuit is open for this venue",
                seconds_remaining=round(self.reset_after_seconds - elapsed, 1),
            )
        # Cooldown elapsed: close and let the next request decide.
        self.opened_at = None
        self.consecutive_failures = 0

    def record_success(self) -> None:
        """Reset the failure run after a successful request."""
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        """Count a failure and open the circuit once the threshold is reached."""
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold and self.opened_at is None:
            self.opened_at = time.monotonic()
            self.open_count += 1
            _log.warning(
                "circuit opened",
                extra={
                    "consecutive_failures": self.consecutive_failures,
                    "reset_after_seconds": self.reset_after_seconds,
                },
            )

    @property
    def is_open(self) -> bool:
        """Whether the circuit is open right now."""
        if self.opened_at is None:
            return False
        return (time.monotonic() - self.opened_at) < self.reset_after_seconds


def backoff_delay(
    attempt: int,
    *,
    base_seconds: float = 0.6,
    max_seconds: float = 20.0,
    retry_after: float | None = None,
) -> float:
    """Return how long to wait before retry ``attempt`` (1-based).

    The venue's own ``Retry-After`` wins when present: it is the venue telling Atlas
    what it wants, and second-guessing it is how a client gets blocked. Otherwise
    the delay is exponential with full jitter, which spreads retries from parallel
    adapters instead of synchronising them into a second burst.

    Args:
        attempt: Retry number, starting at 1.
        base_seconds: Delay for the first retry, before jitter.
        max_seconds: Ceiling, so a long retry chain cannot consume a job timeout.
        retry_after: The venue's instruction, in seconds, if it gave one.
    """
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, max_seconds)
    exponential = min(max_seconds, base_seconds * (2 ** max(0, attempt - 1)))
    return _jitter.uniform(0.0, exponential)


@dataclass(slots=True)
class RetryPolicy:
    """How many times a retryable failure is re-attempted, and how it is spaced."""

    max_retries: int = 3
    base_seconds: float = 0.6
    max_seconds: float = 20.0

    def should_retry(self, error: AtlasError, attempt: int) -> bool:
        """Return whether ``error`` on ``attempt`` warrants another try.

        Only errors the taxonomy marks retryable qualify. A ``404`` or a schema
        mismatch is never retried, because the same request returns the same answer.
        """
        return error.retryable and attempt <= self.max_retries

    def delay_for(self, error: AtlasError, attempt: int) -> float:
        """Return the delay before the next attempt."""
        retry_after = error.retry_after_seconds if isinstance(error, RateLimited) else None
        return backoff_delay(
            attempt,
            base_seconds=self.base_seconds,
            max_seconds=self.max_seconds,
            retry_after=retry_after,
        )


@dataclass(slots=True)
class RequestMetrics:
    """Counters accumulated across one venue's requests in one run.

    Fed into :class:`~vizanix_atlas.models.observations.AdapterHealth` so that
    partial coverage is attributable after the fact.
    """

    attempted: int = 0
    successful: int = 0
    timeouts: int = 0
    rate_limited: int = 0
    http_errors: int = 0
    application_errors: int = 0
    parse_failures: int = 0
    retries: int = 0
    bytes_received: int = 0
    error_types: dict[str, int] = field(default_factory=dict)
    unknown_enum_values: dict[str, int] = field(default_factory=dict)

    def record_error(self, error: AtlasError) -> None:
        """Classify an error into the counters."""
        from vizanix_atlas.core.errors import (
            ExchangeApplicationError,
            HttpError,
            NetworkTimeout,
            SchemaMismatch,
        )

        key = error.error_type
        self.error_types[key] = self.error_types.get(key, 0) + 1
        if isinstance(error, NetworkTimeout):
            self.timeouts += 1
        elif isinstance(error, RateLimited):
            self.rate_limited += 1
        elif isinstance(error, HttpError):
            self.http_errors += 1
        elif isinstance(error, ExchangeApplicationError):
            self.application_errors += 1
        elif isinstance(error, SchemaMismatch):
            self.parse_failures += 1

    def record_unknown_enum(self, field_name: str, value: str) -> None:
        """Note an unrecognised value in an enumerated venue field.

        A rising count here is the earliest signal that a venue changed its schema
        in a way the adapter has not been taught about yet.
        """
        key = f"{field_name}={value}"
        self.unknown_enum_values[key] = self.unknown_enum_values.get(key, 0) + 1

    @property
    def parse_failure_ratio(self) -> float:
        """Parse failures over successful requests, or 0 when nothing succeeded."""
        return self.parse_failures / self.successful if self.successful else 0.0


class ConcurrencyGate:
    """Bounds simultaneous in-flight requests, as an async context manager.

    A thin wrapper over :class:`asyncio.Semaphore` that also tracks the observed
    peak, which is what tells an operator whether the configured bound is the
    binding constraint or whether the token bucket is.
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._semaphore = asyncio.Semaphore(limit)
        self._limit = limit
        self._in_flight = 0
        self._peak = 0

    async def __aenter__(self) -> ConcurrencyGate:
        await self._semaphore.acquire()
        self._in_flight += 1
        self._peak = max(self._peak, self._in_flight)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._in_flight -= 1
        self._semaphore.release()

    @property
    def peak_in_flight(self) -> int:
        """Greatest number of simultaneous requests observed."""
        return self._peak

    @property
    def limit(self) -> int:
        """The configured bound."""
        return self._limit
