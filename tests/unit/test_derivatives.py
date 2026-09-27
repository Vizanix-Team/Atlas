"""Derivative normalisation tests.

The Kraken Futures conversion tests use the venue's own published relative rates as the
expected values, so they verify Atlas against the exchange rather than against itself.
"""

from __future__ import annotations

import pytest

from vizanix_atlas.analytics.derivatives import (
    MarkObservation,
    aggregate_funding,
    aggregate_open_interest,
    compute_basis,
    normalise_funding,
    normalise_open_interest,
)
from vizanix_atlas.models.enums import (
    ContractType,
    ExclusionReason,
    FundingSemantics,
    InstrumentType,
    OpenInterestUnit,
    PriceSource,
)
from vizanix_atlas.models.instrument import Instrument
from vizanix_atlas.models.observations import ObservationTiming, RawDerivativeObservation

TIMING = ObservationTiming(collector_receive_time=1790530000000)


def perp(
    venue: str,
    *,
    interval: float | None = 8.0,
    semantics: FundingSemantics | None = FundingSemantics.RELATIVE_PER_INTERVAL,
    inverse: bool = False,
    multiplier: float | None = 1.0,
    oi_unit: OpenInterestUnit = OpenInterestUnit.CONTRACTS,
) -> Instrument:
    return Instrument(
        instrument_id=f"instrument:{venue}:perp:X",
        venue_slug=venue,
        instrument_type=InstrumentType.PERPETUAL,
        instrument_class="inverse-perp" if inverse else "linear-perp",
        symbol_native="X",
        symbol_normalized="X/USD",
        base_asset_id="asset:native:bitcoin:BTC",
        quote_asset_id="asset:fiat:usd",
        contract_type=ContractType.INVERSE if inverse else ContractType.LINEAR,
        contract_multiplier=multiplier,
        funding_interval_hours=interval,
        funding_semantics=semantics,
        open_interest_unit=oi_unit,
    )


def observation(venue: str, **kwargs) -> RawDerivativeObservation:
    return RawDerivativeObservation(venue_slug=venue, symbol_native="X", timing=TIMING, **kwargs)


# --------------------------------------------------------------------------- funding


@pytest.mark.parametrize(
    ("interval", "raw", "expected_8h"),
    [
        # An 8-hour venue needs no rescaling.
        (8.0, 0.0001, 0.0001),
        # An hourly venue's rate is eight times smaller for the same 8-hour cost.
        (1.0, 0.0000125, 0.0001),
        # OKX publishes 2-hour and 4-hour contracts alongside 8-hour ones.
        (4.0, 0.00005, 0.0001),
        (2.0, 0.000025, 0.0001),
        # Negative funding rescales the same way.
        (1.0, -0.0000125, -0.0001),
    ],
)
def test_funding_rescales_linearly_to_eight_hours(
    interval: float, raw: float, expected_8h: float
) -> None:
    result = normalise_funding(
        observation("v", funding_rate_raw=raw, funding_interval_hours=interval),
        perp("v", interval=interval),
        mark_price=100.0,
    )
    assert not isinstance(result, ExclusionReason)
    assert result.rate_8h == pytest.approx(expected_8h)
    assert result.interval_hours == interval
    # The raw value is preserved exactly as the venue published it.
    assert result.rate_raw == raw


def test_hyperliquid_baseline_funding_matches_the_classic_rate() -> None:
    """Hyperliquid's observed 0.0000125 per hour is exactly 0.01% per 8 hours.

    This is the arithmetic that corroborated the documented hourly interval.
    """
    result = normalise_funding(
        observation("hyperliquid", funding_rate_raw=0.0000125, funding_interval_hours=1.0),
        perp("hyperliquid", interval=1.0),
        mark_price=84411.0,
    )
    assert not isinstance(result, ExclusionReason)
    assert result.rate_8h == pytest.approx(0.0001)


def test_krakenfutures_linear_absolute_funding_matches_the_venues_own_relative_rate() -> None:
    """Verified against Kraken's /v4/historicalfundingrates for PF_XBTUSD.

    absolute 0.18037855027822186, mark 84420.34806550229
    venue's published relativeFundingRate: 2.136845833333e-06
    """
    result = normalise_funding(
        observation(
            "krakenfutures",
            funding_rate_raw=0.18037855027822186,
            funding_interval_hours=1.0,
        ),
        perp(
            "krakenfutures",
            interval=1.0,
            semantics=FundingSemantics.ABSOLUTE_PER_INTERVAL,
            inverse=False,
        ),
        mark_price=84420.34806550229,
    )
    assert not isinstance(result, ExclusionReason)
    hourly_relative = result.rate_8h / 8.0
    assert hourly_relative == pytest.approx(2.136845833333e-06, rel=1e-3)
    assert result.conversion_note == "derived_from_absolute:raw/mark"


