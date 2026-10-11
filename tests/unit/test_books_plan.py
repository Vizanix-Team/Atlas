"""Order-book plan tests (tier B selection and its transport between runs)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from vizanix_atlas.core.config import load_collection_config
from vizanix_atlas.discovery.books_plan import (
    BooksPlan,
    load_books_plan,
    plan_order_books,
)
from vizanix_atlas.models.enums import InstrumentType
from vizanix_atlas.util.sample import build_sample_generation


def _open_config():
    """Selection budgets that do not filter the tiny sample out."""
    config = load_collection_config()
    selection = config.tiers.b_liquidity.selection.model_copy(
        update={"min_reported_volume_24h_usd": 0.0, "min_venue_count": 1}
    )
    b_liquidity = config.tiers.b_liquidity.model_copy(update={"selection": selection})
    return config.model_copy(
        update={"tiers": config.tiers.model_copy(update={"b_liquidity": b_liquidity})}
    )


def _venues(generation) -> set[str]:
    return {i.venue_slug for i in generation.instruments}


def test_the_plan_names_native_symbols_of_real_instruments() -> None:
    generation = build_sample_generation()
    plan = plan_order_books(
        generation, config=_open_config(), venues_with_books=_venues(generation)
    )

    assert plan.generation_id == generation.generation_id
    assert plan.total > 0
    known = {(i.venue_slug, i.symbol_native) for i in generation.instruments}
    for venue, symbols in plan.venues.items():
        assert symbols == tuple(sorted(symbols)), "symbols are sorted so the plan is deterministic"
        for symbol in symbols:
            assert (venue, symbol) in known


def test_the_plan_is_deterministic() -> None:
    generation = build_sample_generation()
    config = _open_config()
    venues = _venues(generation)
    first = plan_order_books(generation, config=config, venues_with_books=venues)
    second = plan_order_books(generation, config=config, venues_with_books=venues)
    assert first.venues == second.venues


def test_venues_without_a_book_endpoint_are_never_planned() -> None:
    generation = build_sample_generation()
    plan = plan_order_books(generation, config=_open_config(), venues_with_books=set())
    assert plan.venues == {}


def test_only_the_listed_venues_are_planned() -> None:
    generation = build_sample_generation()
    venue = sorted(_venues(generation))[0]
    plan = plan_order_books(generation, config=_open_config(), venues_with_books={venue})
    assert set(plan.venues) <= {venue}


def test_spot_is_preferred_over_a_derivative_market_for_the_same_asset() -> None:
    generation = build_sample_generation()
    plan = plan_order_books(
        generation, config=_open_config(), venues_with_books=_venues(generation)
    )
    by_key = {(i.venue_slug, i.symbol_native): i for i in generation.instruments}
    spot_assets = {
        (i.venue_slug, i.base_asset_id)
        for i in generation.instruments
        if i.instrument_type is InstrumentType.SPOT and i.active
    }
    for venue, symbols in plan.venues.items():
        for symbol in symbols:
            instrument = by_key[(venue, symbol)]
            if (venue, instrument.base_asset_id) in spot_assets:
                assert instrument.instrument_type is InstrumentType.SPOT


def test_the_per_venue_budget_is_respected() -> None:
    generation = build_sample_generation()
    config = _open_config()
    selection = config.tiers.b_liquidity.selection.model_copy(
        update={"max_instruments_per_venue": 1}
    )
    config = config.model_copy(
        update={
            "tiers": config.tiers.model_copy(
                update={
                    "b_liquidity": config.tiers.b_liquidity.model_copy(
                        update={"selection": selection}
                    )
                }
            )
        }
    )
    plan = plan_order_books(generation, config=config, venues_with_books=_venues(generation))
    assert all(len(symbols) <= 1 for symbols in plan.venues.values())


# ---------------------------------------------------------------------------- loading


def _plan() -> BooksPlan:
    return BooksPlan(generation_id="g1", generated_at=1, depth=100, venues={"okx": ("BTC-USDT",)})


def test_a_plan_loads_from_a_local_file(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text(_plan().model_dump_json())
    loaded = load_books_plan(str(path))
    assert loaded is not None and loaded.symbols_for("okx") == ("BTC-USDT",)
    assert loaded.symbols_for("kraken") == ()


def test_no_location_means_no_plan() -> None:
    assert load_books_plan(None) is None
    assert load_books_plan("") is None


def test_a_missing_or_malformed_plan_is_no_plan_not_an_error(tmp_path: Path) -> None:
    assert load_books_plan(str(tmp_path / "absent.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_books_plan(str(bad)) is None
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"hello": "world"}))
    assert load_books_plan(str(wrong)) is None


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_a_plan_loads_over_https_following_the_release_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"Location": "https://objects.example/plan"})
        return httpx.Response(200, content=_plan().model_dump_json().encode())

    with _client(handler) as client:
        loaded = load_books_plan(
            "https://github.com/o/r/releases/download/data-latest/books-plan.json", client=client
        )
    assert loaded is not None and loaded.total == 1


def test_a_404_plan_is_no_plan() -> None:
    with _client(lambda request: httpx.Response(404)) as client:
        assert load_books_plan("https://example.test/plan.json", client=client) is None


def test_a_server_error_is_no_plan() -> None:
    with _client(lambda request: httpx.Response(500)) as client:
        assert load_books_plan("https://example.test/plan.json", client=client) is None


def test_a_plain_http_plan_is_refused() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=_plan().model_dump_json().encode())

    with _client(handler) as client:
        assert load_books_plan("http://example.test/plan.json", client=client) is None
    assert not called, "an http URL must not even be requested"
