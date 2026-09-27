"""Numeric policy tests.

The central property under test is that *absent* never becomes *zero*.
"""

from __future__ import annotations

import math

import pytest

from vizanix_atlas.core.numeric import (
    infer_step_decimals,
    parse_float,
    parse_int,
    parse_non_negative,
    parse_positive,
    relative_bps,
    round_for_output,
    safe_divide,
    to_bps,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.5", 1.5),
        (1.5, 1.5),
        (2, 2.0),
        ("  3.25  ", 3.25),
        ("-4", -4.0),
        ("1e3", 1000.0),
        # Every one of these means "no value" on some venue, and none of them mean 0.
        (None, None),
        ("", None),
        ("   ", None),
        ("-", None),
        ("null", None),
        ("None", None),
        ("NaN", None),
        ("n/a", None),
        ("abc", None),
        # Non-finite results are not values.
        (float("inf"), None),
        (float("-inf"), None),
        (float("nan"), None),
        # A boolean in a numeric field is a schema change, not a 1.
        (True, None),
        (False, None),
        ([1], None),
        ({"a": 1}, None),
    ],
)
def test_parse_float_never_substitutes_zero(value: object, expected: float | None) -> None:
    assert parse_float(value) == expected


def test_parse_positive_rejects_zero_and_negative() -> None:
    """A zero price is a bad record, not a cheap market."""
    assert parse_positive("0") is None
    assert parse_positive("-1") is None
    assert parse_positive("0.00000001") == pytest.approx(1e-8)


def test_parse_non_negative_accepts_zero() -> None:
    """Zero volume is a real observation; negative volume is not."""
    assert parse_non_negative("0") == 0.0
    assert parse_non_negative("-0.5") is None
    assert parse_non_negative("12.5") == 12.5


def test_parse_int_accepts_integral_floats_only() -> None:
    assert parse_int("3") == 3
    assert parse_int(3.0) == 3
    assert parse_int("3.0") == 3
    # A fractional value in a count field means the field is not a count.
    assert parse_int("3.5") is None
    assert parse_int(True) is None
    assert parse_int(None) is None


def test_safe_divide_propagates_absence() -> None:
    assert safe_divide(10.0, 4.0) == 2.5
    assert safe_divide(10.0, 0.0) is None
    assert safe_divide(None, 4.0) is None
    assert safe_divide(10.0, None) is None


def test_relative_bps_is_the_single_deviation_definition() -> None:
    assert relative_bps(101.0, 100.0) == pytest.approx(100.0)
    assert relative_bps(99.0, 100.0) == pytest.approx(-100.0)
    assert relative_bps(100.0, 100.0) == 0.0
    assert relative_bps(100.0, 0.0) is None
    assert relative_bps(None, 100.0) is None
    assert to_bps(0.01) == pytest.approx(100.0)
    assert to_bps(None) is None


def test_round_for_output_normalises_negative_zero() -> None:
    """A serialised -0.0 reads as an error; it is normalised to 0.0."""
    result = round_for_output(-0.0000001, 4)
    assert result == 0.0
    assert not math.copysign(1.0, result) < 0
    assert round_for_output(None, 4) is None
    assert round_for_output(1.23456, 2) == 1.23


def test_infer_step_decimals() -> None:
    assert infer_step_decimals(0.01) == 2
    assert infer_step_decimals(1.0) == 0
    assert infer_step_decimals(1e-8) == 8
    assert infer_step_decimals(0.0) is None
    assert infer_step_decimals(None) is None