def test_krakenfutures_inverse_absolute_funding_matches_the_venues_own_relative_rate() -> None:
    """Verified against Kraken's /v4/historicalfundingrates for PI_XBTUSD.

    absolute 2.40324183e-10, mark 84436.41626261781
    venue's published relativeFundingRate: 2.0286595833333e-05

    Note the conversion is multiplication here, not division: an inverse contract's
    funding is denominated in the base asset per unit of quote notional.
    """
    result = normalise_funding(
        observation(
            "krakenfutures", funding_rate_raw=2.40324183e-10, funding_interval_hours=1.0
        ),
        perp(
            "krakenfutures",
            interval=1.0,
            semantics=FundingSemantics.ABSOLUTE_PER_INTERVAL,
            inverse=True,
        ),
        mark_price=84436.41626261781,
    )
    assert not isinstance(result, ExclusionReason)
    hourly_relative = result.rate_8h / 8.0
    assert hourly_relative == pytest.approx(2.0286595833333e-05, rel=1e-3)
    assert result.conversion_note == "derived_from_absolute:raw*mark"


def test_absolute_funding_without_a_mark_price_is_refused() -> None:
    """The conversion needs the mark price; without it Atlas will not guess."""
    result = normalise_funding(
        observation("krakenfutures", funding_rate_raw=0.18, funding_interval_hours=1.0),
        perp(
            "krakenfutures", interval=1.0, semantics=FundingSemantics.ABSOLUTE_PER_INTERVAL
        ),
        mark_price=None,
    )
    assert result is ExclusionReason.UNDOCUMENTED_FUNDING_SEMANTICS


def test_undocumented_funding_semantics_are_excluded_not_assumed() -> None:
    result = normalise_funding(
        observation("v", funding_rate_raw=0.0001),
        perp("v", semantics=FundingSemantics.UNDOCUMENTED),
        mark_price=100.0,
    )
    assert result is ExclusionReason.UNDOCUMENTED_FUNDING_SEMANTICS


def test_missing_interval_is_excluded_rather_than_defaulted_to_eight_hours() -> None:
    """Assuming 8 hours would misstate an hourly venue's funding by eight times."""
    result = normalise_funding(
        observation("v", funding_rate_raw=0.0001),
        perp("v", interval=None),
        mark_price=100.0,
    )
    assert result is ExclusionReason.UNDOCUMENTED_FUNDING_SEMANTICS


def test_funding_aggregate_uses_the_median_and_records_intervals() -> None:
    observations = [
        normalise_funding(
            observation(v, funding_rate_raw=r, funding_interval_hours=i), perp(v, interval=i),
            mark_price=100.0,
        )
        for v, r, i in [
            ("a", 0.0001, 8.0),
            ("b", 0.0000125, 1.0),
            ("c", 0.00005, 4.0),
            ("rogue", 0.5, 8.0),  # an implausible rate must not dominate
        ]
    ]
    usable = [o for o in observations if not isinstance(o, ExclusionReason)]
    state = aggregate_funding(usable, excluded=1)

    assert state.venue_count == 4
    assert state.excluded_venue_count == 1
    # The median is unmoved by the rogue venue; a mean would be about 0.125.
    assert state.rate_8h_median == pytest.approx(0.0001)
    assert state.rate_8h_max == pytest.approx(0.5)
    # Every native interval that contributed is published.
    assert state.intervals_observed_hours == (1.0, 4.0, 8.0)
    # Annualisation is simple, not compounded: three 8-hour periods a day.
    assert state.annualised_simple == pytest.approx(0.0001 * 3 * 365)


def test_no_funding_observations_gives_nulls_not_zeros() -> None:
    state = aggregate_funding([], excluded=3)
    assert state.rate_8h_median is None
    assert state.annualised_simple is None
    assert state.venue_count == 0
    assert state.excluded_venue_count == 3


# ---------------------------------------------------------------- open interest


def test_contracts_convert_through_the_multiplier_for_a_linear_contract() -> None:
    result = normalise_open_interest(
        observation("v", open_interest_raw=1_000_000.0),
        perp("v", multiplier=0.01, oi_unit=OpenInterestUnit.CONTRACTS),
        reference_price=84_400.0,
        quote_to_usd=1.0,
    )
    assert result is not None and result.excluded_reason is None
    # 1,000,000 contracts * 0.01 BTC = 10,000 BTC.
    assert result.base_equivalent == pytest.approx(10_000.0)
    assert result.usd_equivalent == pytest.approx(10_000.0 * 84_400.0)


def test_inverse_contracts_convert_without_the_price() -> None:
    """An inverse contract worth 100 USD is worth 100 USD at any price."""
    result = normalise_open_interest(
        observation("v", open_interest_raw=5_531_440.0),
        perp("v", inverse=True, multiplier=100.0, oi_unit=OpenInterestUnit.CONTRACTS),
        reference_price=84_400.0,
        quote_to_usd=1.0,
    )
    assert result is not None and result.excluded_reason is None
    assert result.usd_equivalent == pytest.approx(5_531_440.0 * 100.0)
    assert result.base_equivalent == pytest.approx(5_531_440.0 * 100.0 / 84_400.0)


