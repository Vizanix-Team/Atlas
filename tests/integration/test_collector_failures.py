"""Failure-mode tests for the collection runtime, against fake venues.

Real venues are not required to be up for CI to pass: every response here is produced by
an in-process mock transport. The property under test is the one the architecture depends
on: **one venue's failure is recorded, never raised, and never affects another venue.**
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import RouteRecorder, venue_for
from tests.contract.wiring import WIRING
from vizanix_atlas.core.config import load_collection_config
from vizanix_atlas.discovery.collector import CollectionRequest, collect_many, collect_venue
from vizanix_atlas.models.enums import CollectionStatus


def _request(slug: str, recorder: RouteRecorder) -> CollectionRequest:
    return CollectionRequest(venue=venue_for(slug), run_id="test", transport=recorder.transport())


async def _collect(slug: str, recorder: RouteRecorder):
    return await collect_venue(_request(slug, recorder), load_collection_config())


async def test_healthy_venue_succeeds() -> None:
    recorder = RouteRecorder()
    WIRING["okx"](recorder)
    result = await _collect("okx", recorder)
    assert result.health.status == CollectionStatus.SUCCESS.value
    assert result.instruments
    assert result.tickers


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_http_errors_are_recorded_not_raised(status: int) -> None:
    recorder = RouteRecorder()
    recorder.add(lambda r: True, {"error": "boom"}, status=status)
    result = await _collect("okx", recorder)
    assert result.health.status == CollectionStatus.FAILED.value
    assert not result.tickers
    assert result.health.notes


async def test_malformed_json_is_a_parse_failure_not_a_crash() -> None:
    recorder = RouteRecorder()
    recorder.add(lambda r: True, content=b"<html>not json</html>")
    result = await _collect("okx", recorder)
    assert result.health.status == CollectionStatus.FAILED.value
    assert not result.tickers


async def test_schema_change_in_ticker_payload_does_not_take_the_venue_down_silently() -> None:
    recorder = RouteRecorder()
    WIRING["okx"](recorder)
    # Override tickers with a payload whose rows lost required fields.
    recorder.routes.insert(
        0,
        (
            lambda r: r.url.path == "/api/v5/market/tickers",
            httpx.Response(200, json={"code": "0", "msg": "", "data": [{"instId": "BTC-USDT"}]}),
        ),
    )
    result = await _collect("okx", recorder)
    assert result.health.status in {
        CollectionStatus.FAILED.value,
        CollectionStatus.DEGRADED.value,
    }
    assert any("schema change" in note for note in result.health.notes)


async def test_a_geo_blocked_venue_is_reported_as_unavailable_not_bypassed() -> None:
    recorder = RouteRecorder()
    recorder.add(lambda r: True, {"msg": "blocked"}, status=451)
    result = await _collect("okx", recorder)
    assert result.health.status == CollectionStatus.UNAVAILABLE_FROM_COLLECTOR_NETWORK.value
    # Exactly the requests the client needed; no retry storm, no alternate host.
    assert len({str(r.url.host) for r in recorder.requests}) == 1


async def test_one_failing_venue_never_affects_another() -> None:
    good, bad = RouteRecorder(), RouteRecorder()
    WIRING["okx"](good)
    bad.add(lambda r: True, {"error": "down"}, status=500)
    results = await collect_many(
        [_request("okx", good), _request("coinbase", bad)], load_collection_config()
    )
    by_slug = {r.venue_slug: r for r in results}
    assert by_slug["okx"].health.status == CollectionStatus.SUCCESS.value
    assert by_slug["coinbase"].health.status == CollectionStatus.FAILED.value


async def test_tier_a_request_ceiling_degrades_a_venue_that_is_not_using_bulk_endpoints() -> None:
    recorder = RouteRecorder()
    WIRING["okx"](recorder)
    config = load_collection_config()
    tight = config.model_copy(
        update={
            "tiers": config.tiers.model_copy(
                update={
                    "a_universal": config.tiers.a_universal.model_copy(
                        update={"max_requests_per_venue": 1}
                    )
                }
            )
        }
    )
    result = await collect_venue(_request("okx", recorder), tight)
    assert result.health.status == CollectionStatus.DEGRADED.value
    assert any("ceiling" in note for note in result.health.notes)
