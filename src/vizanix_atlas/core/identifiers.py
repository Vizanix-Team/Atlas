"""Canonical identifier scheme.

Atlas never uses a bare ticker as an identity (see ``docs/ASSET_RESOLUTION.md``).
Every entity carries a structured, deterministic, human-readable identifier so
that a record found in a Parquet file six months later can still be traced back to
what it described.

Grammar, as published in ``docs/SPECIFICATION.md``::

    venue:<venue_id>
    asset:native:<chain_slug>:<symbol>
    asset:evm:<chain_id>:<contract_address>
    asset:solana:<mint>
    asset:fiat:<iso_code>
    asset:unresolved:<venue_id>:<venue_symbol>
    instrument:<venue_id>:<instrument_class>:<venue_symbol>

Identifiers are lowercase except where case is load-bearing: a Solana mint is
case-sensitive base58, and venue-native symbols are preserved verbatim in the
final segment so that an operator can paste one back into the venue's own API.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

SCHEMA_SEPARATOR: Final = ":"

# Segment charset is deliberately narrow: an identifier ends up in file names,
# URLs, SQL identifiers and shard keys, and a permissive charset would create
# escaping problems in all four.
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._\-]+")
_EVM_ADDRESS = re.compile(r"\A0x[0-9a-fA-F]{40}\Z")
_SOLANA_MINT = re.compile(r"\A[1-9A-HJ-NP-Za-km-z]{32,44}\Z")


def sanitise_segment(raw: str) -> str:
    """Reduce a raw venue string to the identifier segment charset.

    Unsupported characters collapse to ``-`` rather than being dropped, so that
    two different venue symbols cannot silently collapse onto the same segment.
    """
    cleaned = _SAFE_SEGMENT.sub("-", raw.strip())
    return cleaned.strip("-") or "unknown"


def venue_id(venue: str) -> str:
    """Build a venue identifier, for example ``venue:okx``."""
    return f"venue{SCHEMA_SEPARATOR}{sanitise_segment(venue).lower()}"


def native_asset_id(chain_slug: str, symbol: str) -> str:
    """Build an identifier for a chain's own native asset.

    ``asset:native:bitcoin:BTC`` is the native coin of the Bitcoin chain. The
    symbol is retained for readability; the chain slug is what makes it unique.
    """
    return (
        f"asset{SCHEMA_SEPARATOR}native{SCHEMA_SEPARATOR}"
        f"{sanitise_segment(chain_slug).lower()}{SCHEMA_SEPARATOR}{sanitise_segment(symbol).upper()}"
    )


def evm_asset_id(chain_id: int, contract_address: str) -> str:
    """Build an identifier for an EVM token from its chain ID and contract address.

    Raises:
        ValueError: If ``contract_address`` is not a 20-byte hex address. Atlas
            does not store a malformed address as though it were an identity.
    """
    if not _EVM_ADDRESS.match(contract_address):
        raise ValueError(f"not an EVM contract address: {contract_address!r}")
    return (
        f"asset{SCHEMA_SEPARATOR}evm{SCHEMA_SEPARATOR}{chain_id}"
        f"{SCHEMA_SEPARATOR}{contract_address.lower()}"
    )


def solana_asset_id(mint: str) -> str:
    """Build an identifier for a Solana token from its mint address.

    The mint is preserved with its original case because base58 is case-sensitive.

    Raises:
        ValueError: If ``mint`` is not plausible base58 of mint length.
    """
    if not _SOLANA_MINT.match(mint):
        raise ValueError(f"not a Solana mint address: {mint!r}")
    return f"asset{SCHEMA_SEPARATOR}solana{SCHEMA_SEPARATOR}{mint}"


def fiat_asset_id(iso_code: str) -> str:
    """Build an identifier for a government-issued currency, for example ``asset:fiat:usd``."""
    return f"asset{SCHEMA_SEPARATOR}fiat{SCHEMA_SEPARATOR}{sanitise_segment(iso_code).lower()}"


def unresolved_asset_id(venue: str, venue_symbol: str) -> str:
    """Build a venue-scoped identity for an asset that could not be resolved.

    This is the deliberate fallback described in ``docs/ASSET_RESOLUTION.md``. An
    unresolved identity is never aggregated with any other venue's asset, which is
    the whole point: an isolated record is recoverable, a wrong merge is not.
    """
    return (
        f"asset{SCHEMA_SEPARATOR}unresolved{SCHEMA_SEPARATOR}"
        f"{sanitise_segment(venue).lower()}{SCHEMA_SEPARATOR}{sanitise_segment(venue_symbol).upper()}"
    )


def instrument_id(venue: str, instrument_class: str, venue_symbol: str) -> str:
    """Build an instrument identifier.

    ``instrument_class`` distinguishes markets a venue exposes under one symbol
    namespace, for example OKX's ``spot`` and ``linear-perp``. The venue symbol is
    preserved verbatim so it can be pasted back into the venue's API.
    """
    return (
        f"instrument{SCHEMA_SEPARATOR}{sanitise_segment(venue).lower()}"
        f"{SCHEMA_SEPARATOR}{sanitise_segment(instrument_class).lower()}"
        f"{SCHEMA_SEPARATOR}{sanitise_segment(venue_symbol)}"
    )


def is_unresolved(asset_id: str) -> bool:
    """Return whether an asset identifier is a venue-scoped unresolved identity."""
    return asset_id.startswith(f"asset{SCHEMA_SEPARATOR}unresolved{SCHEMA_SEPARATOR}")


def asset_namespace(asset_id: str) -> str:
    """Return the namespace segment of an asset identifier, such as ``evm``.

    Raises:
        ValueError: If ``asset_id`` is not an asset identifier.
    """
    parts = asset_id.split(SCHEMA_SEPARATOR)
    if len(parts) < 3 or parts[0] != "asset":
        raise ValueError(f"not an asset identifier: {asset_id!r}")
    return parts[1]


def shard_for(asset_id: str, shard_count: int) -> int:
    """Map an asset identifier to a shard index.

    Uses the leading 8 bytes of ``sha256(asset_id)`` so that the mapping is stable
    across releases, independent of Python's per-process hash seed, and identical
    in every client implementation. A client that wants one asset therefore
    downloads one shard (see ``docs/STORAGE.md``).

    Raises:
        ValueError: If ``shard_count`` is not positive.
    """
    if shard_count <= 0:
        raise ValueError("shard_count must be positive")
    digest = hashlib.sha256(asset_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % shard_count


def shard_name(index: int, shard_count: int) -> str:
    """Render a shard file stem such as ``assets-07`` with stable zero padding."""
    width = max(2, len(str(shard_count - 1)))
    return f"assets-{index:0{width}d}"
