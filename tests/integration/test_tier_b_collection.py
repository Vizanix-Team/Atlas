"""Tier B end to end: a plan's symbols become order books inside a venue's result."""

from __future__ import annotations

from tests.conftest import RouteRecorder, venue_for
from tests.contract.wiring import WIRING
from vizanix_atlas.core.config import load_collection_config
from vizanix_atlas.discovery.collector import CollectionRequest, collect_venue


async def test_planned_symbols_are_collected_as_books_alongside_tier_a(
    routes: RouteRecorder,
) -> None:
    WIRING["bybit"](routes)
    request = CollectionRequest(
        venue=venue_for("bybit"),
        run_id="test",
        order_book_symbols=("BTCUSDT",),
        order_book_depth=10,
        transport=routes.transport(),
    )

    result = await collect_venue(request, load_collection_config())

    assert result.health.status == "success"
    assert result.health.order_book_count == 1
    (book,) = result.order_books
    assert (book.venue_slug, book.symbol_native, book.instrument_class) == (
        "bybit",
        "BTCUSDT",
        "spot",
    )
    assert len(book.bids) == len(book.asks) == 10
    # Tier A is still collected in the same pass.
    assert result.health.instrument_count > 0 and result.health.ticker_count > 0


async def test_without_a_plan_only_tier_a_is_collected(routes: RouteRecorder) -> None:
    WIRING["bybit"](routes)
    request = CollectionRequest(
        venue=venue_for("bybit"), run_id="test", transport=routes.transport()
    )

    result = await collect_venue(request, load_collection_config())

    assert result.health.order_book_count == 0
    assert not any(r.url.path == "/v5/market/orderbook" for r in routes.requests)


async def test_one_failing_book_costs_only_that_book(routes: RouteRecorder) -> None:
    WIRING["bybit"](routes)
    request = CollectionRequest(
        venue=venue_for("bybit"),
        run_id="test",
        # The second symbol is not in the catalogue, so the venue rejects it.
        order_book_symbols=("BTCUSDT", "NOSUCHUSDT"),
        order_book_depth=10,
        transport=routes.transport(),
    )

    result = await collect_venue(request, load_collection_config())

    assert result.health.order_book_count >= 1
    assert result.health.ticker_count > 0
