"""Shared test fixtures.

The suite never contacts a real exchange. Adapter tests are driven by the recorded
payloads in ``tests/fixtures/exchanges/``, served through an httpx mock transport,
so a venue being unavailable can never fail CI.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from vizanix_atlas.core.config import clear_config_cache, load_exchange_registry
from vizanix_atlas.core.http import ExchangeHttpClient
from vizanix_atlas.models.venue import RateLimitPolicy, Venue

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
EXCHANGE_FIXTURES = FIXTURE_ROOT / "exchanges"


@pytest.fixture(autouse=True)
def _reset_config_cache() -> Iterator[None]:
    """Clear cached configuration around every test.

    Configuration loaders are cached for collection performance, which would
    otherwise leak a temporary config file from one test into the next.
    """
    clear_config_cache()
    yield
    clear_config_cache()


def load_fixture(venue: str, name: str) -> Any:
    """Load a recorded exchange payload.

    Raises:
        FileNotFoundError: With the available names listed, because a typo here is
            otherwise a confusing failure.
    """
    path = EXCHANGE_FIXTURES / venue / f"{name}.json"
    if not path.is_file():
        available = sorted(p.stem for p in (EXCHANGE_FIXTURES / venue).glob("*.json"))
        raise FileNotFoundError(f"no fixture {venue}/{name}.json; available: {available}")
    return json.loads(path.read_text(encoding="utf-8"))


def fast_policy(**overrides: Any) -> RateLimitPolicy:
    """Return a rate-limit policy that does not slow the test suite down.

    Rate limiting is correct and tested separately in ``tests/unit/test_ratelimit.py``;
    adapter tests should not spend wall-clock time waiting for tokens.
    """
    settings: dict[str, Any] = {
        "requests_per_second": 1000.0,
        "burst": 64,
        "max_concurrency": 8,
        "request_timeout_seconds": 5.0,
        "max_retries": 0,
    }
    settings.update(overrides)
    return RateLimitPolicy(**settings)


class RouteRecorder:
    """Routes mock HTTP requests to fixture payloads and records what was asked for.

    Recording the requests is what lets a test assert that an adapter used bulk
    endpoints rather than one request per symbol, which is the property that keeps
    Atlas's request volume defensible.
    """

    def __init__(self) -> None:
        self.routes: list[tuple[Callable[[httpx.Request], bool], httpx.Response]] = []
        self.requests: list[httpx.Request] = []

    def add(
        self,
        matcher: Callable[[httpx.Request], bool],
        payload: Any = None,
        *,
        status: int = 200,
        content: bytes | None = None,
    ) -> RouteRecorder:
        """Register a route. Returns ``self`` so registrations can be chained."""
        response = (
            httpx.Response(status, content=content)
            if content is not None
            else httpx.Response(status, json=payload)
        )
        self.routes.append((matcher, response))
        return self

    def on_path(self, path: str, payload: Any, **kwargs: Any) -> RouteRecorder:
        """Register a route matching an exact request path."""
        return self.add(lambda r, p=path: r.url.path == p, payload, **kwargs)

    def on_path_param(
        self, path: str, param: str, value: str, payload: Any, **kwargs: Any
    ) -> RouteRecorder:
        """Register a route matching a path plus one query parameter value."""
        return self.add(
            lambda r, p=path, k=param, v=value: r.url.path == p and r.url.params.get(k) == v,
            payload,
            **kwargs,
        )

    def transport(self) -> httpx.MockTransport:
        """Build the transport to hand to :class:`ExchangeHttpClient`."""

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            for matcher, response in self.routes:
                if matcher(request):
                    return response
            # An unmatched request is a test bug or an adapter reaching for an
            # endpoint the test did not anticipate. Both should be loud.
            return httpx.Response(
                404, json={"error": f"no mock route for {request.method} {request.url}"}
            )

        return httpx.MockTransport(handler)

    @property
    def request_count(self) -> int:
        """How many HTTP requests the adapter issued."""
        return len(self.requests)

    def paths(self) -> list[str]:
        """The request paths, in order."""
        return [r.url.path for r in self.requests]


@pytest.fixture
def routes() -> RouteRecorder:
    """Provide a fresh route recorder."""
    return RouteRecorder()


def make_client(
    recorder: RouteRecorder, *, venue_slug: str, base_url: str, **policy_overrides: Any
) -> ExchangeHttpClient:
    """Build an HTTP client wired to a route recorder."""
    return ExchangeHttpClient(
        venue_slug=venue_slug,
        base_url=base_url,
        policy=fast_policy(**policy_overrides),
        transport=recorder.transport(),
    )


def venue_for(slug: str) -> Venue:
    """Load a venue's real declared configuration from ``config/exchanges.yaml``.

    Adapter tests use the shipped configuration rather than a hand-built stub, so a
    capability declared in the registry is exercised by the same code path
    collection uses.
    """
    return load_exchange_registry().get(slug)
