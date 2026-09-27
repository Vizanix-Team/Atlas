"""Collection runtime.

Runs one venue's adapter and turns the result into a
:class:`~vizanix_atlas.models.observations.CollectionResult`, which is the unit a GitHub
Actions matrix job writes to an artefact and the aggregation job reads back.

The governing rule is that **one venue's failure is never global**. Every adapter call is
wrapped, every failure is classified and recorded against that venue's health, and the run
continues. A generation built from thirteen of sixteen venues is a real generation with
reduced coverage; a generation abandoned because one venue timed out is a wasted run.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass

from vizanix_atlas.adapters.base import ExchangeAdapter
from vizanix_atlas.adapters.registry import build_adapter
from vizanix_atlas.core.atlas_time import to_epoch_ms, utc_now
from vizanix_atlas.core.config import CollectionConfig
from vizanix_atlas.core.errors import (
    AtlasError,
    GeoRestricted,
    UnsupportedCapability,
)
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.models.enums import CollectionStatus, ObservationOrigin
from vizanix_atlas.models.observations import (
    AdapterHealth,
    CollectionResult,
    RawDerivativeObservation,
    RawFxObservation,
    RawInstrument,
    RawOrderBook,
    RawTicker,
)
from vizanix_atlas.models.venue import Venue

_log = get_logger(__name__)


@dataclass(slots=True)
class CollectionRequest:
    """What to collect from one venue in one run."""

    venue: Venue
    run_id: str
    #: Symbols to sample order books for. Empty means tier A only.
    order_book_symbols: tuple[str, ...] = ()
    order_book_depth: int = 100
    origin: ObservationOrigin = ObservationOrigin.SCHEDULED_SNAPSHOT
    #: Injected transport, used by tests so the suite never opens a socket.
    transport: object | None = None


async def collect_venue(
    request: CollectionRequest, config: CollectionConfig
) -> CollectionResult:
    """Collect everything Atlas wants from one venue.

    Never raises for a venue-side problem. A failure becomes a ``failed`` or
    ``unavailable_from_collector_network`` health record with the observations that did
    succeed still attached.
    """
    venue = request.venue
    started = to_epoch_ms(utc_now())
    clock = time.monotonic()

    instruments: tuple[RawInstrument, ...] = ()
    tickers: tuple[RawTicker, ...] = ()
    derivatives: tuple[RawDerivativeObservation, ...] = ()
    order_books: list[RawOrderBook] = []
    fx: tuple[RawFxObservation, ...] = ()
    notes: list[str] = []
    failures = 0
    operations = 0
    geo_restricted = False
    clock_skew_ms: int | None = None

    if not venue.enabled:
        # A disabled adapter is reported as such, with its reason, rather than silently
        # producing an empty result that would look like a working venue with no markets.
        return _result(
            request,
            started=started,
            status=CollectionStatus.DISABLED,
            duration_ms=0,
            notes=(venue.disabled_reason or "adapter disabled",),
        )

    adapter = build_adapter(venue, transport=request.transport)  # type: ignore[arg-type]
    try:
        # Discovery first: everything else is keyed by instrument.
        operations += 1
        try:
            instruments = tuple(await adapter.discover_instruments())
        except GeoRestricted as error:
            geo_restricted = True
            notes.append(f"venue refused the request from this network: {error.message}")
        except AtlasError as error:
            failures += 1
            notes.append(f"instrument discovery failed: {error.error_type}")
            _log_failure(venue.slug, "discover_instruments", error)

        operations += 1
        try:
            tickers = tuple(await adapter.fetch_tickers())
        except GeoRestricted as error:
            geo_restricted = True
            notes.append(f"venue refused the request from this network: {error.message}")
        except AtlasError as error:
            failures += 1
            notes.append(f"ticker collection failed: {error.error_type}")
            _log_failure(venue.slug, "fetch_tickers", error)

        if venue.capabilities.funding or venue.capabilities.open_interest:
            operations += 1
            try:
                derivatives = tuple(await adapter.fetch_derivatives())
            except UnsupportedCapability:
                # Declared as supported but not implemented for this product set. Not a
                # fault, and not counted against the venue.
                notes.append("no derivative observations were available")
            except AtlasError as error:
                failures += 1
                notes.append(f"derivative collection failed: {error.error_type}")
                _log_failure(venue.slug, "fetch_derivatives", error)

        operations += 1
        try:
            fx = tuple(await adapter.fetch_fx_observations())
        except (UnsupportedCapability, AtlasError) as error:
            # FX observations are best-effort: losing them costs USD normalisation
            # coverage for this venue's quote currencies, not the venue's data.
            notes.append(f"FX observations unavailable: {getattr(error, 'error_type', 'unknown')}")

        if request.order_book_symbols and venue.capabilities.orderbook:
            books, book_failures = await _collect_books(adapter, request)
            order_books.extend(books)
            failures += book_failures
            operations += len(request.order_book_symbols)

        if venue.capabilities.server_time:
            try:
                venue_time = await adapter.server_time_ms()
                if venue_time is not None:
                    clock_skew_ms = venue_time - to_epoch_ms(utc_now())
            except AtlasError:
                # Skew is diagnostic only; failing to measure it is not a venue failure.
                notes.append("server clock unavailable")
    finally:
        await adapter.client.aclose()

    duration_ms = int((time.monotonic() - clock) * 1000)
    status = _status(
        geo_restricted=geo_restricted,
        failures=failures,
        operations=operations,
        instruments=len(instruments),
        tickers=len(tickers),
    )

    return _result(
        request,
        started=started,
        status=status,
        duration_ms=duration_ms,
        instruments=instruments,
        tickers=tickers,
        derivatives=derivatives,
        order_books=tuple(order_books),
        fx=fx,
        notes=tuple(notes),
        metrics=adapter.client.metrics,
        circuit_opened=adapter.client.circuit.open_count > 0,
        clock_skew_ms=clock_skew_ms,
    )


async def _collect_books(
    adapter: ExchangeAdapter, request: CollectionRequest
) -> tuple[list[RawOrderBook], int]:
    """Sample order books for the selected symbols.

    Bounded by the venue's own concurrency limit, which the HTTP client already enforces,
    so the gather here cannot exceed it. One symbol failing costs that symbol only.
    """
    async def one(symbol: str) -> RawOrderBook | None:
        try:
            return await adapter.fetch_order_book(symbol, depth=request.order_book_depth)
        except UnsupportedCapability:
            return None
        except AtlasError as error:
            _log_failure(adapter.slug, "fetch_order_book", error, symbol=symbol)
            return None

    results = await asyncio.gather(*(one(s) for s in request.order_book_symbols))
    books = [book for book in results if book is not None]
    return books, len(request.order_book_symbols) - len(books)


def _status(
    *,
    geo_restricted: bool,
    failures: int,
    operations: int,
    instruments: int,
    tickers: int,
) -> CollectionStatus:
    """Classify a venue's collection outcome.

    ``unavailable_from_collector_network`` is kept distinct from ``failed`` because it says
    something about where Atlas is running rather than about the venue.
    """
    if geo_restricted and tickers == 0:
        return CollectionStatus.UNAVAILABLE_FROM_COLLECTOR_NETWORK
    if tickers == 0:
        return CollectionStatus.FAILED
    if failures > 0 or instruments == 0:
        # Quotes arrived but something else did not, so the venue contributed with
        # reduced coverage.
        return CollectionStatus.DEGRADED
    return CollectionStatus.SUCCESS


def _log_failure(venue: str, operation: str, error: AtlasError, **extra: object) -> None:
    """Record one adapter failure in the structured log."""
    _log.warning(
        "adapter operation failed",
        extra={
            "venue": venue,
            "operation": operation,
            "status": "failed",
            "error_type": error.error_type,
            **extra,
        },
    )


def _result(
    request: CollectionRequest,
    *,
    started: int,
    status: CollectionStatus,
    duration_ms: int,
    instruments: tuple[RawInstrument, ...] = (),
    tickers: tuple[RawTicker, ...] = (),
    derivatives: tuple[RawDerivativeObservation, ...] = (),
    order_books: tuple[RawOrderBook, ...] = (),
    fx: tuple[RawFxObservation, ...] = (),
    notes: tuple[str, ...] = (),
    metrics: object | None = None,
    circuit_opened: bool = False,
    clock_skew_ms: int | None = None,
) -> CollectionResult:
    """Package one venue's collection into a result record."""
    health = AdapterHealth(
        venue_slug=request.venue.slug,
        status=status.value,
        duration_ms=duration_ms,
        requests_attempted=getattr(metrics, "attempted", 0),
        requests_successful=getattr(metrics, "successful", 0),
        timeouts=getattr(metrics, "timeouts", 0),
        rate_limit_responses=getattr(metrics, "rate_limited", 0),
        application_errors=getattr(metrics, "application_errors", 0),
        parse_failures=getattr(metrics, "parse_failures", 0),
        http_errors=getattr(metrics, "http_errors", 0),
        instrument_count=len(instruments),
        ticker_count=len(tickers),
        derivative_observation_count=len(derivatives),
        order_book_count=len(order_books),
        bytes_received=getattr(metrics, "bytes_received", 0),
        circuit_opened=circuit_opened,
        error_types=dict(getattr(metrics, "error_types", {}) or {}),
        unknown_enum_values=dict(getattr(metrics, "unknown_enum_values", {}) or {}),
        notes=notes,
        clock_skew_ms=clock_skew_ms,
    )
    return CollectionResult(
        venue_slug=request.venue.slug,
        run_id=request.run_id,
        origin=request.origin,
        collection_started_at=started,
        collection_finished_at=to_epoch_ms(utc_now()),
        health=health,
        instruments=instruments,
        tickers=tickers,
        derivatives=derivatives,
        order_books=order_books,
        fx_observations=fx,
    )


async def collect_many(
    requests: Sequence[CollectionRequest], config: CollectionConfig
) -> list[CollectionResult]:
    """Collect from several venues concurrently.

    Used for a local run. In production each venue is its own matrix job, so that a venue
    exhausting a job timeout cannot take the others with it.
    """
    results = await asyncio.gather(
        *(collect_venue(request, config) for request in requests),
        return_exceptions=False,
    )
    for result in results:
        _log.info(
            "venue collection finished",
            extra={
                "venue": result.venue_slug,
                "status": result.health.status,
                "duration_ms": result.health.duration_ms,
                "instrument_count": result.health.instrument_count,
                "ticker_count": result.health.ticker_count,
                "requests": result.health.requests_attempted,
            },
        )
    return list(results)
