"""Identity evidence.

Resolution is evidence-driven. Each venue symbol accumulates whatever the venue
actually published about it, and the resolver decides from the strongest evidence
available rather than from the ticker (see ``docs/ASSET_RESOLUTION.md``).

Evidence is ranked. A contract address is decisive; a shared ticker is worth almost
nothing on its own, which is the whole point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Final

from vizanix_atlas.core.identifiers import evm_asset_id, solana_asset_id

#: Contract-address shapes Atlas can turn into an identifier.
_EVM_ADDRESS: Final = re.compile(r"\A0x[0-9a-fA-F]{40}\Z")
_SOLANA_MINT: Final = re.compile(r"\A[1-9A-HJ-NP-Za-km-z]{32,44}\Z")

#: Values venues put in a contract-address field that are not addresses. Observed on
#: MEXC, which uses several of these as placeholders. Treated as no evidence rather
#: than as a bad identity.
_ADDRESS_PLACEHOLDERS: Final = frozenset(
    {
        "",
        "-",
        "none",
        "null",
        "n/a",
        "na",
        "xtokens",
        "unknown",
        "native",
        "bnb",
        "eth",
        "btc",
        "sol",
        "trx",
        "0x",
        "0x0000000000000000000000000000000000000000",
    }
)

#: Chain hints a venue may supply, mapped to the EVM chain ID Atlas keys tokens by.
#: Only chains Atlas can name confidently are listed; anything else yields no
#: chain-scoped identity rather than a guessed one.
_EVM_CHAIN_IDS: Final = {
    "ethereum": 1,
    "eth": 1,
    "erc20": 1,
    "optimism": 10,
    "bsc": 56,
    "bnb smart chain": 56,
    "bep20": 56,
    "polygon": 137,
    "base": 8453,
    "arbitrum": 42161,
    "arbitrum one": 42161,
    "avalanche": 43114,
    "avax c-chain": 43114,
}


class EvidenceStrength(IntEnum):
    """How much a piece of evidence is worth.

    Ordered so that the resolver can simply take the maximum. The gap between
    :attr:`CONTRACT_ADDRESS` and :attr:`SYMBOL_ONLY` is deliberate and large: a ticker
    match is not evidence of identity, it is evidence of a name collision.
    """

    SYMBOL_ONLY = 1
    SYMBOL_AND_NAME = 2
    VENUE_PUBLISHED_SYMBOL_MAP = 3
    ASSET_NAME_MATCH = 4
    CHAIN_SCOPED = 5
    CONTRACT_ADDRESS = 6
    MANUAL_OVERRIDE = 7


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One fact a venue published about an asset.

    Attributes:
        key: Stable machine-readable evidence kind, recorded in the alias row so a
            mapping can be audited later.
        value: The published value, normalised where normalising is lossless.
        strength: How decisive this evidence is.
        venue_slug: Which venue published it.

    """

    key: str
    value: str
    strength: EvidenceStrength
    venue_slug: str

    def __str__(self) -> str:
        return f"{self.key}={self.value}"


@dataclass(slots=True)
class SymbolEvidence:
    """Everything Atlas knows about one venue's symbol for one asset."""

    venue_slug: str
    venue_symbol: str
    items: list[EvidenceItem] = field(default_factory=list)

    def add(self, key: str, value: str, strength: EvidenceStrength) -> None:
        """Record a piece of evidence, ignoring empty values."""
        if not value:
            return
        self.items.append(
            EvidenceItem(key=key, value=value, strength=strength, venue_slug=self.venue_slug)
        )

    @property
    def strongest(self) -> EvidenceStrength:
        """The strength of the best evidence available."""
        return max((item.strength for item in self.items), default=EvidenceStrength.SYMBOL_ONLY)

    def best(self, key: str) -> str | None:
        """Return the value recorded under ``key``, if any."""
        return next((item.value for item in self.items if item.key == key), None)

    def keys(self) -> tuple[str, ...]:
        """Return the evidence keys, sorted, for recording on an alias row."""
        return tuple(sorted({item.key for item in self.items}))


def normalise_contract_address(raw: str | None) -> tuple[str, str] | None:
    """Return ``(namespace, address)`` for a usable contract address, else ``None``.

    Rejects the placeholder values venues put in this field. A placeholder becomes "no
    evidence", which is very different from becoming a wrong identity: MEXC publishes
    ``"xtokens"`` and bare chain names such as ``"BNB"`` for some listings, and
    accepting those would map every one of them onto a single fictitious token.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    if candidate.lower() in _ADDRESS_PLACEHOLDERS:
        return None
    if _EVM_ADDRESS.match(candidate):
        return "evm", candidate.lower()
    if _SOLANA_MINT.match(candidate):
        # Solana mints are case-sensitive base58, so the case is preserved.
        return "solana", candidate
    return None


def asset_id_from_address(raw: str | None, *, chain_hint: str | None = None) -> str | None:
    """Build a canonical asset ID from a contract address, if one can be derived.

    An EVM address needs a chain to be unique, because the same address can exist on
    several chains. When no chain hint is available, Atlas assumes Ethereum mainnet
    only for addresses, and records ``chain_assumed_ethereum`` as evidence so the
    assumption is visible rather than silent.

    Returns ``None`` when the address is unusable, which leaves the caller to fall
    back to a venue-scoped identity.
    """
    normalised = normalise_contract_address(raw)
    if normalised is None:
        return None
    namespace, address = normalised
    if namespace == "solana":
        try:
            return solana_asset_id(address)
        except ValueError:
            return None
    chain_id = resolve_chain_id(chain_hint)
    try:
        return evm_asset_id(chain_id, address)
    except ValueError:
        return None


def resolve_chain_id(chain_hint: str | None) -> int:
    """Map a venue-supplied network name to an EVM chain ID.

    Defaults to Ethereum mainnet when the hint is absent or unrecognised. That default
    is why ``asset_id_from_address`` records the assumption: an unrecognised chain hint
    must not silently place a token on the wrong chain without that being auditable.
    """
    if not chain_hint:
        return 1
    return _EVM_CHAIN_IDS.get(chain_hint.strip().lower(), 1)


def is_recognised_chain(chain_hint: str | None) -> bool:
    """Return whether Atlas recognises ``chain_hint`` rather than falling back."""
    return chain_hint is not None and chain_hint.strip().lower() in _EVM_CHAIN_IDS


def normalise_asset_name(name: str | None) -> str | None:
    """Reduce an asset name to a comparable form.

    Only case and punctuation are normalised, so ``ALEPH.IM`` and ``Aleph.im`` compare
    equal while ``Aleph.im`` and ``Aleph`` do not.

    Deliberately conservative. An earlier version also stripped generic words such as
    "coin" and "token", which turned ``USD Coin`` into ``usd`` and would have let USDC
    match the fiat US dollar on name evidence. Failing to match two spellings of the
    same project costs a little confidence and falls back to weaker evidence; matching
    two different assets creates a wrong merge, which is the more damaging error.

    Names are only ever *supporting* evidence: two venues agreeing on a name raises
    confidence but never establishes identity on its own.
    """
    if not name:
        return None
    normalised = re.sub(r"[^a-z0-9]+", " ", name.strip().lower()).strip()
    return normalised or None
