"""Venue and capability models.

Venues differ in what they expose, and Atlas models that explicitly rather than
pretending every market has identical data. A capability is a declared,
test-covered fact about a venue's public API, not an aspiration.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from vizanix_atlas.models.base import AtlasModel
from vizanix_atlas.models.enums import LiquidationCapability


class VenueCapabilities(AtlasModel):
    """What a venue's public API exposes.

    Every field here is asserted by a contract test against a recorded fixture, so
    that a capability claim cannot drift away from the adapter's behaviour.
    """

    spot: bool = False
    perpetual: bool = False
    future: bool = False
    option: bool = False

    bulk_tickers: bool = Field(
        default=False,
        description=(
            "Whether all tickers can be retrieved in a small number of requests. "
            "Atlas requires this for tier A coverage of an entire venue."
        ),
    )
    bulk_instruments: bool = Field(
        default=False, description="Whether the instrument catalogue is retrievable in bulk."
    )
    orderbook: bool = Field(
        default=False, description="Whether a public order-book snapshot endpoint exists."
    )
    top_of_book_in_bulk: bool = Field(
        default=False,
        description=(
            "Whether the bulk ticker response carries best bid and ask. When false, "
            "spread requires a per-instrument request and is therefore tier B only."
        ),
    )
    funding: bool = False
    open_interest: bool = False
    index_price: bool = False
    mark_price: bool = False
    server_time: bool = Field(
        default=False,
        description=(
            "Whether the venue publishes its own clock, letting Atlas measure skew "
            "against the collector."
        ),
    )
    quote_volume_in_bulk: bool = Field(
        default=False,
        description=(
            "Whether bulk tickers report quote-currency volume. When false, Atlas "
            "publishes base volume only and leaves quote volume null."
        ),
    )
    liquidations: LiquidationCapability = LiquidationCapability.NOT_AVAILABLE

    def supports_instrument_type(self, instrument_type: str) -> bool:
        """Return whether the venue lists the given instrument type."""
        return bool(getattr(self, instrument_type, False))


class RateLimitPolicy(AtlasModel):
    """A venue's documented request budget, as Atlas will respect it.

    Atlas targets a fraction of each documented limit rather than the limit itself,
    because a shared GitHub-hosted runner address may be used by other consumers
    and because bursting to a documented ceiling is a good way to be blocked.
    """

    requests_per_second: float = Field(
        gt=0, description="Sustained request rate Atlas will not exceed."
    )
    burst: int = Field(
        default=1, ge=1, description="Token bucket capacity, bounding instantaneous bursts."
    )
    max_concurrency: int = Field(
        default=4, ge=1, le=64, description="Simultaneous in-flight requests to this venue."
    )
    request_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    max_retries: int = Field(
        default=3, ge=0, le=8, description="Attempts after the first, for retryable failures only."
    )
    max_response_bytes: int = Field(
        default=48 * 1024 * 1024,
        gt=0,
        description=(
            "Body budget. Exceeding it abandons the response rather than risking "
            "runner memory on an unexpected venue change."
        ),
    )
    documented_limit: str | None = Field(
        default=None,
        description="The venue's own published limit, quoted so the headroom is auditable.",
    )


class Venue(AtlasModel):
    """A trading venue Atlas collects from."""

    venue_id: str = Field(description="Canonical identifier, for example venue:okx.")
    slug: str = Field(description="Short lowercase key used in configuration and file paths.")
    display_name: str
    enabled: bool = Field(
        default=True,
        description="Whether the adapter participates in collection. Disabled adapters carry a reason.",
    )
    disabled_reason: str | None = Field(
        default=None,
        description="Why the adapter is disabled. Required whenever enabled is false.",
    )
    capabilities: VenueCapabilities = VenueCapabilities()
    rate_limit: RateLimitPolicy
    documentation_url: str | None = None
    terms_url: str | None = None
    terms_reviewed_on: str | None = Field(
        default=None, description="ISO date on which the venue's data terms were last reviewed."
    )
    redistributes_raw_observations: bool = Field(
        default=False,
        description=(
            "Whether Atlas republishes this venue's observations as rows. Defaults to "
            "false: the conservative position when redistribution rights are unclear."
        ),
    )
    attribution: str | None = Field(
        default=None, description="Attribution text the venue's terms require."
    )
    priority: str = Field(
        default="standard", description="Collection priority band: core, standard or extended."
    )

    @field_validator("disabled_reason")
    @classmethod
    def _reason_required_when_disabled(cls, value: str | None, info: object) -> str | None:
        """Require a reason for every disabled adapter.

        A disabled adapter with no explanation becomes folklore. Enforcing the
        reason at the schema level keeps ``docs/EXCHANGE_ADAPTERS.md`` honest.
        """
        data = getattr(info, "data", {})
        if data.get("enabled") is False and not value:
            raise ValueError("a disabled venue must record disabled_reason")
        return value
