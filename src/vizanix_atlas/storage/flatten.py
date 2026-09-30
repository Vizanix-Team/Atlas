"""Flattening nested Atlas models into tabular rows.

Atlas's schema is deliberately nested (``MarketState.liquidity.bands``, not sixty top
-level columns): the nesting is what keeps related fields grouped and documented as a
unit. Parquet output, on the other hand, wants flat rows. This module is the one place
that bridges the two, so the bridging rule is stated once rather than re-invented per
table.

The rule:

- A field that is itself one nested model is flattened inline, with its own field
  names prefixed by the parent field name and a double underscore
  (``reference_price__value``, ``reference_price__method``). This preserves the
  grouping in the column name without needing a struct-typed column.
- A field that is a sequence of nested models (``liquidity.bands``,
  ``liquidity.by_venue``) is **not** flattened into columns, because its length
  varies per row and a variable-width row is not a table. It is instead serialised as
  a single JSON column, so the detail is preserved and queryable with DuckDB's JSON
  functions without corrupting the row shape. See ``docs/DATA_MODEL.md``.
- An enum becomes its ``.value``. ``None`` stays ``None`` rather than becoming a
  sentinel, so "unknown" and "zero" never collapse into each other on the way to disk.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Final

from pydantic import BaseModel

#: Column-name separator between a parent field and its nested field.
_SEPARATOR: Final = "__"


def _json_default(value: Any) -> Any:
    """Render a value inside a flattened JSON column.

    Raises:
        TypeError: For a type genuinely unexpected in Atlas's schema, so an unhandled
            type surfaces as a loud failure rather than a silently wrong column.

    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"cannot serialise {type(value).__name__} into a flattened column")


def _scalar(value: Any) -> Any:
    """Reduce one leaf value to something a columnar writer accepts."""
    if isinstance(value, Enum):
        return value.value
    return value


def flatten_model(model: BaseModel, *, prefix: str = "") -> dict[str, Any]:
    """Flatten one model instance into a single-level dict of column values.

    Args:
        model: The model to flatten. Frozen Atlas models are read via
            ``__dict__`` rather than ``model_dump()``, so a nested model field stays a
            model instance for this function to recurse into, instead of already being
            a plain dict.
        prefix: Internal recursion state; callers omit it.

    Returns:
        A flat mapping from column name to value. Nested single models are inlined
        with prefixed names; nested sequences of models become one JSON-string column
        named the same as the field.

    """
    row: dict[str, Any] = {}
    for name, value in model:
        column = f"{prefix}{name}"
        if isinstance(value, BaseModel):
            row.update(flatten_model(value, prefix=f"{column}{_SEPARATOR}"))
        elif isinstance(value, tuple | list) and value and isinstance(value[0], BaseModel):
            row[column] = json.dumps(
                [v.model_dump(mode="json") for v in value], default=_json_default
            )
        elif isinstance(value, tuple | list) and not value:
            # An empty sequence of models is indistinguishable from an empty sequence
            # of anything else; represented as an empty JSON array either way.
            row[column] = "[]"
        elif isinstance(value, tuple | list):
            # A sequence of scalars (quote_currencies, intervals_observed_hours) is
            # small and fixed-meaning, so it is kept as a JSON array rather than a
            # nested table of its own.
            row[column] = json.dumps([_scalar(v) for v in value], default=_json_default)
        elif isinstance(value, dict):
            row[column] = json.dumps(value, default=_json_default)
        else:
            row[column] = _scalar(value)
    return row


def flatten_models(models: list[BaseModel]) -> list[dict[str, Any]]:
    """Flatten a list of models, in order.

    A convenience wrapper; the row order is preserved so that callers who already
    sorted their models for deterministic output do not need to sort again.
    """
    return [flatten_model(model) for model in models]
