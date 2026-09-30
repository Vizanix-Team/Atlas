"""Asset identity and the asset graph.

The asset layer is what makes Atlas asset-centric rather than exchange-centric. A
canonical asset is the thing a user asks about (``BTC``); aliases are the venue
symbols that point at it; relationships record how assets relate without merging
them (see ``docs/ASSET_RESOLUTION.md``).
"""

from __future__ import annotations

from pydantic import Field

from vizanix_atlas.models.base import AtlasModel
from vizanix_atlas.models.enums import RelationshipType, ResolutionState


class AssetAlias(AtlasModel):
    """A venue's symbol for an asset, with the evidence that linked it.

    Atlas keeps the evidence rather than only the conclusion, because a mapping
    that cannot be justified cannot be reviewed.
    """

    asset_id: str
    venue_slug: str
    venue_symbol: str = Field(description="The venue's own code, preserved verbatim.")
    resolution_state: ResolutionState
    evidence: tuple[str, ...] = Field(
        default=(),
        description=(
            "Ordered evidence keys that produced this mapping, for example "
            "'venue_contract_address' or 'override:config/asset_overrides.yaml'."
        ),
    )
    confidence_note: str | None = Field(
        default=None, description="A short human note for probable or ambiguous mappings."
    )


class AssetRelationship(AtlasModel):
    """A typed edge between two canonical assets.

    Directed: ``from_asset_id`` stands in the stated relationship to
    ``to_asset_id``. ``wrapped_of`` means the source is a wrapped representation of
    the target, and never that the two are interchangeable.
    """

    from_asset_id: str
    to_asset_id: str
    relationship: RelationshipType
    source: str = Field(
        description="Where the relationship came from: a config override or a venue field."
    )
    note: str | None = None


class CanonicalAsset(AtlasModel):
    """An asset in Atlas's own namespace.

    Attributes:
        asset_id: Canonical identifier. The only field safe to join on.
        symbol: A display ticker. Convenient, and deliberately not an identity: two
            canonical assets may share one.
        chain_slug: The chain the asset is native to, where it has one.
        contract_address: Token contract, when the asset is a token.
        chain_id: EVM chain ID, when applicable.
        resolution_state: How confidently this identity was established.
        is_stablecoin: Whether the asset is issued as a value-tracking instrument.
            Used to build the quote-conversion graph, never to assume a price.
        is_fiat: Whether the asset is a government-issued currency.

    """

    asset_id: str
    symbol: str
    name: str | None = None
    chain_slug: str | None = None
    chain_id: int | None = None
    contract_address: str | None = None
    resolution_state: ResolutionState = ResolutionState.RESOLVED
    is_stablecoin: bool = False
    is_fiat: bool = False
    tracks_asset_id: str | None = Field(
        default=None,
        description=(
            "What a stablecoin is designed to track. A design intention, not an "
            "observed price: Atlas still measures the rate."
        ),
    )
    first_seen_at: int | None = Field(
        default=None, description="Epoch milliseconds of the first discovery run that saw it."
    )
    last_seen_at: int | None = None
    venue_count: int = Field(
        default=0, description="Venues with at least one alias pointing at this asset."
    )

    @property
    def is_unresolved(self) -> bool:
        """Whether this is a venue-scoped placeholder rather than a shared identity."""
        return self.resolution_state is ResolutionState.UNRESOLVED

    @property
    def aggregatable(self) -> bool:
        """Whether observations on this identity may be combined across venues.

        Only confidently resolved identities qualify. An ambiguous or unresolved
        identity stays venue-scoped, which is the conservative behaviour Atlas
        prefers over a wrong merge.
        """
        return self.resolution_state in (
            ResolutionState.RESOLVED,
            ResolutionState.MANUAL_OVERRIDE,
        )