def test_missing_multiplier_excludes_the_observation_and_keeps_the_raw_value() -> None:
    result = normalise_open_interest(
        observation("v", open_interest_raw=1234.0),
        perp("v", multiplier=None, oi_unit=OpenInterestUnit.CONTRACTS),
        reference_price=84_400.0,
        quote_to_usd=1.0,
    )
    assert result is not None
    assert result.excluded_reason is ExclusionReason.UNKNOWN_CONTRACT_MULTIPLIER
    assert result.usd_equivalent is None
    # The raw value survives, so the gap is attributable rather than invisible.
    assert result.raw == 1234.0


def test_unknown_unit_excludes_rather_than_guessing() -> None:
    result = normalise_open_interest(
        observation("v", open_interest_raw=99.0),
        perp("v", oi_unit=OpenInterestUnit.UNKNOWN),
        reference_price=84_400.0,
        quote_to_usd=1.0,
    )
    assert result is not None
    assert result.excluded_reason is ExclusionReason.SCHEMA_FAILURE


def test_venue_published_figures_are_used_directly() -> None:
    """A venue knows its own contract arithmetic better than Atlas can reconstruct it."""
    result = normalise_open_interest(
        observation(
            "okx",
            open_interest_raw=5_531_440.0,
            open_interest_base=6_551.43,
            open_interest_usd=553_144_089.0,
        ),
        perp("okx", inverse=True, multiplier=100.0),
        reference_price=84_400.0,
        quote_to_usd=1.0,
    )
    assert result is not None and result.excluded_reason is None
    assert result.base_equivalent == pytest.approx(6_551.43)
    assert result.usd_equivalent == pytest.approx(553_144_089.0)


def test_base_asset_unit_needs_no_multiplier() -> None:
    """Bitget's holdingAmount is already in base units."""
    result = normalise_open_interest(
        observation("bitget", open_interest_raw=31_357.8),
        perp("bitget", multiplier=None, oi_unit=OpenInterestUnit.BASE_ASSET),
        reference_price=84_400.0,
        quote_to_usd=1.0,
    )
    assert result is not None and result.excluded_reason is None
    assert result.base_equivalent == pytest.approx(31_357.8)
    assert result.usd_equivalent == pytest.approx(31_357.8 * 84_400.0)


def test_no_raw_value_yields_nothing_at_all() -> None:
    assert normalise_open_interest(
        observation("v"), perp("v"), reference_price=84_400.0, quote_to_usd=1.0
    ) is None


def test_open_interest_aggregate_counts_exclusions() -> None:
    usable = normalise_open_interest(
        observation("a", open_interest_raw=100.0),
        perp("a", multiplier=1.0, oi_unit=OpenInterestUnit.BASE_ASSET),
        reference_price=100.0,
        quote_to_usd=1.0,
    )
    unusable = normalise_open_interest(
        observation("b", open_interest_raw=100.0),
        perp("b", multiplier=None, oi_unit=OpenInterestUnit.CONTRACTS),
        reference_price=100.0,
        quote_to_usd=1.0,
    )
    assert usable is not None and unusable is not None
    state = aggregate_open_interest([usable, unusable])

    assert state.total_usd == pytest.approx(10_000.0)
    assert state.instrument_count == 1
    assert state.excluded_instrument_count == 1
    assert state.perp_usd == pytest.approx(10_000.0)
    assert state.futures_usd is None


# ------------------------------------------------------------------------ basis


def test_basis_is_computed_from_mark_prices_only() -> None:
    marks = [
        MarkObservation(venue_slug="a", instrument=perp("a"), mark_price_usd=84_500.0),
        MarkObservation(venue_slug="b", instrument=perp("b"), mark_price_usd=84_450.0),
    ]
    state = compute_basis(marks, reference_price=84_400.0)

    assert state.price_source is PriceSource.MARK
    assert state.venue_count == 2
    # (84500-84400)/84400 = 11.85 bps; (84450-84400)/84400 = 5.92 bps; median 8.88.
    assert state.perp_basis_bps_median == pytest.approx(8.88, abs=0.05)
    assert state.perp_basis_bps_min == pytest.approx(5.92, abs=0.05)
    assert state.perp_basis_bps_max == pytest.approx(11.85, abs=0.05)
    # No dated futures contributed, so that basis is null rather than zero.
    assert state.futures_basis_bps_median is None


def test_no_reference_price_means_no_basis() -> None:
    marks = [MarkObservation(venue_slug="a", instrument=perp("a"), mark_price_usd=84_500.0)]
    state = compute_basis(marks, reference_price=None)
    assert state.perp_basis_bps_median is None
    assert state.venue_count == 0
