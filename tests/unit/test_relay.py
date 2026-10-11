"""Relay tests: publishing a collector's results and pulling them safely.

No network: the relay's web server is a mock transport that serves a temporary directory, so
what is verified is the contract (checksums, freshness, venue identity, https, size) rather
than any particular host.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.conftest import venue_for
from vizanix_atlas.core.atlas_time import to_epoch_ms, utc_now
from vizanix_atlas.core.errors import ConfigurationError
from vizanix_atlas.discovery import relay
from vizanix_atlas.discovery.relay import (
    MANIFEST_FILENAME,
    failure_result,
    fetch_relay,
    publish_relay,
)
from vizanix_atlas.models.observations import AdapterHealth, CollectionResult

BASE = "https://relay.example/atlas-collector"


def _result(slug: str = "bybit", *, finished_at: int | None = None) -> CollectionResult:
    now = to_epoch_ms(utc_now())
    finished = now if finished_at is None else finished_at
    return CollectionResult(
        venue_slug=slug,
        run_id="local-1",
        collection_started_at=finished - 2000,
        collection_finished_at=finished,
        health=AdapterHealth(venue_slug=slug, status="success", duration_ms=2000),
    )


def _publish(tmp_path: Path, result: CollectionResult | None = None, slug: str = "bybit") -> Path:
    collected = tmp_path / "collected"
    collected.mkdir()
    (collected / f"{slug}.json").write_text((result or _result(slug)).model_dump_json())
    served = tmp_path / "served"
    publish_relay(collected, served, venues=[slug])
    return served


def _client(served: Path, *, tamper: bool = False) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        target = served / name
        if not target.is_file():
            return httpx.Response(404)
        body = target.read_bytes()
        if tamper and name != MANIFEST_FILENAME:
            body = body.replace(b'"success"', b'"degraded"')
        return httpx.Response(200, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relay, "_RETRY_DELAY_SECONDS", 0.0)


async def test_a_published_result_round_trips(tmp_path: Path) -> None:
    served = _publish(tmp_path)
    async with _client(served) as http:
        result = await fetch_relay(venue_for("bybit"), base_url=BASE, client=http)

    assert result.venue_slug == "bybit"
    assert result.health.status == "success"
    assert any("collected by the relay at relay.example" in n for n in result.health.notes)


def test_publish_writes_a_manifest_with_the_file_checksum(tmp_path: Path) -> None:
    served = _publish(tmp_path)
    manifest = relay.RelayManifest.model_validate_json((served / MANIFEST_FILENAME).read_text())

    entry = manifest.files["bybit"]
    assert entry.status == "success"
    assert entry.size_bytes == (served / "bybit.json").stat().st_size
    assert not list(served.glob(".*.tmp")), "no temporary file may be left behind"


def test_publish_skips_an_invalid_result_and_removes_the_stale_file(tmp_path: Path) -> None:
    served = _publish(tmp_path)
    (tmp_path / "collected" / "bybit.json").write_text("{not json")

    manifest = publish_relay(tmp_path / "collected", served, venues=["bybit"])

    assert manifest.files == {}
    assert not (served / "bybit.json").exists(), "a stale file must not outlive its manifest entry"


def test_publish_rejects_a_file_holding_another_venue(tmp_path: Path) -> None:
    collected = tmp_path / "collected"
    collected.mkdir()
    (collected / "bybit.json").write_text(_result("okx").model_dump_json())

    manifest = publish_relay(collected, tmp_path / "served", venues=["bybit"])
    assert manifest.files == {}


async def test_a_stale_result_becomes_an_explicit_failure(tmp_path: Path) -> None:
    old = to_epoch_ms(utc_now()) - 3 * 3600 * 1000
    served = _publish(tmp_path, _result(finished_at=old))
    async with _client(served) as http:
        result = await fetch_relay(venue_for("bybit"), base_url=BASE, client=http)

    assert result.health.status == "failed"
    assert any("old" in note for note in result.health.notes)
    assert result.tickers == () and result.instruments == ()


async def test_a_future_dated_result_is_rejected(tmp_path: Path) -> None:
    future = to_epoch_ms(utc_now()) + 3600 * 1000
    served = _publish(tmp_path, _result(finished_at=future))
    async with _client(served) as http:
        result = await fetch_relay(venue_for("bybit"), base_url=BASE, client=http)
    assert result.health.status == "failed"
    assert any("future" in note for note in result.health.notes)


async def test_a_checksum_mismatch_is_rejected_after_one_retry(tmp_path: Path) -> None:
    served = _publish(tmp_path)
    async with _client(served, tamper=True) as http:
        result = await fetch_relay(venue_for("bybit"), base_url=BASE, client=http)

    assert result.health.status == "failed"
    assert any("checksum" in note for note in result.health.notes)


async def test_a_missing_venue_in_the_manifest_is_a_failure(tmp_path: Path) -> None:
    served = _publish(tmp_path)
    async with _client(served) as http:
        result = await fetch_relay(venue_for("binance_derivatives"), base_url=BASE, client=http)
    assert result.health.status == "failed"
    assert any("no result for this venue" in note for note in result.health.notes)


async def test_an_unreachable_relay_is_a_failure_not_an_exception(tmp_path: Path) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        result = await fetch_relay(venue_for("bybit"), base_url=BASE, client=http)
    assert result.health.status == "failed"
    assert any("unreachable" in note for note in result.health.notes)


async def test_an_http_error_status_is_a_failure(tmp_path: Path) -> None:
    async with _client(tmp_path / "empty") as http:
        result = await fetch_relay(venue_for("bybit"), base_url=BASE, client=http)
    assert result.health.status == "failed"
    assert any("404" in note for note in result.health.notes)


async def test_a_redirect_away_from_https_is_refused(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"Location": "http://relay.example/plain"})
        return httpx.Response(200, content=b"{}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        result = await fetch_relay(venue_for("bybit"), base_url=BASE, client=http)
    assert result.health.status == "failed"
    assert any("https" in note for note in result.health.notes)


async def test_an_oversized_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = _publish(tmp_path)
    monkeypatch.setattr(relay, "MAX_FILE_BYTES", 64)
    async with _client(served) as http:
        result = await fetch_relay(venue_for("bybit"), base_url=BASE, client=http)
    assert result.health.status == "failed"
    assert any("size limit" in note for note in result.health.notes)


async def test_a_venue_that_is_not_a_relay_venue_cannot_be_pulled(tmp_path: Path) -> None:
    """A relay must never supply a venue the workflow runners collect themselves."""
    served = _publish(tmp_path, slug="okx")
    async with _client(served) as http:
        with pytest.raises(ConfigurationError):
            await fetch_relay(venue_for("okx"), base_url=BASE, client=http)


async def test_a_plain_http_relay_url_is_refused() -> None:
    with pytest.raises(ConfigurationError):
        await fetch_relay(venue_for("bybit"), base_url="http://relay.example/atlas-collector")


def test_failure_result_is_a_complete_valid_result() -> None:
    result = failure_result(venue_for("bybit"), "boom")
    assert result.health.status == "failed"
    assert result.health.notes == ("relay result unusable: boom",)
    CollectionResult.model_validate_json(result.model_dump_json())
