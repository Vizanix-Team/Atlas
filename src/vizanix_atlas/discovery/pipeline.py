"""The aggregation pipeline.

Takes the per-venue :class:`~vizanix_atlas.models.observations.CollectionResult` records a
collection run produced and turns them into a complete generation: canonical assets,
normalised instruments, market states, venue decompositions and quality records.

This is the fan-in half of the GitHub Actions architecture. Each venue is collected in its
own matrix job and writes an artefact; this module reads them all back and produces the
dataset (see ``docs/GITHUB_ARCHITECTURE.md``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from vizanix_atlas.analytics.series import PriceSeries
from vizanix_atlas.analytics.state import AssetObservations, build_market_state
from vizanix_atlas.core.atlas_time import floor_to_slot, from_epoch_ms, to_epoch_ms, utc_now
from vizanix_atlas.core.config import CollectionConfig, OverrideConfig
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.core.versions import METHODOLOGY_VERSION, SCHEMA_VERSION
from vizanix_atlas.identity.resolver import AssetResolver
from vizanix_atlas.models.asset import AssetAlias, AssetRelationship, CanonicalAsset
from vizanix_atlas.models.enums import (
    CollectionStatus,
    CollectionTier,
    ObservationOrigin,
)
from vizanix_atlas.models.instrument import Instrument
from vizanix_atlas.models.observations import AdapterHealth, CollectionResult
from vizanix_atlas.models.quality import QualityEvent
from vizanix_atlas.models.state import AssetStateBundle
from vizanix_atlas.normalization.conversion import build_conversion_graph
from vizanix_atlas.normalization.instruments import InstrumentIndex, normalise_instruments
from vizanix_atlas.quality.validation import (
    ObservationValidator,
    QuarantineLog,
    deduplicate_instruments,
    validated_tickers,
)

_log = get_logger(__name__)


@dataclass(slots=True)
class Generation:
    """A complete, validated dataset generation, ready to be written.

    Everything a published generation contains, in memory. The dataset builder turns this
    into Parquet files and a manifest; nothing here knows about GitHub.
    """

    generation_id: str
    slot_label: str
    snapshot_effective_time: int
    collection_started_at: int
    collection_finished_at: int
    assets: tuple[CanonicalAsset, ...] = ()
    aliases: tuple[AssetAlias, ...] = ()
    relationships: tuple[AssetRelationship, ...] = ()
    instruments: tuple[Instrument, ...] = ()
    bundles: tuple[AssetStateBundle, ...] = ()
    health: tuple[AdapterHealth, ...] = ()
    quality_events: tuple[QualityEvent, ...] = ()
    resolution_summary: dict[str, int] = field(default_factory=dict)
    quarantine_summary: dict[str, int] = field(default_factory=dict)
    normalisation_summary: dict[str, int] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    methodology_version: str = METHODOLOGY_VERSION
    origin: ObservationOrigin = ObservationOrigin.SCHEDULED_SNAPSHOT

    @property
    def asset_count(self) -> int:
        """Assets with a published state."""
        return len(self.bundles)

    @property
    def instrument_count(self) -> int:
        """Instruments in the generation."""
        return len(self.instruments)

    @property
    def venue_count(self) -> int:
        """Venues that contributed at least one observation."""
        return len(
            {
                h.venue_slug
                for h in self.health
                if h.status in (CollectionStatus.SUCCESS.value, CollectionStatus.DEGRADED.value)
            }
        )

    def venues_by_status(self, status: CollectionStatus) -> tuple[str, ...]:
        """Return the venue slugs with the given collection status, sorted."""
        return tuple(sorted(h.venue_slug for h in self.health if h.status == status.value))


def build_generation(
    results: Sequence[CollectionResult],
    *,
    config: CollectionConfig,
    overrides: OverrideConfig | None = None,
    generation_id: str | None = None,
    snapshot_effective_time: int | None = None,
    history: dict[str, PriceSeries] | None = None,
    origin: ObservationOrigin = ObservationOrigin.SCHEDULED_SNAPSHOT,
) -> Generation:
    """Turn per-venue collection results into one complete generation.

    Args:
        results: One record per venue that was attempted, including failures.
        config: Collection policy.
        overrides: Identity overrides; loaded from configuration when omitted.
        generation_id: Immutable generation identifier; derived from the slot when omitted.
        snapshot_effective_time: The slot this generation is attributed to; derived from
            the collection window when omitted, so that a late GitHub Actions start does
            not misattribute the snapshot.
        history: Per-asset reference-price history for the windowed metrics.
        origin: How these observations were obtained.

    """
    started = min((r.collection_started_at for r in results), default=to_epoch_ms(utc_now()))
    finished = max((r.collection_finished_at for r in results), default=started)
    effective = snapshot_effective_time or to_epoch_ms(
        floor_to_slot(from_epoch_ms(started), slot_minutes=config.schedule.slot_minutes)
    )
    slot = floor_to_slot(from_epoch_ms(effective), slot_minutes=config.schedule.slot_minutes)
    from vizanix_atlas.core.atlas_time import slot_label as render_slot

    label = render_slot(slot)
    identifier = generation_id or label

    # 1. Resolve identity across every venue at once, because whether a ticker is
    # ambiguous is a global question.
    resolver = AssetResolver(overrides=overrides)
    all_raw_instruments = [i for result in results for i in result.instruments]
    resolver.ingest(all_raw_instruments)
    resolver.resolve_all()

    # 2. Normalise instruments into canonical identities.
    instruments, normalisation = normalise_instruments(all_raw_instruments, resolver)
    deduplicated, duplicates = deduplicate_instruments(instruments)
    index = InstrumentIndex(deduplicated)

    # 3. Build the conversion graph from observed rates.
    conversion = build_conversion_graph(
        [fx for result in results for fx in result.fx_observations], resolver, config.quality
    )

    # 4. Validate observations and quarantine the failures.
    validator = ObservationValidator(config.quality)
    quarantine = QuarantineLog(generation_id=identifier)
    accepted, considered = validated_tickers(
        (t for result in results for t in result.tickers),
        index,
        validator,
        quarantine,
        # Staleness is judged against when Atlas finished observing, not against the
        # floored slot label, which sits before every observation in the run.
        reference_ms=finished,
    )

    # 5. Group everything by asset.
    grouped = _group_by_asset(
        accepted,
        results=results,
        index=index,
        resolver=resolver,
        validator=validator,
        quarantine=quarantine,
        history=history or {},
    )

    # 6. Build a state per asset.
    bundles = tuple(
        build_market_state(
            observations,
            conversion=conversion,
            config=config,
            generation_id=identifier,
            snapshot_effective_time=effective,
            observed_through=finished,
            coverage_tier=(
                CollectionTier.B_LIQUIDITY
                if observations.order_books
                else CollectionTier.A_UNIVERSAL
            ),
            origin=origin,
        )
        for observations in grouped
    )

    generation = Generation(
        generation_id=identifier,
        slot_label=label,
        snapshot_effective_time=effective,
        collection_started_at=started,
        collection_finished_at=finished,
        assets=resolver.assets(),
        aliases=resolver.aliases(),
        relationships=resolver.relationships(),
        instruments=deduplicated,
        bundles=tuple(sorted(bundles, key=lambda b: b.state.asset_id)),
        health=tuple(sorted((r.health for r in results), key=lambda h: h.venue_slug)),
        quality_events=tuple(quarantine.events),
        resolution_summary=resolver.report.summary(),
        quarantine_summary=quarantine.summary(),
        normalisation_summary={
            **normalisation.summary(),
            "duplicates_set_aside": len(duplicates),
            "observations_considered": considered,
            "observations_accepted": len(accepted),
        },
        origin=origin,
    )
    _log.info(
        "generation built",
        extra={
            "generation_id": identifier,
            "assets": generation.asset_count,
            "instruments": generation.instrument_count,
            "venues": generation.venue_count,
            "quarantined": quarantine.total,
        },
    )
    return generation


def _group_by_asset(
    accepted: Sequence[tuple[object, Instrument]],
    *,
    results: Sequence[CollectionResult],
    index: InstrumentIndex,
    resolver: AssetResolver,
    validator: ObservationValidator,
    quarantine: QuarantineLog,
    history: dict[str, PriceSeries],
) -> list[AssetObservations]:
    """Collect every observation for each asset into one bundle.

    Assets are keyed by canonical identifier, so an ambiguous ticker's venue-scoped
    identities each get their own bundle and are never pooled.
    """
    assets = {asset.asset_id: asset for asset in resolver.assets()}
    grouped: dict[str, AssetObservations] = {}
    listing_venues: dict[str, set[str]] = {}

    for instrument in index.all():
        listing_venues.setdefault(instrument.base_asset_id, set()).add(instrument.venue_slug)

    def bundle_for(asset_id: str) -> AssetObservations:
        existing = grouped.get(asset_id)
        if existing is not None:
            return existing
        asset = assets.get(asset_id) or CanonicalAsset(asset_id=asset_id, symbol=asset_id)
        created = AssetObservations(
            asset=asset,
            listing_venues=set(listing_venues.get(asset_id, ())),
            expected_venues=set(listing_venues.get(asset_id, ())),
            history=history.get(asset_id, PriceSeries(points=())),
        )
        grouped[asset_id] = created
        return created

    for ticker, instrument in accepted:
        bundle_for(instrument.base_asset_id).tickers.append((ticker, instrument))  # type: ignore[arg-type]

    for result in results:
        for observation in result.derivatives:
            derivative_instrument = index.by_venue_symbol(
                observation.venue_slug,
                observation.symbol_native,
                instrument_class=observation.instrument_class,
            )
            if derivative_instrument is not None:
                bundle_for(derivative_instrument.base_asset_id).derivatives.append(
                    (observation, derivative_instrument)
                )
        for book in result.order_books:
            book_instrument = index.by_venue_symbol(
                book.venue_slug, book.symbol_native, instrument_class=book.instrument_class
            )
            if book_instrument is None:
                continue
            outcome = validator.validate_order_book(book, book_instrument)
            if outcome.valid:
                bundle_for(book_instrument.base_asset_id).order_books.append(
                    (book, book_instrument)
                )
            else:
                quarantine.record(
                    outcome,
                    venue_slug=book.venue_slug,
                    instrument_id=instrument.instrument_id,
                    asset_id=instrument.base_asset_id,
                    observed_at=book.timing.best_effective_time,
                )

    # Only assets with at least one accepted quote get a state. An asset listed everywhere
    # but quoted nowhere has no observable market this snapshot.
    return [bundle for _, bundle in sorted(grouped.items()) if bundle.tickers]
