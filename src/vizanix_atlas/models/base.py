"""Shared Pydantic configuration for every Atlas model.

Atlas validates at exactly one boundary: raw venue JSON becomes a typed model in
the adapter, and everything downstream works with typed models. The settings here
make that boundary strict on purpose.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class AtlasModel(BaseModel):
    """Base class for Atlas schema models.

    Configuration choices and why:

    ``extra="forbid"``
        An unexpected field is a schema change, which Atlas wants to hear about.
        Silently accepting it would let venue drift pass unnoticed.
    ``frozen=True``
        Observations are facts about a moment. Making them immutable removes a
        class of bug where an analytics stage mutates a shared input.
    ``validate_assignment=True``
        Belt and braces for the frozen setting.
    ``str_strip_whitespace=True``
        Several venues pad symbol fields.
    ``use_enum_values=False``
        Enum members are kept as members internally; serialisation converts them.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        str_strip_whitespace=True,
        use_enum_values=False,
        populate_by_name=True,
        ser_json_timedelta="float",
    )


class MutableAtlasModel(BaseModel):
    """Base class for models that accumulate state while a run is in progress.

    Used for run-scoped aggregates such as adapter health counters, which are
    updated as requests complete. Published records always use :class:`AtlasModel`.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        validate_assignment=True,
        str_strip_whitespace=True,
    )
