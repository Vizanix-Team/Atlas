"""Observation models.

These are the typed records an adapter produces. They are the boundary: no
venue-specific field name exists above this layer.

Two principles are enforced structurally rather than by convention:

- Every observation carries ``collector_receive_time``, which is always available,
  separately from ``exchange_event_time`` and ``exchange_server_time``, which are
  not. Freshness is computed from what the venue actually said.
- Every numeric field is optional and defaults to ``None``. A venue that stops
  publishing open interest produces nulls, never zeros.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from vizanix_atlas.core.numeric import safe_divide
from vizanix_atlas.models.base import AtlasModel
from vizanix_atlas.models.enums import (
    ObservationOrigin,
    OpenInterestUnit,
    PriceSource,
)


class ObservationTiming(AtlasModel):
    """The several distinct times attached to one observation.

    Kept as one nested model so that every observation type gets the full set and
    none of them can quietly reduce to a single ``timestamp``.
    """

    collector_receive_time: int = Field(
        description="Epoch milliseconds when Atlas received the response. Always present."
    )
    exchange_event_time: int | None = Field(
        default=None,
        description=(
            "Epoch milliseconds the venue attributes to the market event. Null when the "
            "venue does not publish one, which several bulk ticker endpoints do not."
        ),
    )
    exchange_server_time: int | None = Field(
        default=None, description="Epoch milliseconds the venue says it generated the response."
    )

    @property
    def best_effective_time(self) -> int:
        """The most authoritative time available, preferring venue-supplied values.

        Falls back to the collector's receive time, which overstates freshness for a
        venue that publishes no timestamp. That limitation is why
        ``exchange_event_time`` is published separately rather than being folded in.
        """
        return self.exchange_event_time or self.exchange_server_time or self.collector_receive_time


class RawInstrument(AtlasModel):
    """An instrument as one adapter discovered it, before identity resolution.

    Base and quote assets are still venue strings here. Turning them into canonical
    asset IDs is the resolver's job, and doing it inside the adapter would put
    identity decisions in fifteen places.
    """

    venue_slug: str
    symbol_native: str
    instrument_class: str
    instrument_type: str
    base_symbol_native: str
    quote_symbol_native: str
    settlement_symbol_native: str | None = None
    base_name: str | None = Field(
        default=None, description="Venue-supplied asset name, used as resolution evidence."
    )
    base_contract_address: str | None = Field(
        default=None,
        description="Venue-supplied token contract, the strongest evidence Atlas can get.",
    )
    base_chain_hint: str | None = Field(
        default=None, description="Venue-supplied network name, used as resolution evidence."
    )

    contract_type: str | None = None
    contract_multiplier: float | None = Field(default=None, gt=0)
    contract_value_symbol_native: str | None = None
    tick_size: float | None = Field(default=None, gt=0)
    quantity_step: float | None = Field(default=None, gt=0)
    minimum_quantity: float | None = Field(default=None, ge=0)
    minimum_notional: float | None = Field(default=None, ge=0)

    expiry: int | None = None
    strike: float | None = Field(default=None, gt=0)
    option_type: str | None = None

    funding_interval_hours: float | None = Field(default=None, gt=0)
    funding_semantics: str | None = None
    open_interest_unit: str | None = None

    active: bool = True
    listing_time: int | None = None


class RawTicker(AtlasModel):
    """A venue's summary quote for one instrument.

    ``base_volume_24h`` and ``quote_volume_24h`` are kept separate and are both
    optional, because a venue publishing only one of them must not appear to
    publish both.
    """

    venue_slug: str
    symbol_native: str
    instrument_class: str | None = Field(
        default=None,
        description=(
            "The RawInstrument.instrument_class this observation belongs to, when the "
            "venue reuses one native symbol across product lines (for example Gate.io's "
            "'ETH_USDT' spot pair and 'ETH_USDT' perpetual contract). Null when the "
            "venue's symbol space does not collide across instrument classes, which is "
            "the common case; the matcher then falls back to symbol_native alone."
        ),
    )
    timing: ObservationTiming

    last_price: float | None = Field(default=None, gt=0)
    bid_price: float | None = Field(default=None, gt=0)
    ask_price: float | None = Field(default=None, gt=0)
    bid_size: float | None = Field(default=None, ge=0)
    ask_size: float | None = Field(default=None, ge=0)

    open_24h: float | None = Field(default=None, gt=0)
    high_24h: float | None = Field(default=None, gt=0)
    low_24h: float | None = Field(default=None, gt=0)

    base_volume_24h: float | None = Field(
        default=None, ge=0, description="Venue-reported 24-hour volume in the base asset."
    )
    quote_volume_24h: float | None = Field(
        default=None, ge=0, description="Venue-reported 24-hour volume in the quote currency."
    )
    contract_volume_24h: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Venue-reported 24-hour volume in contracts, for derivatives that report it "
            "that way. Preserved so that the conversion to base units stays auditable."
        ),
    )
    trade_count_24h: int | None = Field(default=None, ge=0)

    @property
    def mid_price(self) -> float | None:
        """The midpoint of the quoted spread, or ``None`` when either side is missing.

        Returns ``None`` for a crossed book rather than a nonsensical mid: a crossed
        snapshot is a bad record, and the quality gate records it as such.
        """
        if self.bid_price is None or self.ask_price is None:
            return None
        if self.bid_price > self.ask_price:
            return None
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread_bps(self) -> float | None:
        """Top-of-book spread in basis points of the mid, or ``None``."""
        mid = self.mid_price
        if mid is None or self.ask_price is None or self.bid_price is None:
            return None
        return safe_divide((self.ask_price - self.bid_price) * 10_000.0, mid)

    @property
    def reference_candidate(self) -> tuple[float, PriceSource] | None:
        """The price Atlas would use for this instrument, and where it came from.

        Prefers the mid, because it is less sensitive to a single stale trade than
        the last price. Falls back to the last price, and records which was used so
        that a consumer can see the mixture.
        """
        mid = self.mid_price
        if mid is not None:
            return mid, PriceSource.MID
        if self.last_price is not None:
            return self.last_price, PriceSource.LAST
        return None


class RawDerivativeObservation(AtlasModel):
    """Derivative-specific values for one instrument.

    Separate from :class:`RawTicker` because a venue may publish these in a
    different request, at a different cadence, with a different timestamp. Merging
    them into one record would fabricate a single freshness for both.
    """

    venue_slug: str
    symbol_native: str
    instrument_class: str | None = Field(
        default=None,
        description=(
            "Disambiguates a native symbol the venue reuses across product lines. See "
            "RawTicker.instrument_class."
        ),
    )
    timing: ObservationTiming

    mark_price: float | None = Field(default=None, gt=0)
    index_price: float | None = Field(default=None, gt=0)

    funding_rate_raw: float | None = Field(
        default=None,
        description=(
            "Exactly what the venue published. Interpretation requires the "
            "instrument's funding_semantics and funding_interval_hours."
        ),
    )
    funding_interval_hours: float | None = Field(
        default=None,
        gt=0,
        description="Observed interval, when derivable from this response's own timestamps.",
    )
    next_funding_time: int | None = None
    previous_funding_time: int | None = None

    open_interest_raw: float | None = Field(default=None, ge=0)
    open_interest_unit: OpenInterestUnit | None = None
    open_interest_base: float | None = Field(
        default=None,
        ge=0,
        description="Open interest in base units, only when the venue publishes it directly.",
    )
    open_interest_usd: float | None = Field(
        default=None,
        ge=0,
        description="Open interest in USD, only when the venue publishes it directly.",
    )

    mark_iv: float | None = Field(
        default=None, ge=0, description="Venue mark implied volatility, for options."
    )
    bid_iv: float | None = Field(default=None, ge=0)
    ask_iv: float | None = Field(default=None, ge=0)
    underlying_price: float | None = Field(default=None, gt=0)


class OrderBookLevel(AtlasModel):
    """One price level in an order-book snapshot."""

    price: float = Field(gt=0)
    size: float = Field(ge=0)


class RawOrderBook(AtlasModel):
    """An order-book snapshot for one instrument.

    Depth is truncated by the adapter to the configured level count. Bids are
    ordered best-first (descending) and asks best-first (ascending); the validator
    enforces it so that depth integration can rely on it.
    """

    venue_slug: str
    symbol_native: str
    instrument_class: str | None = Field(
        default=None,
        description=(
            "Disambiguates a native symbol the venue reuses across product lines. See "
            "RawTicker.instrument_class."
        ),
    )
    timing: ObservationTiming
    bids: tuple[OrderBookLevel, ...] = ()
    asks: tuple[OrderBookLevel, ...] = ()
    truncated: bool = Field(
        default=False,
        description=(
            "Whether levels beyond the requested depth exist. A depth figure from a "
            "truncated book is a lower bound."
        ),
    )

    @property
    def best_bid(self) -> float | None:
        """The highest bid price, or ``None`` for an empty side."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        """The lowest ask price, or ``None`` for an empty side."""
        return self.asks[0].price if self.asks else None

    @property
    def is_crossed(self) -> bool:
        """Whether the best bid is at or above the best ask.

        A crossed snapshot is normal for a few milliseconds on a real venue but
        indicates an unusable snapshot here, so it is surfaced rather than ignored.
        """
        bid, ask = self.best_bid, self.best_ask
        return bid is not None and ask is not None and bid >= ask

    @model_validator(mode="after")
    def _check_ordering(self) -> RawOrderBook:
        """Enforce best-first ordering on both sides."""
        for side, levels, descending in (("bids", self.bids, True), ("asks", self.asks, False)):
            prices = [level.price for level in levels]
            expected = sorted(prices, reverse=descending)
            if prices != expected:
                raise ValueError(f"{side} must be ordered best price first")
        return self


class RawFxObservation(AtlasModel):
    """An observed exchange rate between two settlement or quote currencies.

    Collected so that ``USDT`` is never assumed equal to ``USD``. These populate
    the quote-conversion graph (see ``docs/METHODOLOGY.md``).
    """

    venue_slug: str
    symbol_native: str
    timing: ObservationTiming
    from_symbol_native: str
    to_symbol_native: str
    rate: float = Field(gt=0, description="Units of `to` per one unit of `from`.")
    source_price: PriceSource


class AdapterHealth(AtlasModel):
    """What happened during one venue's collection attempt.

    Published per generation so that partial coverage is visible and attributable
    (see ``docs/QUALITY.md``).
    """

    venue_slug: str
    status: str
    duration_ms: int = Field(ge=0)
    requests_attempted: int = Field(default=0, ge=0)
    requests_successful: int = Field(default=0, ge=0)
    timeouts: int = Field(default=0, ge=0)
    rate_limit_responses: int = Field(default=0, ge=0)
    application_errors: int = Field(default=0, ge=0)
    parse_failures: int = Field(default=0, ge=0)
    http_errors: int = Field(default=0, ge=0)
    instrument_count: int = Field(default=0, ge=0)
    ticker_count: int = Field(default=0, ge=0)
    derivative_observation_count: int = Field(default=0, ge=0)
    order_book_count: int = Field(default=0, ge=0)
    bytes_received: int = Field(default=0, ge=0)
    circuit_opened: bool = False
    error_types: dict[str, int] = Field(
        default_factory=dict, description="Counts keyed by error_type, for run triage."
    )
    unknown_enum_values: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Unrecognised values seen in enumerated venue fields. A schema-drift "
            "early-warning signal."
        ),
    )
    notes: tuple[str, ...] = ()
    clock_skew_ms: int | None = Field(
        default=None,
        description="Venue server time minus collector time, where the venue publishes a clock.",
    )


class CollectionResult(AtlasModel):
    """Everything one adapter produced in one run.

    This is the unit written to a GitHub Actions artefact by a matrix collection
    job and read back by the aggregation job.
    """

    venue_slug: str
    run_id: str
    origin: ObservationOrigin = ObservationOrigin.SCHEDULED_SNAPSHOT
    collection_started_at: int
    collection_finished_at: int
    health: AdapterHealth
    instruments: tuple[RawInstrument, ...] = ()
    tickers: tuple[RawTicker, ...] = ()
    derivatives: tuple[RawDerivativeObservation, ...] = ()
    order_books: tuple[RawOrderBook, ...] = ()
    fx_observations: tuple[RawFxObservation, ...] = ()
