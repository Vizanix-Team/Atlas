"""Quote-conversion tests.

The property under test throughout: Atlas never assumes a stablecoin equals a dollar,
and never publishes a USD figure resting on an unobserved rate.
"""

from __future__ import annotations

import pytest

from vizanix_atlas.core.config import load_collection_config
from vizanix_atlas.identity.resolver import build_resolver
from vizanix_atlas.models.enums import ConversionMethod, PriceSource
from vizanix_atlas.models.observations import (
    ObservationTiming,
    RawFxObservation,
    RawInstrument,
)
from vizanix_atlas.normalization.conversion import (
    USD_ASSET_ID,
    RateEdge,
    build_conversion_graph,
)

OBSERVED_AT = 1790530000000


def spot(venue: str, base: str, quote: str) -> RawInstrument:
    return RawInstrument(
        venue_slug=venue,
        symbol_native=f"{base}{quote}",
        instrument_class="spot",
        instrument_type="spot",
        base_symbol_native=base,
        quote_symbol_native=quote,
    )


def fx(venue: str, source: str, target: str, rate: float) -> RawFxObservation:
    return RawFxObservation(
        venue_slug=venue,
        symbol_native=f"{source}{target}",
        timing=ObservationTiming(
            collector_receive_time=OBSERVED_AT, exchange_event_time=OBSERVED_AT
        ),
        from_symbol_native=source,
        to_symbol_native=target,
        rate=rate,
        source_price=PriceSource.MID,
    )


@pytest.fixture
def quality():
    return load_collection_config().quality


@pytest.fixture
def resolver():
    return build_resolver(
        [
            spot("coinbase", "BTC", "USD"),
            spot("coinbase", "USDT", "USD"),
            spot("okx", "BTC", "USDT"),
            spot("okx", "USDC", "USDT"),
            spot("binance", "FDUSD", "USDT"),
        ]
    )


def test_usd_is_the_only_assumed_unit_rate(resolver, quality) -> None:
    """A rate of 1.0 is assumed for USD itself and for nothing else."""
    graph = build_conversion_graph([], resolver, quality)
    conversion = graph.to_usd(USD_ASSET_ID)

    assert conversion is not None
    assert conversion.rate == 1.0
    assert conversion.method is ConversionMethod.UNIT_ASSUMED
    assert conversion.usable


def test_stablecoin_parity_is_never_assumed(resolver, quality) -> None:
    """With no observed rate, USDT has no USD conversion at all.

    This is the behaviour that separates Atlas from a system that hardcodes parity: the
    absence of an observation produces a null, not a 1.0.
    """
    graph = build_conversion_graph([], resolver, quality)
    conversion = graph.to_usd(resolver.asset_id_for("okx", "USDT"))

    assert conversion is not None
    assert conversion.rate is None
    assert conversion.method is ConversionMethod.UNAVAILABLE
    assert not conversion.usable


def test_direct_observation_is_preferred_and_labelled(resolver, quality) -> None:
    graph = build_conversion_graph([fx("coinbase", "USDT", "USD", 0.99974)], resolver, quality)
    conversion = graph.to_usd(resolver.asset_id_for("okx", "USDT"))

    assert conversion is not None
    assert conversion.rate == pytest.approx(0.99974)
    assert conversion.method is ConversionMethod.DIRECT_OBSERVED
    assert conversion.source_venue_slug == "coinbase"
    assert conversion.observed_at == OBSERVED_AT
    assert len(conversion.path) == 2
    # The deviation is published so a depeg is visible rather than absorbed.
    assert conversion.deviation_from_parity_bps == pytest.approx(-2.6, abs=0.1)


def test_derived_conversions_are_labelled_as_derived(resolver, quality) -> None:
    """USDC reaches USD through USDT, and says so."""
    graph = build_conversion_graph(
        [fx("coinbase", "USDT", "USD", 0.99974), fx("okx", "USDC", "USDT", 1.000185)],
        resolver,
        quality,
    )
    conversion = graph.to_usd(resolver.asset_id_for("okx", "USDC"))

    assert conversion is not None
    assert conversion.method is ConversionMethod.DERIVED_VIA_INTERMEDIARY
    assert conversion.rate == pytest.approx(0.99974 * 1.000185)
    # The path names every hop, so a reviewer can see the chain.
    assert len(conversion.path) == 3
    assert conversion.path[0] == resolver.asset_id_for("okx", "USDC")
    assert conversion.path[-1] == USD_ASSET_ID


