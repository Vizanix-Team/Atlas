"""OKX adapter contract tests.

Driven entirely by the payloads recorded in ``tests/fixtures/exchanges/okx/``.
The assertions encode the unit semantics verified at capture time, so that a
regression in the conversion arithmetic fails here rather than silently changing a
published volume figure.
"""

from __future__ import annotations

import pytest

from tests.conftest import RouteRecorder, make_client, venue_for
from tests.contract.wiring import WIRING
from vizanix_atlas.adapters.okx import OkxAdapter
from vizanix_atlas.models.enums import OpenInterestUnit


def _wire(recorder: RouteRecorder) -> OkxAdapter:
    """Wire OKX to its recorded payloads using the shared fixture routing table."""
    WIRING["okx"](recorder)
    client = make_client(recorder, venue_slug="okx", base_url=OkxAdapter.base_url)
    return OkxAdapter(venue_for("okx"), client)


async def test_discovers_instruments_from_the_venue_catalogue(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    instruments = await adapter.discover_instruments()

    assert instruments, "discovery must return instruments from the venue catalogue"
    by_symbol = {i.symbol_native: i for i in instruments}

    spot = by_symbol["BTC-USDT"]
    assert spot.instrument_type == "spot"
    assert spot.instrument_class == "spot"
    assert (spot.base_symbol_native, spot.quote_symbol_native) == ("BTC", "USDT")
    # A spot instrument must carry no contract semantics at all.
    assert spot.contract_multiplier is None
    assert spot.contract_type is None

    linear = by_symbol["BTC-USDT-SWAP"]
    assert linear.instrument_type == "perpetual"
    assert linear.instrument_class == "linear-perp"
    assert linear.contract_type == "linear"
    # Verified from the live catalogue: ctVal 0.01 BTC, ctMult 1.
    assert linear.contract_multiplier == pytest.approx(0.01)
    assert linear.contract_value_symbol_native == "BTC"

    inverse = by_symbol["BTC-USD-SWAP"]
    assert inverse.instrument_class == "inverse-perp"
    assert inverse.contract_type == "inverse"
    # Verified: ctVal 100, denominated in USD.
    assert inverse.contract_multiplier == pytest.approx(100.0)
    assert inverse.contract_value_symbol_native == "USD"
    # Derivatives report open interest in contracts on OKX.
    assert inverse.open_interest_unit == OpenInterestUnit.CONTRACTS.value


async def test_uses_bulk_endpoints_only(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    await adapter.discover_instruments()
    await adapter.fetch_tickers()

    # Discovery: three product types, plus the option family list, plus one request
    # per option family. Tickers: four product types. If any of this ever becomes
    # per-symbol, the count explodes and this test fails.
    assert routes.request_count == 9, routes.paths()
    ceiling = venue_for("okx").rate_limit.max_concurrency
    assert ceiling >= 1


async def test_derivative_volume_is_not_recorded_as_quote_volume(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    tickers = {t.symbol_native: t for t in await adapter.fetch_tickers()}

    spot = tickers["BTC-USDT"]
    # For spot, OKX vol24h is base and volCcy24h is quote.
    assert spot.base_volume_24h is not None
    assert spot.quote_volume_24h is not None
    assert spot.contract_volume_24h is None

    swap = tickers["BTC-USDT-SWAP"]
    # For derivatives, vol24h is contracts and volCcy24h is base units. Quote volume
    # is left null rather than derived from a price the venue did not quote against.
    assert swap.contract_volume_24h is not None
    assert swap.base_volume_24h is not None
    assert swap.quote_volume_24h is None


async def test_swap_base_volume_equals_contracts_times_multiplier(routes: RouteRecorder) -> None:
    """The arithmetic identity verified against the live venue at capture time."""
    adapter = _wire(routes)
    instruments = {i.symbol_native: i for i in await adapter.discover_instruments()}
    tickers = {t.symbol_native: t for t in await adapter.fetch_tickers()}

    linear = instruments["BTC-USDT-SWAP"]
    ticker = tickers["BTC-USDT-SWAP"]
    assert linear.contract_multiplier is not None
    assert ticker.contract_volume_24h is not None
    assert ticker.base_volume_24h is not None
    assert ticker.base_volume_24h == pytest.approx(
        ticker.contract_volume_24h * linear.contract_multiplier, rel=1e-9
    )


async def test_funding_interval_is_derived_not_assumed(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    observations = {d.symbol_native: d for d in await adapter.fetch_derivatives()}

    assert observations, "funding collection must return observations"
    intervals = {
        d.funding_interval_hours for d in observations.values() if d.funding_interval_hours
    }
    assert intervals, "at least one funding interval must be derivable from venue timestamps"
    # Every derived interval must be a real funding cycle length.
    assert all(0.5 <= i <= 24.0 for i in intervals)

    btc = observations["BTC-USDT-SWAP"]
    assert btc.funding_rate_raw is not None
    assert btc.open_interest_unit == OpenInterestUnit.CONTRACTS
    assert btc.open_interest_raw is not None
    assert btc.open_interest_base is not None
    assert btc.open_interest_usd is not None


async def test_application_error_is_not_silently_accepted(routes: RouteRecorder) -> None:
    from vizanix_atlas.core.errors import ExchangeApplicationError

    routes.on_path("/api/v5/public/time", {"code": "50011", "msg": "rate limited", "data": []})
    client = make_client(routes, venue_slug="okx", base_url=OkxAdapter.base_url)
    adapter = OkxAdapter(venue_for("okx"), client)

    # OKX answers 200 OK with a non-zero code. Accepting it would produce an empty
    # generation that looked successful.
    with pytest.raises(ExchangeApplicationError) as excinfo:
        await adapter.server_time_ms()
    assert excinfo.value.venue_code == "50011"


async def test_order_book_levels_are_ordered_best_first(routes: RouteRecorder) -> None:
    adapter = _wire(routes)
    book = await adapter.fetch_order_book("BTC-USDT", depth=10)

    assert book.bids and book.asks
    assert [b.price for b in book.bids] == sorted((b.price for b in book.bids), reverse=True)
    assert [a.price for a in book.asks] == sorted(a.price for a in book.asks)
    assert not book.is_crossed
    assert book.best_bid is not None and book.best_ask is not None
    assert book.best_bid < book.best_ask
