"""Asset resolution tests.

The property that matters most: a shared ticker must never produce a shared identity.
Every test here is a scenario where merging would be plausible and wrong.
"""

from __future__ import annotations

from vizanix_atlas.core.identifiers import is_unresolved
from vizanix_atlas.identity.resolver import build_resolver
from vizanix_atlas.models.enums import ResolutionState
from vizanix_atlas.models.observations import RawInstrument

USDC_ETHEREUM = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT_ETHEREUM = "0xdac17f958d2ee523a2206206994597c13d831ec7"
SOLANA_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def instrument(
    venue: str,
    base: str,
    quote: str = "USDT",
    *,
    name: str | None = None,
    address: str | None = None,
    chain: str | None = None,
) -> RawInstrument:
    """Build a minimal spot instrument for resolution tests."""
    return RawInstrument(
        venue_slug=venue,
        symbol_native=f"{base}{quote}",
        instrument_class="spot",
        instrument_type="spot",
        base_symbol_native=base,
        quote_symbol_native=quote,
        base_name=name,
        base_contract_address=address,
        base_chain_hint=chain,
    )


def test_same_ticker_on_two_venues_is_not_merged_without_evidence() -> None:
    """Two venues listing 'ZZZ' with no evidence stay separate.

    This is the core conservative behaviour. Neither venue said what ZZZ is, so Atlas
    records two venue-scoped identities rather than inventing a shared one.
    """
    resolver = build_resolver([instrument("okx", "ZZZ"), instrument("mexc", "ZZZ")])
    okx = resolver.asset_id_for("okx", "ZZZ")
    mexc = resolver.asset_id_for("mexc", "ZZZ")

    assert okx != mexc
    assert is_unresolved(okx) and is_unresolved(mexc)
    outcome = resolver.resolve("okx", "ZZZ")
    assert outcome is not None
    assert outcome.state is ResolutionState.UNRESOLVED
    assert not outcome.aggregatable


def test_matching_contract_addresses_do_merge() -> None:
    """The same contract address on two venues is the same asset.

    Evidence-based merging is the point: Atlas is conservative, not useless.
    """
    resolver = build_resolver(
        [
            instrument("mexc", "WIDGET", address=f"0x{'ab' * 20}"),
            instrument("gateio", "WIDGET", address=f"0x{'AB' * 20}"),
        ]
    )
    assert resolver.asset_id_for("mexc", "WIDGET") == resolver.asset_id_for("gateio", "WIDGET")
    outcome = resolver.resolve("mexc", "WIDGET")
    assert outcome is not None
    assert outcome.state is ResolutionState.RESOLVED
    assert outcome.aggregatable
    assert "venue_contract_address" in outcome.evidence_keys


def test_different_addresses_under_one_ticker_stay_separate() -> None:
    """Two venues listing 'ABC' with different contract addresses are two assets."""
    resolver = build_resolver(
        [
            instrument("mexc", "ABC", address=f"0x{'11' * 20}"),
            instrument("gateio", "ABC", address=f"0x{'22' * 20}"),
        ]
    )
    assert resolver.asset_id_for("mexc", "ABC") != resolver.asset_id_for("gateio", "ABC")
    # Both are confidently resolved: each address names a real, distinct asset.
    for venue in ("mexc", "gateio"):
        outcome = resolver.resolve(venue, "ABC")
        assert outcome is not None
        assert outcome.state is ResolutionState.RESOLVED
    assert len(resolver.candidates_for_symbol("ABC")) == 2


def test_never_merge_tickers_stay_venue_scoped() -> None:
    """A ticker unrelated projects have shared is never resolved from the ticker.

    UST is Tether on Bitfinex and has been TerraUSD elsewhere. Without an address or an
    override, Atlas marks it ambiguous rather than picking one.
    """
    resolver = build_resolver([instrument("okx", "UST"), instrument("gateio", "UST")])

    for venue in ("okx", "gateio"):
        outcome = resolver.resolve(venue, "UST")
        assert outcome is not None
        assert outcome.state is ResolutionState.AMBIGUOUS
        assert not outcome.aggregatable
        assert is_unresolved(outcome.asset_id)
        assert outcome.note is not None and "never_merge" in outcome.note

    assert resolver.asset_id_for("okx", "UST") != resolver.asset_id_for("gateio", "UST")
    assert "UST" in resolver.report.ambiguous_symbols


