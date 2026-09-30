"""Instrument normalisation.

Turns a :class:`RawInstrument` (venue strings) into an :class:`Instrument` (canonical
identities and Atlas's own enums). This is where venue vocabulary is finally discarded.

Nothing is guessed. A contract multiplier the venue did not publish stays ``None``,
which excludes that instrument from open-interest normalisation rather than producing
a plausible-looking wrong number.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeVar

from vizanix_atlas.core.errors import SchemaMismatch
from vizanix_atlas.core.identifiers import instrument_id
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.identity.resolver import AssetResolver
from vizanix_atlas.models.enums import (
    ContractType,
    FundingSemantics,
    InstrumentType,
    OpenInterestUnit,
    OptionType,
    SettlementPeriod,
)
from vizanix_atlas.models.instrument import Instrument
from vizanix_atlas.models.observations import RawInstrument

_log = get_logger(__name__)

_EnumT = TypeVar("_EnumT", bound=StrEnum)


@dataclass(slots=True)
class NormalisationReport:
    """What happened during instrument normalisation."""

    normalised: int = 0
    rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    missing_multiplier: int = 0
    unknown_open_interest_unit: int = 0

    def reject(self, reason: str) -> None:
        """Count one rejected instrument."""
        self.rejected += 1
        self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1

    def summary(self) -> dict[str, int]:
        """Return counts for logging and the quality tables."""
        return {
            "normalised": self.normalised,
            "rejected": self.rejected,
            "missing_multiplier": self.missing_multiplier,
            "unknown_open_interest_unit": self.unknown_open_interest_unit,
        }


def _enum_or_none(enum_cls: type[_EnumT], value: str | None) -> _EnumT | None:
    """Coerce a venue string into one of Atlas's enums, or ``None``.

    Returning ``None`` rather than raising lets normalisation reject one instrument
    without losing the venue's other thousands.
    """
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        return None


def normalise_symbol(base: str, quote: str) -> str:
    """Render a readable, venue-independent symbol such as ``BTC/USDT``.

    For display and search only. Joining on this would be wrong, because two venues can
    produce the same normalised symbol for different instruments.
    """
    return f"{base.upper()}/{quote.upper()}"


def normalise_instrument(
    raw: RawInstrument, resolver: AssetResolver, report: NormalisationReport
) -> Instrument | None:
    """Normalise one instrument, or return ``None`` if it cannot be represented.

    Rejection reasons are counted in ``report`` so that a venue whose catalogue changes
    shape shows up as a rising rejection count rather than as quietly missing markets.
    """
    instrument_type = _enum_or_none(InstrumentType, raw.instrument_type)
    if instrument_type is None:
        report.reject("unknown_instrument_type")
        return None

    base_asset_id = resolver.asset_id_for(raw.venue_slug, raw.base_symbol_native)
    quote_asset_id = resolver.asset_id_for(raw.venue_slug, raw.quote_symbol_native)
    settlement_asset_id = (
        resolver.asset_id_for(raw.venue_slug, raw.settlement_symbol_native)
        if raw.settlement_symbol_native
        else None
    )

    contract_type = _enum_or_none(ContractType, raw.contract_type)
    if raw.contract_type is not None and contract_type is None:
        report.reject("unknown_contract_type")
        return None

    option_type = _enum_or_none(OptionType, raw.option_type)
    if raw.option_type is not None and option_type is None:
        report.reject("unknown_option_type")
        return None

    open_interest_unit = _enum_or_none(OpenInterestUnit, raw.open_interest_unit)
    if instrument_type is not InstrumentType.SPOT and open_interest_unit is None:
        # A derivative whose open-interest unit is unknown keeps its raw value but is
        # excluded from aggregation. Recorded explicitly rather than assumed.
        open_interest_unit = OpenInterestUnit.UNKNOWN
        report.unknown_open_interest_unit += 1

    funding_semantics = _enum_or_none(FundingSemantics, raw.funding_semantics)
    if raw.funding_semantics is not None and funding_semantics is None:
        funding_semantics = FundingSemantics.UNDOCUMENTED

    settlement_period: SettlementPeriod | None = None
    if instrument_type is InstrumentType.PERPETUAL:
        settlement_period = SettlementPeriod.PERPETUAL
    elif instrument_type in (InstrumentType.FUTURE, InstrumentType.OPTION):
        settlement_period = SettlementPeriod.DATED

    if (
        instrument_type is not InstrumentType.SPOT
        and open_interest_unit is OpenInterestUnit.CONTRACTS
        and raw.contract_multiplier is None
    ):
        # Counted so the effect on open-interest coverage is visible. The instrument is
        # still published; it just cannot be converted.
        report.missing_multiplier += 1

    identifier = instrument_id(raw.venue_slug, raw.instrument_class, raw.symbol_native)
    try:
        instrument = Instrument(
            instrument_id=identifier,
            venue_slug=raw.venue_slug,
            instrument_type=instrument_type,
            instrument_class=raw.instrument_class,
            symbol_native=raw.symbol_native,
            symbol_normalized=normalise_symbol(raw.base_symbol_native, raw.quote_symbol_native),
            base_asset_id=base_asset_id,
            quote_asset_id=quote_asset_id,
            settlement_asset_id=settlement_asset_id,
            underlying_asset_id=(
                base_asset_id
                if instrument_type in (InstrumentType.OPTION, InstrumentType.FUTURE)
                else None
            ),
            contract_type=contract_type,
            contract_multiplier=raw.contract_multiplier,
            contract_value_asset_id=(
                resolver.asset_id_for(raw.venue_slug, raw.contract_value_symbol_native)
                if raw.contract_value_symbol_native
                else None
            ),
            settlement_period=settlement_period,
            tick_size=raw.tick_size,
            quantity_step=raw.quantity_step,
            minimum_quantity=raw.minimum_quantity,
            minimum_notional=raw.minimum_notional,
            expiry=raw.expiry,
            strike=raw.strike,
            option_type=option_type,
            funding_interval_hours=raw.funding_interval_hours,
            funding_semantics=funding_semantics,
            open_interest_unit=open_interest_unit,
            active=raw.active,
            listing_time=raw.listing_time,
        )
    except ValueError as exc:
        # The Instrument model enforces cross-field coherence (an option needs a strike,
        # a perpetual must not have an expiry). A failure here is an adapter bug, so it
        # is counted and the instrument dropped rather than allowed through.
        report.reject("model_validation")
        _log.warning(
            "instrument failed validation",
            extra={
                "venue": raw.venue_slug,
                "instrument_id": identifier,
                "error_type": "InstrumentValidation",
                "detail": str(exc)[:200],
            },
        )
        return None

    report.normalised += 1
    return instrument


def normalise_instruments(
    raws: Iterable[RawInstrument], resolver: AssetResolver
) -> tuple[tuple[Instrument, ...], NormalisationReport]:
    """Normalise every instrument, dropping and counting the ones that cannot be.

    Output is sorted by identifier so that a generation built twice from the same
    observations serialises identically (see ``docs/METHODOLOGY.md`` on determinism).
    """
    report = NormalisationReport()
    normalised = [
        instrument
        for instrument in (normalise_instrument(raw, resolver, report) for raw in raws)
        if instrument is not None
    ]
    normalised.sort(key=lambda i: i.instrument_id)
    _log.info("instrument normalisation complete", extra=report.summary())
    return tuple(normalised), report


class InstrumentIndex:
    """Fast lookups over a generation's normalised instruments.

    Built once and shared, because analytics needs to go from a venue symbol to an
    instrument many times per asset.
    """

    def __init__(self, instruments: Sequence[Instrument]) -> None:
        self._by_id: dict[str, Instrument] = {i.instrument_id: i for i in instruments}
        # Keyed by (venue, class, symbol) for an exact match, and separately by
        # (venue, symbol) holding every instrument class that symbol maps to. A venue
        # normally has one instrument per native symbol; a few (Gate.io, Bitget) reuse a
        # symbol across product lines, which is why the second index can hold more than
        # one entry and callers must disambiguate by class when it does.
        self._by_venue_class_symbol: dict[tuple[str, str, str], Instrument] = {}
        self._by_venue_symbol: dict[tuple[str, str], list[Instrument]] = {}
        self._by_base_asset: dict[str, list[Instrument]] = {}
        self._collision_symbols: set[tuple[str, str]] = set()

        for instrument in instruments:
            class_key = (
                instrument.venue_slug,
                instrument.instrument_class,
                instrument.symbol_native,
            )
            self._by_venue_class_symbol[class_key] = instrument

            symbol_key = (instrument.venue_slug, instrument.symbol_native)
            bucket = self._by_venue_symbol.setdefault(symbol_key, [])
            bucket.append(instrument)
            if len(bucket) > 1:
                self._collision_symbols.add(symbol_key)

            self._by_base_asset.setdefault(instrument.base_asset_id, []).append(instrument)

        for symbol_key in self._collision_symbols:
            venue, symbol = symbol_key
            _log.info(
                "venue symbol appears in more than one instrument class",
                extra={
                    "venue": venue,
                    "symbol_native": symbol,
                    "classes": sorted(
                        i.instrument_class for i in self._by_venue_symbol[symbol_key]
                    ),
                },
            )

    def by_id(self, identifier: str) -> Instrument | None:
        """Return the instrument with this identifier."""
        return self._by_id.get(identifier)

    def by_venue_symbol(
        self, venue_slug: str, symbol_native: str, *, instrument_class: str | None = None
    ) -> Instrument | None:
        """Return the instrument a venue publishes under this symbol.

        ``instrument_class`` disambiguates a symbol a venue reuses across product lines
        (Gate.io and Bitget both reuse a native symbol between a spot pair and a
        derivative contract). An adapter may not always know an observation's *exact*
        class at the point it is built, so the hint is matched in three tiers, each
        strictly narrowing rather than guessing:

        1. An exact match against ``Instrument.instrument_class`` (for example
           ``'linear-perp'``).
        2. A match against ``Instrument.instrument_type`` (for example ``'perpetual'``),
           for an adapter that only knows the coarse product family at observation time.
        3. The literal hint ``'derivative'``, matching any instrument whose type is not
           spot, for an adapter that only knows the symbol is not a spot pair.

        With no hint, or with a hint that does not resolve to exactly one instrument, the
        method falls back to the symbol alone: it returns the instrument when the symbol
        maps to exactly one, and returns ``None`` when it maps to more than one. An
        observation that cannot say which product line it belongs to is safer treated as
        unmatched than silently attached to the wrong market's volume and price.
        """
        if instrument_class is not None:
            exact = self._by_venue_class_symbol.get((venue_slug, instrument_class, symbol_native))
            if exact is not None:
                return exact
            candidates = self._by_venue_symbol.get((venue_slug, symbol_native), [])
            narrowed = [c for c in candidates if c.instrument_type.value == instrument_class]
            if len(narrowed) == 1:
                return narrowed[0]
            if instrument_class == "derivative":
                non_spot = [c for c in candidates if c.instrument_type.value != "spot"]
                if len(non_spot) == 1:
                    return non_spot[0]

        all_candidates = self._by_venue_symbol.get((venue_slug, symbol_native))
        if not all_candidates:
            return None
        if len(all_candidates) == 1:
            return all_candidates[0]
        return None

    def has_collision(self, venue_slug: str, symbol_native: str) -> bool:
        """Return whether this venue symbol maps to more than one instrument class."""
        return (venue_slug, symbol_native) in self._collision_symbols

    def for_asset(self, asset_id: str) -> tuple[Instrument, ...]:
        """Return every instrument whose base asset is ``asset_id``."""
        return tuple(self._by_base_asset.get(asset_id, ()))

    def asset_ids(self) -> tuple[str, ...]:
        """Return every base asset that has at least one instrument, sorted."""
        return tuple(sorted(self._by_base_asset))

    def all(self) -> tuple[Instrument, ...]:
        """Return every indexed instrument, ordered by identifier."""
        return tuple(sorted(self._by_id.values(), key=lambda i: i.instrument_id))

    def venues_for_asset(self, asset_id: str) -> frozenset[str]:
        """Return the venues listing an instrument on ``asset_id``."""
        return frozenset(i.venue_slug for i in self._by_base_asset.get(asset_id, ()))

    def __len__(self) -> int:
        return len(self._by_id)

    def require(self, identifier: str) -> Instrument:
        """Return an instrument, raising if it is unknown.

        Raises:
            SchemaMismatch: If no such instrument exists in this generation.

        """
        instrument = self._by_id.get(identifier)
        if instrument is None:
            raise SchemaMismatch("unknown instrument", instrument_id=identifier)
        return instrument
