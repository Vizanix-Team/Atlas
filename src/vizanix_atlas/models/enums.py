"""Enumerations used across the Universal Market Schema.

These are string enums so that a value survives a round trip through Parquet and
JSON unchanged and remains readable in a published file. Every member is a value
Atlas can actually produce; there are no aspirational members.
"""

from __future__ import annotations

from enum import StrEnum


class InstrumentType(StrEnum):
    """The kind of market an instrument represents.

    Kept deliberately small. Venue-specific product names (OKX's ``SWAP``,
    Kraken's ``flexible_futures``) are mapped onto these by their adapter, so that
    no venue vocabulary reaches analytics.
    """

    SPOT = "spot"
    PERPETUAL = "perpetual"
    FUTURE = "future"
    OPTION = "option"


class ContractType(StrEnum):
    """How a derivative contract is denominated and settled.

    The distinction drives open-interest and volume conversion, so it is modelled
    explicitly rather than inferred from the symbol.
    """

    LINEAR = "linear"
    INVERSE = "inverse"
    QUANTO = "quanto"


class OptionType(StrEnum):
    """Option exercise direction."""

    CALL = "call"
    PUT = "put"


class SettlementPeriod(StrEnum):
    """Whether a derivative ever settles."""

    PERPETUAL = "perpetual"
    DATED = "dated"


class ResolutionState(StrEnum):
    """Confidence in an asset identity.

    Only :attr:`RESOLVED` and :attr:`MANUAL_OVERRIDE` identities are aggregated
    across venues. The others exist so that an uncertain identity can be recorded
    without being merged (see ``docs/ASSET_RESOLUTION.md``).
    """

    RESOLVED = "resolved"
    PROBABLE = "probable"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    MANUAL_OVERRIDE = "manual_override"


class RelationshipType(StrEnum):
    """An edge in the asset graph.

    A wrapped or bridged representation is never collapsed into its reference
    asset. The relationship is recorded instead, so a consumer can decide whether
    to treat them together.
    """

    WRAPPED_OF = "wrapped_of"
    BRIDGED_REPRESENTATION_OF = "bridged_representation_of"
    TRACKS = "tracks"
    SETTLES_IN = "settles_in"
    QUOTED_IN = "quoted_in"
    UNDERLYING = "underlying"
    SAME_ASSET_ALIAS = "same_asset_alias"


class CollectionTier(StrEnum):
    """How deeply an instrument or asset was collected in a generation.

    Published on every state row so that a consumer never mistakes the absence of
    a tier B metric for a zero (see ``docs/ARCHITECTURE.md``).
    """

    A_UNIVERSAL = "A"
    B_LIQUIDITY = "B"
    C_ADVANCED = "C"


class CollectionStatus(StrEnum):
    """Outcome of one venue's collection attempt.

    :attr:`UNAVAILABLE_FROM_COLLECTOR_NETWORK` is separate from :attr:`FAILED`
    because it describes where Atlas is running rather than the venue's health.
    """

    SUCCESS = "success"
    DEGRADED = "degraded"
    FAILED = "failed"
    DISABLED = "disabled"
    UNAVAILABLE_FROM_COLLECTOR_NETWORK = "unavailable_from_collector_network"


class ExclusionReason(StrEnum):
    """Why a venue observation was excluded from a derived value.

    Every exclusion is recorded. An observation is never silently dropped, so that
    ``included + excluded`` always equals what was considered.
    """

    STALE = "stale"
    EXTREME_DEVIATION = "extreme_deviation"
    INVALID_QUOTE_CONVERSION = "invalid_quote_conversion"
    UNRESOLVED_INSTRUMENT = "unresolved_instrument"
    AMBIGUOUS_IDENTITY = "ambiguous_identity"
    CROSSED_BOOK = "crossed_book"
    SCHEMA_FAILURE = "schema_failure"
    MISSING_TIMESTAMP = "missing_timestamp"
    NON_POSITIVE_PRICE = "non_positive_price"
    MISSING_PRICE = "missing_price"
    INACTIVE_INSTRUMENT = "inactive_instrument"
    UNKNOWN_CONTRACT_MULTIPLIER = "unknown_contract_multiplier"
    UNDOCUMENTED_FUNDING_SEMANTICS = "undocumented_funding_semantics"
    DUPLICATE_INSTRUMENT = "duplicate_instrument"
    #: A derivative observation that was eligible but not needed, because qualifying
    #: spot venues were available. Recorded rather than dropped so that a reader can
    #: see the observation existed and why it was not used.
    SPOT_PREFERRED = "spot_preferred"