def test_a_contract_address_overrides_a_never_merge_ticker() -> None:
    """never_merge blocks ticker-based merging, not evidence-based merging."""
    resolver = build_resolver(
        [
            instrument("mexc", "ARB", address=f"0x{'cd' * 20}"),
            instrument("gateio", "ARB", address=f"0x{'cd' * 20}"),
        ]
    )
    outcome = resolver.resolve("mexc", "ARB")
    assert outcome is not None
    assert outcome.state is ResolutionState.RESOLVED
    assert resolver.asset_id_for("mexc", "ARB") == resolver.asset_id_for("gateio", "ARB")


def test_configured_override_resolves_kraken_xbt_to_bitcoin() -> None:
    """Kraken's XBT is bitcoin, asserted in configuration with a citation."""
    resolver = build_resolver(
        [
            RawInstrument(
                venue_slug="kraken",
                symbol_native="XXBTZUSD",
                instrument_class="spot",
                instrument_type="spot",
                base_symbol_native="XBT",
                quote_symbol_native="USD",
            ),
            instrument("coinbase", "BTC", "USD"),
        ]
    )
    kraken_btc = resolver.asset_id_for("kraken", "XBT")
    coinbase_btc = resolver.asset_id_for("coinbase", "BTC")

    assert kraken_btc == coinbase_btc == "asset:native:bitcoin:BTC"
    outcome = resolver.resolve("kraken", "XBT")
    assert outcome is not None
    assert outcome.state is ResolutionState.MANUAL_OVERRIDE
    assert outcome.aggregatable
    assert any("override" in key for key in outcome.evidence_keys)


def test_native_chain_assets_resolve_across_venues() -> None:
    """BTC on four venues is one asset, because bitcoin has no contract address."""
    resolver = build_resolver(
        [
            instrument("okx", "BTC", "USDT"),
            instrument("coinbase", "BTC", "USD"),
            instrument("bitstamp", "BTC", "USD"),
            instrument("deribit", "BTC", "USD"),
        ]
    )
    ids = {resolver.asset_id_for(v, "BTC") for v in ("okx", "coinbase", "bitstamp", "deribit")}
    assert ids == {"asset:native:bitcoin:BTC"}
    assert resolver.resolve("okx", "BTC").state is ResolutionState.RESOLVED  # type: ignore[union-attr]


def test_fiat_quote_currencies_resolve_by_iso_code() -> None:
    resolver = build_resolver(
        [instrument("coinbase", "BTC", "USD"), instrument("kraken", "BTC", "EUR")]
    )
    assert resolver.asset_id_for("coinbase", "USD") == "asset:fiat:usd"
    assert resolver.asset_id_for("kraken", "EUR") == "asset:fiat:eur"


def test_name_agreement_gives_probable_not_resolved() -> None:
    """Two venues agreeing on a name is suggestive, not decisive.

    A probable identity is deliberately not aggregatable: it is recorded so a reviewer
    can promote it with an override, not pooled on the strength of a name.
    """
    resolver = build_resolver(
        [
            instrument("mexc", "WGT", name="Widget Protocol"),
            instrument("gateio", "WGT", name="Widget Protocol"),
        ]
    )
    outcome = resolver.resolve("mexc", "WGT")
    assert outcome is not None
    assert outcome.state is ResolutionState.PROBABLE
    assert not outcome.aggregatable, "a name match must not license cross-venue pooling"
    assert "multi_venue_name_agreement" in outcome.evidence_keys
    # Both venues reach the same probable identity, so a reviewer can act on it.
    assert resolver.asset_id_for("mexc", "WGT") == resolver.asset_id_for("gateio", "WGT")


def test_one_venue_naming_an_asset_is_not_agreement() -> None:
    resolver = build_resolver([instrument("mexc", "WGT", name="Widget Protocol")])
    outcome = resolver.resolve("mexc", "WGT")
    assert outcome is not None
    assert outcome.state is ResolutionState.UNRESOLVED


def test_placeholder_contract_addresses_are_not_identities() -> None:
    """MEXC publishes 'xtokens' and bare chain names in its address field.

    Accepting them would map every such listing onto one fictitious token.
    """
    resolver = build_resolver(
        [
            instrument("mexc", "AAA", address="xtokens"),
            instrument("mexc", "BBB", address="BNB"),
            instrument("mexc", "CCC", address="0x0000000000000000000000000000000000000000"),
        ]
    )
    ids = {resolver.asset_id_for("mexc", s) for s in ("AAA", "BBB", "CCC")}
    assert len(ids) == 3, "placeholder addresses must not collapse distinct assets"
    assert all(is_unresolved(i) for i in ids)


