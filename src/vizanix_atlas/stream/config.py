"""Configuration for the live layer, loaded from ``config/stream.yaml``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field

from vizanix_atlas.core.config import _load_yaml, config_dir
from vizanix_atlas.core.errors import ConfigurationError
from vizanix_atlas.models.base import AtlasModel


class UniverseSettings(AtlasModel):
    """Which assets the live layer follows."""

    assets: int = Field(gt=0)
    min_venue_count: int = Field(ge=1)
    include_probable: bool
    refresh_seconds: float = Field(gt=0)


class ReferenceSettings(AtlasModel):
    """How a live reference price is formed from the latest venue quotes."""

    stale_after_seconds: float = Field(gt=0)
    max_deviation_bps: float = Field(gt=0)
    min_venues_for_rejection: int = Field(ge=2)
    disconnect_grace_seconds: float = Field(ge=0)


class EmitSettings(AtlasModel):
    """How often changes are announced to clients."""

    interval_ms: int = Field(gt=0)
    min_change_bps: float = Field(ge=0)
    heartbeat_seconds: float = Field(gt=0)


class ServerSettings(AtlasModel):
    """The HTTP surface."""

    host: str
    port: int = Field(gt=0, lt=65536)
    max_clients: int = Field(gt=0)
    keepalive_seconds: float = Field(gt=0)
    cors_origin: str


class StreamConfig(AtlasModel):
    """The whole of ``config/stream.yaml``."""

    schema_version: str
    universe: UniverseSettings
    reference: ReferenceSettings
    emit: EmitSettings
    server: ServerSettings
    feeds: tuple[str, ...]


@lru_cache(maxsize=4)
def load_stream_config(path: Path | None = None) -> StreamConfig:
    """Load and validate ``config/stream.yaml``.

    Raises:
        ConfigurationError: If the document is malformed.

    """
    resolved = path or (config_dir() / "stream.yaml")
    document = _load_yaml(resolved)
    try:
        return StreamConfig(**document)
    except Exception as exc:
        raise ConfigurationError(
            "invalid stream configuration", path=str(resolved), detail=str(exc)
        ) from exc
