"""Universal adapter contract tests.

Every enabled adapter is exercised against its recorded payloads and must satisfy the
same invariants. These are the properties Atlas depends on being true for all sixteen
venues, so they are asserted once and parametrised rather than restated per adapter.

Venue-specific semantics live in the per-venue test modules.
"""

from __future__ import annotations

import contextlib

import pytest

from tests.conftest import RouteRecorder, make_client, venue_for
from tests.contract.wiring import BOOK_SYMBOLS, WIRING
from vizanix_atlas.adapters.registry import adapter_class
from vizanix_atlas.core.config import load_exchange_registry
from vizanix_atlas.core.errors import UnsupportedCapability
from vizanix_atlas.models.enums import InstrumentType

ENABLED_SLUGS = sorted(v.slug for v in load_exchange_registry().enabled())

#: Instrument types Atlas models. An adapter must never emit anything else.
VALID_TYPES = {t.value for t in InstrumentType}


def build(slug: str, recorder: RouteRecorder):
    """Wire an adapter to its fixtures."""
    WIRING[slug](recorder)
    venue = venue_for(slug)
    cls = adapter_class(slug)
    return cls(venue, make_client(recorder, venue_slug=slug, base_url=cls.base_url))


@pytest.mark.parametrize("slug", ENABLED_SLUGS)
async def test_discovery_returns_wellformed_instruments(slug: str, routes: RouteRecorder) -> None:
    adapter = build(slug, routes)
    instruments = await adapter.discover_instruments()

    assert instruments, f"{slug} discovery returned nothing from its own catalogue"
    for instrument in instruments:
        assert instrument.venue_slug == slug
        assert instrument.symbol_native, "every instrument needs the venue's own symbol"
        assert instrument.instrument_type in VALID_TYPES, instrument.instrument_type
        assert instrument.base_symbol_native and instrument.quote_symbol_native
        # A spot instrument must carry no derivative semantics at all: this is the
        # invariant the Instrument model enforces downstream.
        if instrument.instrument_type == InstrumentType.SPOT.value:
            assert instrument.contract_multiplier is None
            assert instrument.contract_type is None
            assert instrument.funding_interval_hours is None
        if instrument.instrument_type == InstrumentType.PERPETUAL.value:
            assert instrument.expiry is None, "a perpetual must never carry an expiry"
        if instrument.instrument_type in (
            InstrumentType.FUTURE.value,
            InstrumentType.OPTION.value,
        ):
            assert instrument.expiry is not None, "a dated instrument needs an expiry"
        if instrument.instrument_type == InstrumentType.OPTION.value:
            assert instrument.strike is not None and instrument.strike > 0
            assert instrument.option_type in ("call", "put")


@pytest.mark.parametrize("slug", ENABLED_SLUGS)
async def test_instrument_symbols_are_unique(slug: str, routes: RouteRecorder) -> None:
    """A duplicate symbol would double-count that market's volume."""
    adapter = build(slug, routes)
    instruments = await adapter.discover_instruments()
    keys = [(i.instrument_class, i.symbol_native) for i in instruments]
    assert len(keys) == len(set(keys)), f"{slug} emitted duplicate instrument keys"


@pytest.mark.parametrize("slug", ENABLED_SLUGS)
async def test_tickers_are_wellformed(slug: str, routes: RouteRecorder) -> None:
    adapter = build(slug, routes)
    tickers = await adapter.fetch_tickers()

    assert tickers, f"{slug} returned no tickers"
    for ticker in tickers:
        assert ticker.venue_slug == slug
        assert ticker.symbol_native
        # collector_receive_time is always available; the venue times may not be.
        assert ticker.timing.collector_receive_time > 0
        for value in (ticker.last_price, ticker.bid_price, ticker.ask_price):
            assert value is None or value > 0, "a price is either absent or positive"
        for value in (ticker.base_volume_24h, ticker.quote_volume_24h):
            assert value is None or value >= 0, "volume is either absent or non-negative"


