"""Numeric parsing and rounding policy.

Venues serialise numbers as JSON strings, JSON numbers, empty strings, ``null``
and occasionally ``"NaN"``. Atlas funnels all of them through one place so that
the distinction between *absent* and *zero* survives (see ``docs/QUALITY.md``).

Policy, stated once and applied everywhere:

- Parsing returns ``None`` for anything not finitely numeric. It never returns 0
  as a fallback.
- Arithmetic is IEEE-754 ``float64``. Atlas publishes measurements derived from
  venue-reported figures that are themselves float-precision, so decimal
  arithmetic would imply a precision the inputs do not have.
- Rounding happens at presentation and serialisation boundaries only, never
  between calculation steps.
"""

from __future__ import annotations

import math
from typing import Any

# Values some venues use to mean "no value". Treated as absent rather than zero.
_EMPTY_SENTINELS = frozenset({"", "-", "null", "none", "nan", "n/a", "na", "None"})


def parse_float(value: Any) -> float | None:
    """Parse a venue-supplied scalar into a finite float, or ``None``.

    Returns ``None`` for ``None``, empty strings, venue "no value" sentinels,
    non-numeric text, and non-finite results such as infinity or NaN. Booleans are
    rejected because a boolean in a numeric field indicates a schema change rather
    than a value.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        candidate = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped in _EMPTY_SENTINELS or stripped.lower() in _EMPTY_SENTINELS:
            return None
        try:
            candidate = float(stripped)
        except ValueError:
            return None
    else:
        return None
    return candidate if math.isfinite(candidate) else None


def parse_int(value: Any) -> int | None:
    """Parse a venue-supplied scalar into an int, or ``None``.

    Accepts integral floats (``3.0``) because several venues serialise integer
    counts that way, and rejects fractional ones, which would indicate the field
    is not the count it was expected to be.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    parsed = parse_float(value)
    if parsed is None or not parsed.is_integer():
        return None
    return int(parsed)


def parse_positive(value: Any) -> float | None:
    """Parse a value that is only meaningful when strictly positive.

    Prices and tick sizes use this: a zero or negative price is not a cheap
    market, it is a bad record.
    """
    parsed = parse_float(value)
    return parsed if parsed is not None and parsed > 0.0 else None


def parse_non_negative(value: Any) -> float | None:
    """Parse a value that is meaningful at zero but not below it.

    Volumes, depths and open interest use this: zero volume is a real
    observation, negative volume is not.
    """
    parsed = parse_float(value)
    return parsed if parsed is not None and parsed >= 0.0 else None


def safe_divide(numerator: float | None, denominator: float | None) -> float | None:
    """Divide, returning ``None`` when the result would not be a finite number.

    Used instead of guarding every call site, so that a missing input propagates
    as missing rather than as zero.
    """
    if numerator is None or denominator is None or denominator == 0.0:
        return None
    result = numerator / denominator
    return result if math.isfinite(result) else None


def to_bps(ratio: float | None) -> float | None:
    """Convert a dimensionless ratio to basis points."""
    return None if ratio is None else ratio * 10_000.0


def relative_bps(value: float | None, reference: float | None) -> float | None:
    """Return ``(value - reference) / reference`` in basis points.

    The canonical form for every deviation metric Atlas publishes, so that basis,
    dispersion and impact all use one definition.
    """
    if value is None or reference is None or reference == 0.0:
        return None
    result = (value - reference) / reference * 10_000.0
    return result if math.isfinite(result) else None


def round_for_output(value: float | None, digits: int) -> float | None:
    """Round a value for serialisation.

    Only called when writing a record, never between calculation steps.
    """
    if value is None:
        return None
    rounded = round(value, digits)
    # Normalise -0.0, which is a valid float but reads as an error in output.
    return 0.0 if rounded == 0.0 else rounded


def infer_step_decimals(step: float | None) -> int | None:
    """Return the number of decimal places implied by a tick or step size."""
    if step is None or step <= 0:
        return None
    text = f"{step:.12f}".rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])
