"""BitMEX is closed, and Atlas must say so rather than collect a dead venue.

The two fixtures are real responses captured on 2026-10-11 from an unrestricted network:
the active catalogue contains only unlisted placeholders and the interval list is empty.
They are the evidence behind the ``disabled_reason`` in ``config/exchanges.yaml``.
"""

from __future__ import annotations

import pytest

from tests.conftest import RouteRecorder, load_fixture, make_client, venue_for
from vizanix_atlas.adapters.disabled import BitmexAdapter
from vizanix_atlas.core.config import load_exchange_registry
from vizanix_atlas.core.errors import UnsupportedCapability


def test_the_recorded_catalogue_has_no_live_instrument() -> None:
    catalogue = load_fixture("bitmex", "instrument_active_closed")
    assert catalogue, "the fixture must hold the captured catalogue"
    assert {row["state"] for row in catalogue} == {"Unlisted"}
    assert all(row["listing"].startswith("2200-") for row in catalogue)
    assert all(not row["hasLiquidity"] and row["volume24h"] == 0 for row in catalogue)
    assert load_fixture("bitmex", "active_intervals_closed") == {"intervals": [], "symbols": []}


def test_the_venue_is_disabled_with_the_real_reason() -> None:
    venue = load_exchange_registry().get("bitmex")
    assert venue.enabled is False
    assert venue.disabled_reason and "closed" in venue.disabled_reason.lower()


async def test_the_adapter_refuses_instead_of_returning_empty_data(routes: RouteRecorder) -> None:
    client = make_client(routes, venue_slug="bitmex", base_url=BitmexAdapter.base_url)
    adapter = BitmexAdapter(venue_for("bitmex"), client)

    with pytest.raises(UnsupportedCapability):
        await adapter.discover_instruments()
    with pytest.raises(UnsupportedCapability):
        await adapter.fetch_tickers()
    assert routes.request_count == 0, "a closed venue must not be contacted"