class ConversionMethod(StrEnum):
    """How a quote currency was converted to USD.

    Published alongside every USD-normalised figure. ``UNIT_ASSUMED`` is used only
    for a currency that *is* USD; it never means "assumed equal to USD".
    """

    UNIT_ASSUMED = "unit_assumed"
    DIRECT_OBSERVED = "direct_observed"
    DERIVED_VIA_INTERMEDIARY = "derived_via_intermediary"
    UNAVAILABLE = "unavailable"


class ReferencePriceMethod(StrEnum):
    """How a global reference price was produced."""

    SPOT_WEIGHTED_MEDIAN = "spot_weighted_median"
    SINGLE_VENUE_SPOT = "single_venue_spot"
    DERIVATIVE_FALLBACK = "derivative_fallback"
    UNAVAILABLE = "unavailable"


class LiquidationCapability(StrEnum):
    """What liquidation data a venue exposes.

    Modelled explicitly because liquidation semantics differ so much between
    venues that aggregating them blindly produces a meaningless number
    (see ``docs/METHODOLOGY.md``).
    """

    FULL_STREAM = "full_stream"
    RECENT_PUBLIC_FEED = "recent_public_feed"
    AGGREGATED_ONLY = "aggregated_only"
    NOT_AVAILABLE = "not_available"
    UNKNOWN_SEMANTICS = "unknown_semantics"


class FundingSemantics(StrEnum):
    """How a venue's published funding number must be interpreted.

    :attr:`RELATIVE_PER_INTERVAL` is the common case: a dimensionless rate applying
    over the venue's funding interval. :attr:`ABSOLUTE_PER_INTERVAL` is a currency
    amount per contract and requires the instrument's mark price to become a rate.
    :attr:`UNDOCUMENTED` means Atlas will not convert it at all.
    """

    RELATIVE_PER_INTERVAL = "relative_per_interval"
    ABSOLUTE_PER_INTERVAL = "absolute_per_interval"
    UNDOCUMENTED = "undocumented"


class OpenInterestUnit(StrEnum):
    """The unit a venue's open-interest figure is expressed in.

    :attr:`UNKNOWN` excludes the observation from aggregation rather than causing
    a guess.
    """

    CONTRACTS = "contracts"
    BASE_ASSET = "base_asset"
    QUOTE_CURRENCY = "quote_currency"
    USD = "usd"
    UNKNOWN = "unknown"


class ObservationOrigin(StrEnum):
    """How an observation entered the dataset.

    A backfilled record is always distinguishable from one captured live, because
    the two have different reliability and different timestamp semantics.
    """

    SCHEDULED_SNAPSHOT = "scheduled_snapshot"
    MANUAL_RUN = "manual_run"
    HISTORICAL_BACKFILL = "historical_backfill"


class GenomeStatus(StrEnum):
    """Market Genome lifecycle state.

    Atlas will not emit genome features before the minimum history exists, and
    says which state an asset is in rather than emitting approximate features.
    """

    INSUFFICIENT_HISTORY = "insufficient_history"
    WARMING_UP = "warming_up"
    AVAILABLE = "available"


class Universe(StrEnum):
    """A deterministic asset universe for market-wide statistics.

    Breadth over "all assets" is misleading when thousands of listings are
    inactive, so every market-wide figure names the universe it was computed over.
    """

    ALL_RESOLVED = "all_resolved"
    MULTI_VENUE = "multi_venue"
    LIQUID = "liquid"
    TOP_100_LIQUIDITY = "top_100_liquidity"
    TOP_500_LIQUIDITY = "top_500_liquidity"
    SPOT_ONLY = "spot_only"
    DERIVATIVE_ACTIVE = "derivative_active"


class PriceSource(StrEnum):
    """Which venue-published price a value was taken from.

    Recorded so that basis and dispersion are never computed from a mixture of
    mark, mid and last prices without that being visible.
    """

    MID = "mid"
    LAST = "last"
    MARK = "mark"
    INDEX = "index"
