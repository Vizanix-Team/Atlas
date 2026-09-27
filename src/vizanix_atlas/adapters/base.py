"""The exchange adapter contract.

An adapter's single responsibility is to turn one venue's public API into Atlas's
raw observation models. Everything venue-specific stops here: no adapter emits a
canonical asset ID, and no code above this layer knows that OKX says ``instId``.

Adapters do not implement methods their venue does not support. Capability
detection drives what the runtime asks for, and the base class raises
:class:`~vizanix_atlas.core.errors.UnsupportedCapability` for anything else, which
the runtime records as expected rather than as a fault.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from typing import Any, ClassVar, Final

from vizanix_atlas.core.atlas_time import to_epoch_ms, utc_now
from vizanix_atlas.core.errors import SchemaMismatch, UnsupportedCapability
from vizanix_atlas.core.http import ExchangeHttpClient
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.models.observations import (
    ObservationTiming,
    OrderBookLevel,
    RawDerivativeObservation,
    RawFxObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)
from vizanix_atlas.models.venue import Venue

_log = get_logger(__name__)

#: How many order-book levels an adapter keeps by default.
DEFAULT_BOOK_DEPTH: Final = 100


class ExchangeAdapter(abc.ABC):
    """Base class for every venue adapter.

    Subclasses declare ``slug`` and ``base_url``, then implement the operations
    their venue actually supports. Three hooks exist:

    ``discover_instruments``
        Required. Must enumerate every eligible active public instrument from the
        venue's own catalogue. A hardcoded symbol list is not an implementation.
    ``fetch_tickers``
        Required. Must use bulk endpoints.
    ``fetch_derivatives``, ``fetch_order_book``, ``fetch_fx_observations``
        Optional, gated on capabilities.
    """

    #: Registry key. Must match an ``id`` in ``config/exchanges.yaml``.
    slug: ClassVar[str]

    #: Default API host. May be overridden per instance for a regional endpoint.
    base_url: ClassVar[str]

    def __init__(self, venue: Venue, client: ExchangeHttpClient) -> None:
        """Bind an adapter to its configuration and its HTTP client."""
        self.venue = venue
        self.client = client
        self._now_ms = to_epoch_ms(utc_now())

    # ----------------------------------------------------------------- required

    @abc.abstractmethod
    async def discover_instruments(self) -> list[RawInstrument]:
        """Enumerate every eligible active public instrument on the venue.

        Must read the venue's own catalogue so that a newly listed market is picked
        up without a code change.
        """

    @abc.abstractmethod
    async def fetch_tickers(self) -> list[RawTicker]:
        """Fetch a summary quote for every instrument, using bulk endpoints."""

    # ----------------------------------------------------------------- optional

    async def fetch_derivatives(self) -> list[RawDerivativeObservation]:
        """Fetch mark, index, funding and open interest for derivative instruments.

        Raises:
            UnsupportedCapability: For a spot-only venue. Recorded as expected.
        """
        raise UnsupportedCapability(
            "this venue exposes no derivative observations", venue=self.slug
        )

    async def fetch_order_book(
        self, symbol_native: str, *, depth: int = DEFAULT_BOOK_DEPTH
    ) -> RawOrderBook:
        """Fetch one order-book snapshot.

        Raises:
            UnsupportedCapability: When the venue has no public book endpoint.
        """
        raise UnsupportedCapability("this venue exposes no order-book endpoint", venue=self.slug)

    async def fetch_fx_observations(self) -> list[RawFxObservation]:
        """Fetch observed rates between quote currencies.

        Default implementation returns nothing. Venues that list stablecoin or fiat
        pairs override it so the quote-conversion graph has observed edges rather
        than assumptions.
        """
        return []

    async def server_time_ms(self) -> int | None:
        """Fetch the venue's own clock, for skew measurement.

        Returns ``None`` when the venue publishes no clock, rather than substituting
        the collector's time, which would report zero skew for every such venue.
        """
        return None

    # -------------------------------------------------------------- utilities

    def timing(
        self,
        *,
        exchange_event_time: int | None = None,
        exchange_server_time: int | None = None,
    ) -> ObservationTiming:
        """Build an :class:`ObservationTiming` with the collector's receive time filled in.

        Uses a per-run receive time rather than calling the clock per record: within
        one bulk response every record was received together, and a per-record clock
        read would imply a precision that does not exist.
        """
        return ObservationTiming(
            collector_receive_time=self._now_ms,
            exchange_event_time=exchange_event_time,
            exchange_server_time=exchange_server_time,
        )

    def refresh_receive_time(self) -> None:
        """Re-read the collector clock before a new bulk request."""
        self._now_ms = to_epoch_ms(utc_now())

    @staticmethod
    def require_list(payload: Any, *, path: str, key: str | None = None) -> list[Any]:
        """Return a list from ``payload``, raising if the shape is wrong.

        The common adapter guard: a venue that starts returning an object where a
        list was documented must fail loudly, because silently iterating a dict's
        keys produces garbage.

        Raises:
            SchemaMismatch: If the value is missing or is not a list.
        """
        value = payload if key is None else (payload or {}).get(key)
        if not isinstance(value, list):
            raise SchemaMismatch(
                "expected a JSON array",
                path=path,
                key=key,
                actual_type=type(value).__name__,
            )
        return value

    @staticmethod
    def require_mapping(payload: Any, *, path: str, key: str | None = None) -> dict[str, Any]:
        """Return a mapping from ``payload``, raising if the shape is wrong.

        Raises:
            SchemaMismatch: If the value is missing or is not an object.
        """
        value = payload if key is None else (payload or {}).get(key)
        if not isinstance(value, dict):
            raise SchemaMismatch(
                "expected a JSON object",
                path=path,
                key=key,
                actual_type=type(value).__name__,
            )
        return value

    def build_levels(
        self,
        rows: Sequence[Any],
        *,
        depth: int,
        descending: bool,
    ) -> tuple[tuple[OrderBookLevel, ...], bool]:
        """Turn raw ``[price, size, ...]`` rows into ordered order-book levels.

        Every supported venue serialises book levels as positional arrays whose
        first two elements are price and size; the trailing elements differ and are
        ignored. Zero-size levels are dropped because several venues use them as
        deletion markers in snapshots.

        Levels are re-sorted rather than trusted, so that a venue changing its
        ordering produces correct depth rather than a silently wrong number.

        Returns:
            The levels, best price first, and whether the venue returned more than
            ``depth`` of them.
        """
        levels: list[OrderBookLevel] = []
        for row in rows:
            if not isinstance(row, list | tuple) or len(row) < 2:
                continue
            price = _as_float(row[0])
            size = _as_float(row[1])
            if price is None or size is None or price <= 0 or size <= 0:
                continue
            levels.append(OrderBookLevel(price=price, size=size))
        levels.sort(key=lambda level: level.price, reverse=descending)
        truncated = len(levels) > depth
        return tuple(levels[:depth]), truncated

    def note_unknown_enum(self, field_name: str, value: str) -> None:
        """Record an unrecognised value in an enumerated venue field.

        The observation is still emitted where its meaning is unaffected; the count
        is what surfaces schema drift before it becomes a silent data error.
        """
        self.client.metrics.record_unknown_enum(field_name, value)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} slug={self.slug!r}>"


def _as_float(value: Any) -> float | None:
    """Local float coercion for order-book rows.

    Duplicates :func:`~vizanix_atlas.core.numeric.parse_float` deliberately: this is
    the hottest loop in the collector, running over hundreds of thousands of book
    levels, and the sentinel handling there is not needed for positional numerics.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
