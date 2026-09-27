"""Conservative asset resolution.

The rule Atlas is built around: **a shared ticker is not a shared asset.** ``UST`` is
Tether on Bitfinex and has been TerraUSD elsewhere; ``ARB``, ``MASK``, ``ONE``,
``PAY`` and ``MAGIC`` have each been used by unrelated projects. Merging on the ticker
would silently combine them into one price, one volume and one liquidity surface.

So the resolver works from evidence, in this order:

1. A manual override in ``config/asset_overrides.yaml``, which carries its own
   reason, source, date and author.
2. A contract address the venue published. Decisive, because it names the asset.
3. An ISO 4217 code, for a government-issued currency.
4. A ticker claimed by exactly one canonical asset declared in configuration.
5. A curated native-chain ticker, for a coin that has no contract address.
6. An asset name that at least two venues agree on, *plus* a matching ticker.

When none of those settles it, the symbol gets a **venue-scoped identity**
(``asset:unresolved:<venue>:<SYMBOL>``). That asset is never aggregated with any other
venue's. Its observations are still recorded and still queryable; they are simply not
pooled. An isolated record is recoverable later; a wrong merge silently corrupts every
metric derived from it and is not.

See ``docs/ASSET_RESOLUTION.md`` for the worked examples.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

from vizanix_atlas.core.config import OverrideConfig, load_override_config
from vizanix_atlas.core.identifiers import (
    fiat_asset_id,
    native_asset_id,
    unresolved_asset_id,
)
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.identity.evidence import (
    EvidenceStrength,
    SymbolEvidence,
    asset_id_from_address,
    is_recognised_chain,
    normalise_asset_name,
    normalise_contract_address,
)
from vizanix_atlas.models.asset import AssetAlias, AssetRelationship, CanonicalAsset
from vizanix_atlas.models.enums import RelationshipType, ResolutionState
from vizanix_atlas.models.observations import RawInstrument

_log = get_logger(__name__)

#: Government-issued currencies venues quote against. Recognised so that a fiat quote
#: currency is never treated as a token needing a contract address.
_FIAT_CODES: Final = frozenset(
    {
        "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "SGD", "HKD",
        "KRW", "TRY", "BRL", "MXN", "ZAR", "PLN", "RON", "HUF", "CZK", "SEK",
        "NOK", "DKK", "AED", "ARS", "INR", "IDR", "THB", "VND", "NGN", "UAH",
    }
)

#: Assets whose identity is their own chain. Keyed by ticker because a native coin has
#: no contract address to resolve against. Only chains Atlas can name with confidence
#: appear here; everything else resolves through a contract address or stays
#: venue-scoped.
_NATIVE_CHAINS: Final = {
    "BTC": "bitcoin",
    "XBT": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "ADA": "cardano",
    "XRP": "xrp-ledger",
    "DOGE": "dogecoin",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
    "DOT": "polkadot",
    "AVAX": "avalanche",
    "TRX": "tron",
    "ATOM": "cosmos",
    "NEAR": "near",
    "ALGO": "algorand",
    "XLM": "stellar",
    "XMR": "monero",
    "ETC": "ethereum-classic",
    "FIL": "filecoin",
    "HBAR": "hedera",
    "ICP": "internet-computer",
    "TON": "ton",
    "SUI": "sui",
    "APT": "aptos",
    "SEI": "sei",
    "TIA": "celestia",
    "INJ": "injective",
    "KAS": "kaspa",
    "ZEC": "zcash",
    "DASH": "dash",
    "XTZ": "tezos",
    "EOS": "eos",
    "VET": "vechain",
    "BNB": "bnb-chain",
    "S": "sonic",
    "HYPE": "hyperliquid",
}

#: How many venues must agree on a normalised asset name before the name counts as
#: corroborating evidence. Two independent venues writing the same project name is a
#: meaningful signal; one venue's name is just that venue's spelling.
_NAME_AGREEMENT_THRESHOLD: Final = 2


@dataclass(slots=True)
class ResolutionOutcome:
    """The result of resolving one venue symbol."""

    asset_id: str
    state: ResolutionState
    evidence_keys: tuple[str, ...]
    note: str | None = None

    @property
    def aggregatable(self) -> bool:
        """Whether observations on this identity may be pooled across venues."""
        return self.state in (ResolutionState.RESOLVED, ResolutionState.MANUAL_OVERRIDE)


@dataclass(slots=True)
class ResolutionReport:
    """Counts and examples from one resolution pass, for the quality tables."""

    resolved: int = 0
    probable: int = 0
    ambiguous: int = 0
    unresolved: int = 0
    manual_override: int = 0
    ambiguous_symbols: set[str] = field(default_factory=set)
    unresolved_symbols: set[str] = field(default_factory=set)

    def record(self, outcome: ResolutionOutcome, venue_symbol: str) -> None:
        """Count one outcome."""
        match outcome.state:
            case ResolutionState.RESOLVED:
                self.resolved += 1
            case ResolutionState.PROBABLE:
                self.probable += 1
            case ResolutionState.AMBIGUOUS:
                self.ambiguous += 1
                self.ambiguous_symbols.add(venue_symbol.upper())
            case ResolutionState.UNRESOLVED:
                self.unresolved += 1
                self.unresolved_symbols.add(venue_symbol.upper())
            case ResolutionState.MANUAL_OVERRIDE:
                self.manual_override += 1

    @property
    def total(self) -> int:
        """Every symbol considered."""
        return (
            self.resolved
            + self.probable
            + self.ambiguous
            + self.unresolved
            + self.manual_override
        )

    def summary(self) -> dict[str, int]:
        """Return the counts for logging and for the quality tables."""
        return {
            "resolved": self.resolved,
            "probable": self.probable,
            "ambiguous": self.ambiguous,
            "unresolved": self.unresolved,
            "manual_override": self.manual_override,
            "total": self.total,
        }


class AssetResolver:
    """Resolves venue symbols to canonical asset identities.

    Built once per generation from every discovered instrument, because resolution is a
    global decision: whether a ticker is ambiguous depends on what every other venue
    calls the same thing. Resolving symbol by symbol as they arrive would make the
    answer depend on arrival order.
    """

    def __init__(self, overrides: OverrideConfig | None = None) -> None:
        self.overrides = overrides or load_override_config()
        self._never_merge = self.overrides.never_merge_symbols()
        self._assets: dict[str, CanonicalAsset] = {}
        self._aliases: list[AssetAlias] = []
        self._relationships: list[AssetRelationship] = []
        self._evidence: dict[tuple[str, str], SymbolEvidence] = {}
        self._outcomes: dict[tuple[str, str], ResolutionOutcome] = {}
        self._symbol_to_asset_ids: dict[str, set[str]] = defaultdict(set)
        #: Tickers claimed by exactly one asset declared in configuration. A declaration
        #: is a reviewed assertion carrying a reason, source, date and author, which is
        #: what makes it usable evidence rather than a guess.
        self._declared_by_symbol: dict[str, str] = {}
        self.report = ResolutionReport()
        self._seed_declared_assets()

    # ------------------------------------------------------------------ seeding

    def _seed_declared_assets(self) -> None:
        """Load the canonical assets and relationships declared in configuration."""
        for declared in self.overrides.canonical_assets:
            self._assets[declared.asset_id] = CanonicalAsset(
                asset_id=declared.asset_id,
                symbol=declared.symbol,
                name=declared.name,
                chain_slug=declared.chain_slug,
                chain_id=declared.chain_id,
                contract_address=declared.contract_address,
                resolution_state=ResolutionState.RESOLVED,
                is_stablecoin=declared.is_stablecoin,
                is_fiat=declared.is_fiat,
                tracks_asset_id=declared.tracks_asset_id,
            )
            self._symbol_to_asset_ids[declared.symbol.upper()].add(declared.asset_id)

        # A ticker claimed by more than one declared asset is not usable as evidence,
        # so it is removed rather than resolved arbitrarily.
        claims: dict[str, list[str]] = defaultdict(list)
        for declared in self.overrides.canonical_assets:
            claims[declared.symbol.upper()].append(declared.asset_id)
        self._declared_by_symbol = {
            symbol: asset_ids[0] for symbol, asset_ids in claims.items() if len(asset_ids) == 1
        }

        for edge in self.overrides.relationships:
            try:
                relationship = RelationshipType(edge.relationship)
            except ValueError:
                _log.warning(
                    "skipping relationship with unknown type",
                    extra={"relationship": edge.relationship},
                )
                continue
            self._relationships.append(
                AssetRelationship(
                    from_asset_id=edge.from_asset_id,
                    to_asset_id=edge.to_asset_id,
                    relationship=relationship,
                    source=f"override:{edge.provenance.source}",
                    note=edge.provenance.reason,
                )
            )

    # ------------------------------------------------------------ evidence pass

    def ingest(self, instruments: Iterable[RawInstrument]) -> None:
        """Collect evidence from every discovered instrument.

        Both sides of every pair contribute: a quote currency is an asset too, and
        resolving it is what makes quote conversion possible.
        """
        for instrument in instruments:
            self._collect(
                venue_slug=instrument.venue_slug,
                symbol=instrument.base_symbol_native,
                name=instrument.base_name,
                contract_address=instrument.base_contract_address,
                chain_hint=instrument.base_chain_hint,
            )
            self._collect(
                venue_slug=instrument.venue_slug,
                symbol=instrument.quote_symbol_native,
                name=None,
                contract_address=None,
                chain_hint=None,
            )
            if instrument.settlement_symbol_native:
                self._collect(
                    venue_slug=instrument.venue_slug,
                    symbol=instrument.settlement_symbol_native,
                    name=None,
                    contract_address=None,
                    chain_hint=None,
                )

    def _collect(
        self,
        *,
        venue_slug: str,
        symbol: str,
        name: str | None,
        contract_address: str | None,
        chain_hint: str | None,
    ) -> None:
        """Record what one venue published about one symbol."""
        cleaned = symbol.strip()
        if not cleaned:
            return
        key = (venue_slug, cleaned.upper())
        evidence = self._evidence.get(key)
        if evidence is None:
            evidence = SymbolEvidence(venue_slug=venue_slug, venue_symbol=cleaned)
            evidence.add("venue_symbol", cleaned.upper(), EvidenceStrength.SYMBOL_ONLY)
            self._evidence[key] = evidence

        if normalise_contract_address(contract_address) is not None:
            evidence.add(
                "venue_contract_address",
                str(contract_address).strip(),
                EvidenceStrength.CONTRACT_ADDRESS,
            )
        normalised_name = normalise_asset_name(name)
        if normalised_name:
            evidence.add("venue_asset_name", normalised_name, EvidenceStrength.SYMBOL_AND_NAME)
        if is_recognised_chain(chain_hint):
            evidence.add(
                "venue_chain", str(chain_hint).strip().lower(), EvidenceStrength.CHAIN_SCOPED
            )

    # ------------------------------------------------------------ resolve pass

    def resolve_all(self) -> None:
        """Resolve every collected symbol.

        Runs in two phases because name agreement is a cross-venue fact: the count of
        venues per normalised name has to be known before any name can be used as
        evidence.
        """
        name_venues: dict[str, set[str]] = defaultdict(set)
        for (venue_slug, _), evidence in self._evidence.items():
            asset_name = evidence.best("venue_asset_name")
            if asset_name:
                name_venues[asset_name].add(venue_slug)

        corroborated_names = {
            name for name, venues in name_venues.items()
            if len(venues) >= _NAME_AGREEMENT_THRESHOLD
        }

        for key in sorted(self._evidence):
            outcome = self._resolve_one(self._evidence[key], corroborated_names)
            self._outcomes[key] = outcome
            self.report.record(outcome, self._evidence[key].venue_symbol)
            self._register_alias(self._evidence[key], outcome)

        _log.info("asset resolution complete", extra=self.report.summary())

    def _resolve_one(
        self, evidence: SymbolEvidence, corroborated_names: set[str]
    ) -> ResolutionOutcome:
        """Decide one symbol's identity."""
        upper = evidence.venue_symbol.upper()

        # 1. A manual override wins outright, and carries its own justification.
        override = self.overrides.alias_for(evidence.venue_slug, evidence.venue_symbol)
        if override is not None:
            self._ensure_asset(
                override, symbol=upper, name=None, state=ResolutionState.MANUAL_OVERRIDE
            )
            return ResolutionOutcome(
                asset_id=override,
                state=ResolutionState.MANUAL_OVERRIDE,
                evidence_keys=(*evidence.keys(), "override:config/asset_overrides.yaml"),
                note="asserted in configuration with a recorded reason and source",
            )

        # 2. A contract address names the asset, so it is decisive.
        address = evidence.best("venue_contract_address")
        if address is not None:
            asset_id = asset_id_from_address(
                address, chain_hint=evidence.best("venue_chain")
            )
            if asset_id is not None:
                keys = list(evidence.keys())
                if not evidence.best("venue_chain"):
                    # The chain was assumed, so say so on the alias row.
                    keys.append("chain_assumed_ethereum")
                self._ensure_asset(
                    asset_id,
                    symbol=upper,
                    name=evidence.best("venue_asset_name"),
                    state=ResolutionState.RESOLVED,
                )
                return ResolutionOutcome(
                    asset_id=asset_id,
                    state=ResolutionState.RESOLVED,
                    evidence_keys=tuple(keys),
                )

        # 3. A government-issued currency is identified by its ISO code.
        if upper in _FIAT_CODES:
            asset_id = fiat_asset_id(upper)
            self._ensure_asset(
                asset_id, symbol=upper, name=None, state=ResolutionState.RESOLVED, is_fiat=True
            )
            return ResolutionOutcome(
                asset_id=asset_id,
                state=ResolutionState.RESOLVED,
                evidence_keys=(*evidence.keys(), "iso_4217_currency_code"),
            )

        # A never-merge ticker is never resolved from the ticker alone, whatever else
        # is true. Only a contract address or an override, both handled above, can
        # settle it, so reaching here means it stays venue-scoped.
        if upper in self._never_merge:
            return self._venue_scoped(
                evidence,
                state=ResolutionState.AMBIGUOUS,
                note=(
                    "ticker is listed in never_merge because unrelated assets have used "
                    "it; no contract address or override was available to settle it"
                ),
            )

        # 4. A ticker claimed by exactly one canonical asset declared in configuration.
        # This is what lets every venue's "USDT" land on one identity, which in turn is
        # what makes USD normalisation possible at all. It is evidence rather than a
        # guess because the declaration carries a reviewed reason and source, and it is
        # reached only after a contract address has had its chance: a venue publishing
        # an address for a bridged USDT still gets its own identity, because a bridged
        # representation is related to the original rather than identical to it.
        declared = self._declared_by_symbol.get(upper)
        if declared is not None:
            return ResolutionOutcome(
                asset_id=declared,
                state=ResolutionState.RESOLVED,
                evidence_keys=(*evidence.keys(), "declared_canonical_asset"),
            )

        # 5. A chain's own native coin has no contract address, so its ticker is the
        # only handle it has. Restricted to a curated list of chains, so an arbitrary
        # three-letter ticker cannot claim to be a native coin.
        chain = _NATIVE_CHAINS.get(upper)
        if chain is not None:
            asset_id = native_asset_id(chain, upper)
            self._ensure_asset(
                asset_id,
                symbol=upper,
                name=evidence.best("venue_asset_name"),
                state=ResolutionState.RESOLVED,
                chain_slug=chain,
            )
            return ResolutionOutcome(
                asset_id=asset_id,
                state=ResolutionState.RESOLVED,
                evidence_keys=(*evidence.keys(), "curated_native_chain"),
            )

        # 6. A name that several venues independently agree on, together with the
        # ticker, is enough for a probable identity but not a confident one.
        asset_name = evidence.best("venue_asset_name")
        if asset_name and asset_name in corroborated_names:
            asset_id = f"asset:named:{_slugify(asset_name)}:{upper}"
            self._ensure_asset(
                asset_id, symbol=upper, name=asset_name, state=ResolutionState.PROBABLE
            )
            return ResolutionOutcome(
                asset_id=asset_id,
                state=ResolutionState.PROBABLE,
                evidence_keys=(*evidence.keys(), "multi_venue_name_agreement"),
                note=(
                    "identity rests on several venues agreeing on the asset name; "
                    "not aggregated across venues without stronger evidence"
                ),
            )

        # 7. Nothing settled it. A venue-scoped identity keeps the observation without
        # risking a wrong merge.
        return self._venue_scoped(
            evidence,
            state=ResolutionState.UNRESOLVED,
            note="no contract address, curated chain, override or corroborated name",
        )

    def _venue_scoped(
        self, evidence: SymbolEvidence, *, state: ResolutionState, note: str
    ) -> ResolutionOutcome:
        """Create a venue-scoped identity that is never pooled across venues."""
        asset_id = unresolved_asset_id(evidence.venue_slug, evidence.venue_symbol)
        self._ensure_asset(
            asset_id,
            symbol=evidence.venue_symbol.upper(),
            name=evidence.best("venue_asset_name"),
            state=state,
        )
        return ResolutionOutcome(
            asset_id=asset_id,
            state=state,
            evidence_keys=evidence.keys(),
            note=note,
        )

    def _ensure_asset(
        self,
        asset_id: str,
        *,
        symbol: str,
        name: str | None,
        state: ResolutionState,
        chain_slug: str | None = None,
        is_fiat: bool = False,
    ) -> None:
        """Create or update the canonical asset record for ``asset_id``."""
        existing = self._assets.get(asset_id)
        if existing is None:
            self._assets[asset_id] = CanonicalAsset(
                asset_id=asset_id,
                symbol=symbol,
                name=name,
                chain_slug=chain_slug,
                resolution_state=state,
                is_fiat=is_fiat,
            )
        elif existing.name is None and name is not None:
            # Fill in a name a later venue supplied, without changing the identity.
            self._assets[asset_id] = existing.model_copy(update={"name": name})
        self._symbol_to_asset_ids[symbol].add(asset_id)

    def _register_alias(self, evidence: SymbolEvidence, outcome: ResolutionOutcome) -> None:
        """Record the venue symbol to canonical asset mapping with its evidence."""
        self._aliases.append(
            AssetAlias(
                asset_id=outcome.asset_id,
                venue_slug=evidence.venue_slug,
                venue_symbol=evidence.venue_symbol,
                resolution_state=outcome.state,
                evidence=outcome.evidence_keys,
                confidence_note=outcome.note,
            )
        )

    # ------------------------------------------------------------------ lookups

    def resolve(self, venue_slug: str, venue_symbol: str) -> ResolutionOutcome | None:
        """Return the outcome for one venue symbol, or ``None`` if never collected."""
        return self._outcomes.get((venue_slug, venue_symbol.strip().upper()))

    def asset_id_for(self, venue_slug: str, venue_symbol: str) -> str:
        """Return the canonical asset ID for a venue symbol.

        Falls back to a venue-scoped identity for a symbol that was never ingested,
        rather than raising, so that a ticker for an instrument discovery missed is
        still recorded instead of discarding the observation.
        """
        outcome = self.resolve(venue_slug, venue_symbol)
        if outcome is not None:
            return outcome.asset_id
        return unresolved_asset_id(venue_slug, venue_symbol)

    def assets(self) -> tuple[CanonicalAsset, ...]:
        """Return every canonical asset, ordered by identifier."""
        counts: dict[str, set[str]] = defaultdict(set)
        for alias in self._aliases:
            counts[alias.asset_id].add(alias.venue_slug)
        return tuple(
            asset.model_copy(update={"venue_count": len(counts.get(asset.asset_id, ()))})
            for asset in sorted(self._assets.values(), key=lambda a: a.asset_id)
        )

    def aliases(self) -> tuple[AssetAlias, ...]:
        """Return every alias, ordered so output is deterministic."""
        return tuple(
            sorted(self._aliases, key=lambda a: (a.venue_slug, a.venue_symbol, a.asset_id))
        )

    def relationships(self) -> tuple[AssetRelationship, ...]:
        """Return every asset-graph edge, ordered so output is deterministic."""
        return tuple(
            sorted(
                self._relationships,
                key=lambda r: (r.from_asset_id, r.to_asset_id, r.relationship.value),
            )
        )

    def candidates_for_symbol(self, symbol: str) -> tuple[str, ...]:
        """Return every canonical asset that a ticker maps to, sorted.

        More than one means the ticker is ambiguous, and a lookup by that ticker must
        report the ambiguity rather than choosing.
        """
        return tuple(sorted(self._symbol_to_asset_ids.get(symbol.strip().upper(), ())))

    def stablecoin_asset_ids(self) -> tuple[str, ...]:
        """Return the asset IDs declared as stablecoins, for the conversion graph."""
        return tuple(
            sorted(asset.asset_id for asset in self._assets.values() if asset.is_stablecoin)
        )


def _slugify(text: str) -> str:
    """Reduce a normalised name to an identifier segment."""
    return "-".join(text.split())[:48] or "unknown"


def build_resolver(
    instruments: Sequence[RawInstrument], *, overrides: OverrideConfig | None = None
) -> AssetResolver:
    """Build and run a resolver over every discovered instrument."""
    resolver = AssetResolver(overrides=overrides)
    resolver.ingest(instruments)
    resolver.resolve_all()
    return resolver