@pytest.mark.parametrize("slug", ENABLED_SLUGS)
async def test_collection_uses_a_small_number_of_requests(slug: str, routes: RouteRecorder) -> None:
    """Tier A must be bulk. A per-symbol adapter would blow this ceiling immediately."""
    adapter = build(slug, routes)
    await adapter.discover_instruments()
    await adapter.fetch_tickers()
    with contextlib.suppress(UnsupportedCapability):
        await adapter.fetch_derivatives()

    ceiling = 40
    assert routes.request_count <= ceiling, (
        f"{slug} issued {routes.request_count} requests for tier A; "
        f"the configured ceiling is {ceiling}. Paths: {routes.paths()}"
    )


@pytest.mark.parametrize("slug", ENABLED_SLUGS)
async def test_declared_capabilities_match_behaviour(slug: str, routes: RouteRecorder) -> None:
    """A capability claim in configuration must be honoured by the adapter."""
    venue = venue_for(slug)
    adapter = build(slug, routes)

    instruments = await adapter.discover_instruments()
    observed = {i.instrument_type for i in instruments}

    # Every type the adapter emits must be declared. The converse is not required:
    # a fixture set need not cover every product type the venue lists.
    for instrument_type in observed:
        assert venue.capabilities.supports_instrument_type(instrument_type), (
            f"{slug} emitted {instrument_type} instruments but does not declare that capability"
        )

    if venue.capabilities.funding or venue.capabilities.open_interest:
        derivatives = await adapter.fetch_derivatives()
        assert isinstance(derivatives, list)
    else:
        with pytest.raises(UnsupportedCapability):
            await adapter.fetch_derivatives()


@pytest.mark.parametrize("slug", ENABLED_SLUGS)
async def test_derivative_observations_preserve_raw_values(
    slug: str, routes: RouteRecorder
) -> None:
    """Funding and open interest are stored exactly as published, with their units."""
    venue = venue_for(slug)
    if not (venue.capabilities.funding or venue.capabilities.open_interest):
        pytest.skip(f"{slug} exposes no derivative observations")

    adapter = build(slug, routes)
    observations = await adapter.fetch_derivatives()

    for observation in observations:
        assert observation.venue_slug == slug
        assert observation.timing.collector_receive_time > 0
        if observation.open_interest_raw is not None:
            assert observation.open_interest_raw >= 0
        if observation.funding_interval_hours is not None:
            # Real funding cycles across supported venues run from 1 to 8 hours.
            assert 0.5 <= observation.funding_interval_hours <= 24.0
        for price in (observation.mark_price, observation.index_price):
            assert price is None or price > 0


@pytest.mark.parametrize("slug", sorted(BOOK_SYMBOLS))
async def test_order_books_are_ordered_and_uncrossed(slug: str, routes: RouteRecorder) -> None:
    adapter = build(slug, routes)
    book = await adapter.fetch_order_book(BOOK_SYMBOLS[slug], depth=15)

    assert book.bids and book.asks, f"{slug} returned an empty order book"
    assert [b.price for b in book.bids] == sorted((b.price for b in book.bids), reverse=True), (
        "bids must be ordered best first"
    )
    assert [a.price for a in book.asks] == sorted(a.price for a in book.asks), (
        "asks must be ordered best first"
    )
    assert not book.is_crossed, f"{slug} returned a crossed book"
    assert all(level.size > 0 for level in (*book.bids, *book.asks))


@pytest.mark.parametrize("slug", ENABLED_SLUGS)
async def test_adapter_does_not_reach_for_unmocked_endpoints(
    slug: str, routes: RouteRecorder
) -> None:
    """Every request an adapter makes must be one the fixture set anticipated.

    An unmatched route returns 404, which surfaces an adapter quietly depending on an
    endpoint nobody recorded a payload for.
    """
    adapter = build(slug, routes)
    await adapter.discover_instruments()
    await adapter.fetch_tickers()
    with contextlib.suppress(UnsupportedCapability):
        await adapter.fetch_derivatives()

    assert routes.request_count > 0
    unmatched = [
        request.url.path
        for request in routes.requests
        if not any(matcher(request) for matcher, _ in routes.routes)
    ]
    assert not unmatched, f"{slug} requested unrecorded endpoints: {unmatched}"
