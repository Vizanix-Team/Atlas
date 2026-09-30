"""Vizanix Atlas: the semantic layer for global crypto markets.

``from vizanix_atlas import Atlas`` is the public entry point. It is imported lazily so
that ``import vizanix_atlas`` (which every submodule triggers) stays cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vizanix_atlas.core.versions import SOFTWARE_VERSION

if TYPE_CHECKING:
    from vizanix_atlas.sdk.client import Atlas

__version__ = SOFTWARE_VERSION
__all__ = ["Atlas", "__version__"]


def __getattr__(name: str) -> object:
    if name == "Atlas":
        from vizanix_atlas.sdk.client import Atlas

        return Atlas
    raise AttributeError(f"module 'vizanix_atlas' has no attribute {name!r}")