def test_solana_mints_resolve_and_preserve_case() -> None:
    resolver = build_resolver([instrument("mexc", "USDCSOL", address=SOLANA_MINT)])
    asset_id = resolver.asset_id_for("mexc", "USDCSOL")
    assert asset_id == f"asset:solana:{SOLANA_MINT}"


def test_chain_hint_scopes_an_evm_token() -> None:
    """The same address on two chains is two assets."""
    resolver = build_resolver(
        [
            instrument("mexc", "TKN", address=f"0x{'ef' * 20}", chain="Ethereum"),
            instrument("gateio", "TKN", address=f"0x{'ef' * 20}", chain="Base"),
        ]
    )
    assert resolver.asset_id_for("mexc", "TKN") != resolver.asset_id_for("gateio", "TKN")
    assert resolver.asset_id_for("gateio", "TKN").startswith("asset:evm:8453:")


def test_assumed_chain_is_recorded_as_evidence() -> None:
    """An address with no chain hint defaults to Ethereum, and says so."""
    resolver = build_resolver([instrument("mexc", "TKN", address=f"0x{'11' * 20}")])
    outcome = resolver.resolve("mexc", "TKN")
    assert outcome is not None
    assert "chain_assumed_ethereum" in outcome.evidence_keys


def test_ambiguous_ticker_lookup_returns_every_candidate() -> None:
    """A lookup by an ambiguous ticker must report the ambiguity, not choose."""
    resolver = build_resolver(
        [
            instrument("mexc", "DUP", address=f"0x{'33' * 20}"),
            instrument("gateio", "DUP", address=f"0x{'44' * 20}"),
        ]
    )
    candidates = resolver.candidates_for_symbol("DUP")
    assert len(candidates) == 2
    assert candidates == tuple(sorted(candidates)), "candidates must be deterministically ordered"


def test_every_alias_records_its_evidence() -> None:
    """A mapping nobody can justify is a mapping nobody can review."""
    resolver = build_resolver(
        [
            instrument("okx", "BTC", "USDT"),
            instrument("mexc", "TKN", address=f"0x{'55' * 20}"),
            instrument("gateio", "UST"),
        ]
    )
    aliases = resolver.aliases()
    assert aliases
    for alias in aliases:
        assert alias.evidence, f"{alias.venue_slug}:{alias.venue_symbol} has no recorded evidence"
        if alias.resolution_state in (ResolutionState.AMBIGUOUS, ResolutionState.PROBABLE):
            assert alias.confidence_note, "an uncertain mapping must explain itself"


def test_report_counts_reconcile() -> None:
    resolver = build_resolver(
        [instrument("okx", "BTC", "USDT"), instrument("mexc", "ZZZ"), instrument("gateio", "UST")]
    )
    report = resolver.report
    assert report.total == len(resolver.aliases())
    assert (
        sum(
            report.summary()[k]
            for k in ("resolved", "probable", "ambiguous", "unresolved", "manual_override")
        )
        == report.total
    )


def test_resolution_is_deterministic() -> None:
    """Identical inputs must produce identical outputs, whatever the order."""
    instruments = [
        instrument("okx", "BTC", "USDT"),
        instrument("mexc", "TKN", address=f"0x{'77' * 20}"),
        instrument("gateio", "UST"),
        instrument("kucoin", "WGT", name="Widget Protocol"),
        instrument("htx", "WGT", name="Widget Protocol"),
    ]
    first = build_resolver(instruments)
    second = build_resolver(list(reversed(instruments)))

    assert [a.asset_id for a in first.assets()] == [a.asset_id for a in second.assets()]
    assert first.aliases() == second.aliases()
    assert first.report.summary() == second.report.summary()


def test_declared_relationships_are_loaded_without_merging() -> None:
    """WBTC relates to BTC; it is not BTC."""
    resolver = build_resolver([instrument("okx", "BTC", "USDT")])
    edges = resolver.relationships()
    wrapped = [e for e in edges if e.relationship.value == "wrapped_of"]
    assert wrapped, "the configured wrapped_of relationship must be loaded"
    assert wrapped[0].to_asset_id == "asset:native:bitcoin:BTC"
    assert wrapped[0].from_asset_id != "asset:native:bitcoin:BTC"
    assert wrapped[0].note, "a relationship must carry its reason"


def test_stablecoins_are_discoverable_for_the_conversion_graph() -> None:
    resolver = build_resolver([instrument("okx", "BTC", "USDT")])
    stablecoins = resolver.stablecoin_asset_ids()
    assert stablecoins, "declared stablecoins must be available to the conversion graph"
    assert all(s.startswith("asset:") for s in stablecoins)