def test_inverse_edges_are_derived_from_observed_ones(resolver, quality) -> None:
    """A venue quoting USDT/USD has told us the USD/USDT rate too."""
    graph = build_conversion_graph([fx("coinbase", "USDT", "USD", 0.5)], resolver, quality)
    usdt = resolver.asset_id_for("okx", "USDT")

    # Both directions exist, with exactly inverse rates.
    forward = [e for e in graph.edges[usdt] if e.to_asset_id == USD_ASSET_ID]
    backward = [e for e in graph.edges[USD_ASSET_ID] if e.to_asset_id == usdt]
    assert forward and backward
    assert forward[0].rate == pytest.approx(1.0 / backward[0].rate)
    assert backward[0].inverted is True
    assert forward[0].inverted is False


def test_a_far_depeg_refuses_conversion_but_still_reports_it(resolver, quality) -> None:
    """Beyond the tolerance the market is more likely broken than depegging.

    Converting through such a rate would corrupt every downstream USD total, so the
    conversion is refused. The measured deviation is still published, so a genuine
    depeg is visible rather than hidden.
    """
    assert quality.max_stablecoin_deviation_bps == pytest.approx(500.0)
    graph = build_conversion_graph([fx("coinbase", "USDT", "USD", 0.90)], resolver, quality)
    conversion = graph.to_usd(resolver.asset_id_for("okx", "USDT"))

    assert conversion is not None
    assert conversion.rate is None
    assert not conversion.usable
    assert conversion.method is ConversionMethod.UNAVAILABLE
    assert conversion.deviation_from_parity_bps == pytest.approx(-1000.0)


def test_a_small_depeg_is_used_and_reported(resolver, quality) -> None:
    """A real, modest depeg must still convert: refusing it would lose the market."""
    graph = build_conversion_graph([fx("coinbase", "USDT", "USD", 0.985)], resolver, quality)
    conversion = graph.to_usd(resolver.asset_id_for("okx", "USDT"))

    assert conversion is not None
    assert conversion.usable
    assert conversion.rate == pytest.approx(0.985)
    assert conversion.deviation_from_parity_bps == pytest.approx(-150.0)


def test_unreachable_currencies_yield_no_usd_figure(resolver, quality) -> None:
    graph = build_conversion_graph([fx("coinbase", "USDT", "USD", 0.9997)], resolver, quality)
    conversion = graph.to_usd("asset:unresolved:somevenue:WEIRD")

    assert conversion is not None
    assert conversion.rate is None
    assert not conversion.usable


def test_long_chains_are_refused(quality) -> None:
    """Each hop widens the sampling window and compounds error."""
    resolver = build_resolver(
        [
            spot("v", "A", "B"),
            spot("v", "B", "C"),
            spot("v", "C", "D"),
            spot("v", "D", "USD"),
        ]
    )
    graph = build_conversion_graph(
        [
            fx("v", "A", "B", 1.0),
            fx("v", "B", "C", 1.0),
            fx("v", "C", "D", 1.0),
            fx("v", "D", "USD", 1.0),
        ],
        resolver,
        quality,
    )
    # A is four hops from USD, beyond the three-hop ceiling.
    assert graph.to_usd(resolver.asset_id_for("v", "A")).rate is None  # type: ignore[union-attr]
    # D is one hop away and resolves.
    assert graph.to_usd(resolver.asset_id_for("v", "D")).rate == pytest.approx(1.0)  # type: ignore[union-attr]


def test_conversion_is_deterministic_regardless_of_observation_order(resolver, quality) -> None:
    """Two runs over the same observations must publish the same rate and path."""
    observations = [
        fx("coinbase", "USDT", "USD", 0.99974),
        fx("okx", "USDC", "USDT", 1.000185),
        fx("binance", "FDUSD", "USDT", 0.99945),
    ]
    first = build_conversion_graph(observations, resolver, quality)
    second = build_conversion_graph(list(reversed(observations)), resolver, quality)

    for code in ("USDT", "USDC"):
        asset_id = resolver.asset_id_for("okx", code)
        a = first.to_usd(asset_id)
        b = second.to_usd(asset_id)
        assert a is not None and b is not None
        assert a.rate == b.rate
        assert a.path == b.path
        assert a.method is b.method


def test_edge_cost_prefers_a_directly_quoted_rate(quality) -> None:
    """When both a quoted and an inverted edge reach USD, the quoted one wins."""
    direct = RateEdge(
        from_asset_id="asset:x",
        to_asset_id=USD_ASSET_ID,
        rate=1.0,
        venue_slug="a",
        observed_at=OBSERVED_AT,
        inverted=False,
    )
    inverted = RateEdge(
        from_asset_id="asset:x",
        to_asset_id=USD_ASSET_ID,
        rate=1.0,
        venue_slug="a",
        observed_at=OBSERVED_AT,
        inverted=True,
    )
    assert direct.cost < inverted.cost
