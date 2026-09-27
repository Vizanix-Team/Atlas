"""Configuration loading and validation.

Configuration is declarative YAML in ``config/``, validated into typed models here
so that a malformed policy fails at startup rather than midway through a
collection run. Every loader is cached, because a collection run reads the same
configuration from many places.

Configuration is located in this order:

1. An explicit path passed by the caller.
2. ``ATLAS_CONFIG_DIR``, for operators who keep policy outside the repository.
3. ``config/`` relative to the repository root, for a development checkout.
4. The copy bundled into the installed wheel, so that ``pip install
   vizanix-atlas`` works without a checkout.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator, model_validator

from vizanix_atlas.core.errors import ConfigurationError
from vizanix_atlas.core.identifiers import venue_id
from vizanix_atlas.models.base import AtlasModel
from vizanix_atlas.models.enums import LiquidationCapability, PriceSource, ResolutionState
from vizanix_atlas.models.venue import RateLimitPolicy, Venue, VenueCapabilities


def _repo_config_dir() -> Path:
    """Return ``config/`` in a development checkout, if one is above this file."""
    # src/vizanix_atlas/core/config.py -> repository root is four levels up.
    return Path(__file__).resolve().parents[3] / "config"


def _bundled_config_dir() -> Path:
    """Return the configuration copied into the installed wheel."""
    return Path(__file__).resolve().parent.parent / "_bundled_config"


def config_dir(explicit: Path | None = None) -> Path:
    """Resolve the configuration directory.

    Raises:
        ConfigurationError: If no candidate directory exists. The message lists
            every location tried, because a missing config directory is otherwise a
            confusing failure.
    """
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    from_env = os.environ.get("ATLAS_CONFIG_DIR")
    if from_env:
        candidates.append(Path(from_env))
    candidates.extend((_repo_config_dir(), _bundled_config_dir()))

    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise ConfigurationError(
        "could not locate the Atlas configuration directory",
        tried=[str(c) for c in candidates],
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read and parse one YAML document.

    Raises:
        ConfigurationError: If the file is missing, unparseable, or not a mapping.
    """
    if not path.is_file():
        raise ConfigurationError("configuration file not found", path=str(path))
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError("configuration file is not valid YAML", path=str(path)) from exc
    if not isinstance(loaded, dict):
        raise ConfigurationError("configuration file must contain a mapping", path=str(path))
    return loaded


# --------------------------------------------------------------------------- #
# Exchange registry
# --------------------------------------------------------------------------- #


class ExchangeRegistry(AtlasModel):
    """Every declared venue, indexed by slug."""

    schema_version: str
    venues: tuple[Venue, ...]

    def get(self, slug: str) -> Venue:
        """Return the venue declared under ``slug``.

        Raises:
            ConfigurationError: If no such venue is declared.
        """
        for venue in self.venues:
            if venue.slug == slug:
                return venue
        raise ConfigurationError(
            "unknown venue", slug=slug, known=[v.slug for v in self.venues]
        )

    def enabled(self) -> tuple[Venue, ...]:
        """Return enabled venues, ordered by priority band then slug.

        Core venues come first so that a run cut short by a job timeout still
        produced the broadest coverage it could.
        """
        order = {"core": 0, "standard": 1, "extended": 2}
        return tuple(
            sorted(
                (v for v in self.venues if v.enabled),
                key=lambda v: (order.get(v.priority, 9), v.slug),
            )
        )

    def disabled(self) -> tuple[Venue, ...]:
        """Return disabled venues, each of which carries a reason."""
        return tuple(sorted((v for v in self.venues if not v.enabled), key=lambda v: v.slug))

    def slugs(self) -> tuple[str, ...]:
        """Return every declared venue slug, sorted."""
        return tuple(sorted(v.slug for v in self.venues))


def _build_venue(raw: dict[str, Any], defaults: dict[str, Any]) -> Venue:
    """Turn one registry entry into a :class:`Venue`.

    Rate-limit settings are layered over the registry defaults so that an entry
    need only state where it differs.
    """
    slug = raw.get("id")
    if not isinstance(slug, str) or not slug:
        raise ConfigurationError("every exchange entry requires a string id", entry=raw)

    rate_limit_raw = {**defaults.get("rate_limit", {}), **(raw.get("rate_limit") or {})}
    capabilities_raw = dict(raw.get("capabilities") or {})

    liquidations = capabilities_raw.pop("liquidations", None)
    if liquidations is not None:
        try:
            capabilities_raw["liquidations"] = LiquidationCapability(liquidations)
        except ValueError as exc:
            raise ConfigurationError(
                "unknown liquidations capability", slug=slug, value=liquidations
            ) from exc

    try:
        capabilities = VenueCapabilities(**capabilities_raw)
        rate_limit = RateLimitPolicy(**rate_limit_raw)
    except Exception as exc:  # noqa: BLE001 - re-raised as a configuration error below
        raise ConfigurationError("invalid venue configuration", slug=slug, detail=str(exc)) from exc

    return Venue(
        venue_id=venue_id(slug),
        slug=slug,
        display_name=raw.get("display_name") or slug,
        enabled=bool(raw.get("enabled", True)),
        disabled_reason=_collapse(raw.get("disabled_reason")),
        capabilities=capabilities,
        rate_limit=rate_limit,
        documentation_url=raw.get("documentation_url"),
        terms_url=raw.get("terms_url"),
        terms_reviewed_on=_stringify_date(raw.get("terms_reviewed_on")),
        redistributes_raw_observations=bool(raw.get("redistributes_raw_observations", False)),
        attribution=raw.get("attribution"),
        priority=raw.get("priority", "standard"),
    )


def _collapse(text: object) -> str | None:
    """Collapse a YAML folded block into a single line of prose."""
    if text is None:
        return None
    return " ".join(str(text).split()) or None


def _stringify_date(value: object) -> str | None:
    """Render a YAML date or string as an ISO date string.

    PyYAML parses an unquoted ``2026-09-27`` into a ``date``, so both forms have to
    be accepted.
    """
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


@lru_cache(maxsize=4)
def load_exchange_registry(path: Path | None = None) -> ExchangeRegistry:
    """Load and validate ``config/exchanges.yaml``.

    Raises:
        ConfigurationError: If the document is malformed, a slug is duplicated, or
            an enabled venue declares no way to collect in bulk.
    """
    resolved = path or (config_dir() / "exchanges.yaml")
    document = _load_yaml(resolved)
    entries = document.get("exchanges")
    if not isinstance(entries, list) or not entries:
        raise ConfigurationError("exchanges.yaml must define a non-empty exchanges list")

    defaults = document.get("defaults") or {}
    venues = [_build_venue(entry, defaults) for entry in entries]

    seen: set[str] = set()
    for venue in venues:
        if venue.slug in seen:
            raise ConfigurationError("duplicate venue id in exchanges.yaml", slug=venue.slug)
        seen.add(venue.slug)
        # Tier A depends on bulk collection. An enabled venue that cannot do it
        # would silently degrade to one request per symbol.
        if venue.enabled and not venue.capabilities.bulk_tickers:
            raise ConfigurationError(
                "an enabled venue must support bulk ticker retrieval",
                slug=venue.slug,
            )

    return ExchangeRegistry(
        schema_version=str(document.get("schema_version", "1.0.0")),
        venues=tuple(venues),
    )


# --------------------------------------------------------------------------- #
# Collection policy
# --------------------------------------------------------------------------- #


class ScheduleConfig(AtlasModel):
    """When snapshots are attempted."""

    cron_minutes: tuple[int, ...]
    slot_minutes: int = Field(gt=0, le=60)
    max_acceptable_start_delay_seconds: int = Field(ge=0)

    @field_validator("cron_minutes")
    @classmethod
    def _check_minutes(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        """Require valid, sorted, distinct minutes, and discourage minute zero.

        Minute 0 is where GitHub's scheduled-workflow queue is busiest, so a run
        placed there is the most likely to start late.
        """
        if not value:
            raise ValueError("cron_minutes must not be empty")
        if any(m < 0 or m > 59 for m in value):
            raise ValueError("cron_minutes must all be between 0 and 59")
        if len(set(value)) != len(value):
            raise ValueError("cron_minutes must be distinct")
        return tuple(sorted(value))


class TierSelection(AtlasModel):
    """Deterministic selection rules for a collection tier."""

    max_instruments_total: int = Field(default=0, ge=0)
    max_instruments_per_venue: int = Field(default=0, ge=0)
    min_reported_volume_24h_usd: float = Field(default=0.0, ge=0)
    min_venue_count: int = Field(default=0, ge=0)
    max_books_per_asset: int = Field(default=0, ge=0)
    always_include_assets: tuple[str, ...] = ()


class TierAConfig(AtlasModel):
    """Universal coverage settings."""

    description: str
    requires_bulk_endpoints: bool = True
    max_requests_per_venue: int = Field(gt=0)


class TierBConfig(AtlasModel):
    """Order-book sampling settings."""

    description: str
    order_book_depth_levels: int = Field(gt=0, le=5000)
    depth_bands_bps: tuple[int, ...]
    impact_notionals_usd: tuple[float, ...]
    selection: TierSelection

    @field_validator("depth_bands_bps")
    @classmethod
    def _sorted_bands(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        """Require at least one positive, ascending band."""
        if not value or any(b <= 0 for b in value):
            raise ValueError("depth_bands_bps must contain positive values")
        return tuple(sorted(value))


class OptionsConfig(AtlasModel):
    """Option-surface collection settings."""

    enabled: bool = False
    max_currencies: int = Field(default=0, ge=0)


class TierCConfig(AtlasModel):
    """Advanced analytics settings."""

    description: str
    enabled: bool = True
    order_book_depth_levels: int = Field(gt=0, le=5000)
    selection: TierSelection
    options: OptionsConfig = OptionsConfig()


class TiersConfig(AtlasModel):
    """The three collection tiers."""

    a_universal: TierAConfig
    b_liquidity: TierBConfig
    c_advanced: TierCConfig


class QualityConfig(AtlasModel):
    """Thresholds the quality gate applies to observations."""

    max_observation_age_seconds: float = Field(gt=0)
    max_deviation_bps: float = Field(gt=0)
    min_venues_for_dispersion: int = Field(ge=1)
    max_stablecoin_deviation_bps: float = Field(gt=0)


class ReferencePriceConfig(AtlasModel):
    """Reference-price aggregation parameters."""

    method: str
    max_venue_weight: float = Field(gt=0.0, le=1.0)
    volume_weight_exponent: float = Field(ge=0.0, le=1.0)
    min_freshness_weight: float = Field(gt=0.0, le=1.0)
    allow_derivative_fallback: bool = True
    derivative_fallback_price_source: PriceSource = PriceSource.INDEX

    @model_validator(mode="after")
    def _check_cap_is_useful(self) -> ReferencePriceConfig:
        """Require a cap that can actually bind.

        A cap of 1.0 is no cap at all, which would let one venue's reported volume
        determine the price. Caught here rather than being discovered in a number.
        """
        if self.max_venue_weight >= 1.0:
            raise ValueError("max_venue_weight must be below 1.0 to constrain any venue")
        return self


class UniverseConfig(AtlasModel):
    """One deterministic asset universe."""

    criteria: str
    min_venue_count: int = Field(default=0, ge=0)
    min_reported_volume_24h_usd: float = Field(default=0.0, ge=0)
    rank_by: str | None = None
    limit: int | None = Field(default=None, gt=0)


class GenomeConfig(AtlasModel):
    """Market Genome warm-up thresholds."""

    minimum_observations: int = Field(gt=0)
    warming_up_threshold: int = Field(gt=0)
    feature_version: str

    @model_validator(mode="after")
    def _check_thresholds(self) -> GenomeConfig:
        """Require the warm-up threshold to sit below the availability threshold."""
        if self.warming_up_threshold >= self.minimum_observations:
            raise ValueError("warming_up_threshold must be below minimum_observations")
        return self


class HistoryConfig(AtlasModel):
    """Minimum history required before a windowed metric is published."""

    minimum_observations: dict[str, int]
    max_window_missingness: float = Field(ge=0.0, lt=1.0)
    genome: GenomeConfig

    def required_for(self, metric: str) -> int:
        """Return the observation count ``metric`` needs, or 0 when unconstrained."""
        return self.minimum_observations.get(metric, 0)


class AnomalyGuards(AtlasModel):
    """Sanity checks a candidate generation must pass to become ``latest``."""

    max_instrument_count_drop_ratio: float = Field(gt=0.0, le=1.0)
    max_asset_count_drop_ratio: float = Field(gt=0.0, le=1.0)
    max_generation_bytes_change_ratio: float = Field(gt=1.0)
    max_adapter_parse_failure_ratio: float = Field(ge=0.0, le=1.0)
    min_successful_venues: int = Field(ge=1)
    min_successful_venue_ratio: float = Field(gt=0.0, le=1.0)


class PublishingConfig(AtlasModel):
    """Physical layout and hard size budgets for a published generation."""

    shard_count: int = Field(gt=0, le=1024)
    max_single_asset_bytes: int = Field(gt=0)
    max_generation_bytes: int = Field(gt=0)
    max_snapshot_rows: int = Field(gt=0)
    max_release_assets: int = Field(gt=0)
    compression: str = "zstd"
    compression_level: int = Field(default=9, ge=1, le=22)
    parquet_row_group_size: int = Field(default=65536, gt=0)
    anomaly_guards: AnomalyGuards


class CollectionConfig(AtlasModel):
    """The whole of ``config/collection.yaml``."""

    schema_version: str
    schedule: ScheduleConfig
    tiers: TiersConfig
    quality: QualityConfig
    reference_price: ReferencePriceConfig
    universes: dict[str, UniverseConfig]
    history: HistoryConfig
    publishing: PublishingConfig


@lru_cache(maxsize=4)
def load_collection_config(path: Path | None = None) -> CollectionConfig:
    """Load and validate ``config/collection.yaml``.

    Raises:
        ConfigurationError: If the document is malformed or internally inconsistent.
    """
    resolved = path or (config_dir() / "collection.yaml")
    document = _load_yaml(resolved)
    try:
        return CollectionConfig(**document)
    except Exception as exc:  # noqa: BLE001 - re-raised as a configuration error
        raise ConfigurationError(
            "invalid collection configuration", path=str(resolved), detail=str(exc)
        ) from exc


# --------------------------------------------------------------------------- #
# Retention policy
# --------------------------------------------------------------------------- #


class RetentionWindows(AtlasModel):
    """How long each class of data is kept."""

    raw_snapshot_days: int = Field(ge=0)
    daily_market_state_days: int = Field(ge=0)
    daily_venue_observations_days: int = Field(ge=0)
    daily_liquidity_days: int = Field(ge=0)
    quality_events_days: int = Field(ge=0)
    long_term_rollups: bool = True


class RollupRule(AtlasModel):
    """One rollup stage."""

    enabled: bool = True
    retain_days: int | None = Field(default=None, ge=0)
    aggregates: tuple[str, ...] = ()


class GenerationRetention(AtlasModel):
    """How many current-state generations stay downloadable."""

    keep_recent_generations: int = Field(ge=1)


class HousekeepingConfig(AtlasModel):
    """Guard rails every destructive cleanup must satisfy."""

    dry_run_default: bool = True
    require_tag_prefix: tuple[str, ...]
    never_delete_tag_prefix: tuple[str, ...]
    min_age_hours_before_delete: int = Field(ge=0)
    require_compacted_replacement: bool = True
    require_checksum_verification: bool = True
    max_deletions_per_run: int = Field(gt=0)
    log_every_deletion: bool = True

    @model_validator(mode="after")
    def _check_prefixes(self) -> HousekeepingConfig:
        """Require a non-empty allow list with no overlap against the deny list.

        An empty allow list would permit wildcard deletion across every release in
        the repository, including software releases.
        """
        if not self.require_tag_prefix:
            raise ValueError("require_tag_prefix must not be empty")
        for allowed in self.require_tag_prefix:
            for denied in self.never_delete_tag_prefix:
                if allowed.startswith(denied) or denied.startswith(allowed):
                    raise ValueError(
                        f"tag prefix {allowed!r} overlaps protected prefix {denied!r}"
                    )
        return self


class RetentionConfig(AtlasModel):
    """The whole of ``config/retention.yaml``."""

    schema_version: str
    retention: RetentionWindows
    rollups: dict[str, RollupRule]
    generations: GenerationRetention
    housekeeping: HousekeepingConfig


@lru_cache(maxsize=4)
def load_retention_config(path: Path | None = None) -> RetentionConfig:
    """Load and validate ``config/retention.yaml``.

    Raises:
        ConfigurationError: If the document is malformed or its guard rails are unsafe.
    """
    resolved = path or (config_dir() / "retention.yaml")
    document = _load_yaml(resolved)
    try:
        return RetentionConfig(**document)
    except Exception as exc:  # noqa: BLE001 - re-raised as a configuration error
        raise ConfigurationError(
            "invalid retention configuration", path=str(resolved), detail=str(exc)
        ) from exc


# --------------------------------------------------------------------------- #
# Identity overrides
# --------------------------------------------------------------------------- #

_PROVENANCE_FIELDS = ("reason", "source", "date", "author")


class OverrideProvenance(AtlasModel):
    """Who decided a manual mapping, when, and on what basis.

    Required on every override. A mapping nobody can justify is a mapping nobody
    can review, which is how an identity layer quietly becomes wrong.
    """

    reason: str = Field(min_length=10)
    source: str = Field(min_length=3)
    date: str
    author: str = Field(min_length=2)


class CanonicalAssetOverride(AtlasModel):
    """A canonical asset asserted directly in configuration."""

    asset_id: str
    symbol: str
    name: str | None = None
    chain_slug: str | None = None
    chain_id: int | None = None
    contract_address: str | None = None
    is_stablecoin: bool = False
    is_fiat: bool = False
    tracks_asset_id: str | None = None
    provenance: OverrideProvenance


class AliasOverride(AtlasModel):
    """A venue symbol mapped to a canonical asset by hand."""

    venue_slug: str
    venue_symbol: str
    asset_id: str
    provenance: OverrideProvenance


class NeverMergeRule(AtlasModel):
    """A ticker that must never be merged across venues on symbol alone."""

    symbol: str
    provenance: OverrideProvenance


class RelationshipOverride(AtlasModel):
    """An asset-graph edge asserted in configuration."""

    from_asset_id: str
    to_asset_id: str
    relationship: str
    provenance: OverrideProvenance


class OverrideConfig(AtlasModel):
    """The whole of ``config/asset_overrides.yaml``."""

    schema_version: str
    canonical_assets: tuple[CanonicalAssetOverride, ...] = ()
    aliases: tuple[AliasOverride, ...] = ()
    never_merge: tuple[NeverMergeRule, ...] = ()
    relationships: tuple[RelationshipOverride, ...] = ()

    def never_merge_symbols(self) -> frozenset[str]:
        """Return the upper-cased tickers that must stay venue-scoped."""
        return frozenset(rule.symbol.upper() for rule in self.never_merge)

    def alias_for(self, venue_slug: str, venue_symbol: str) -> str | None:
        """Return the overridden asset ID for a venue symbol, if one is declared."""
        target = venue_symbol.upper()
        for alias in self.aliases:
            if alias.venue_slug == venue_slug and alias.venue_symbol.upper() == target:
                return alias.asset_id
        return None

    def resolution_state_for(self, asset_id: str) -> ResolutionState:
        """Return the state an overridden asset should carry."""
        declared = {a.asset_id for a in self.canonical_assets}
        overridden = {a.asset_id for a in self.aliases}
        if asset_id in overridden:
            return ResolutionState.MANUAL_OVERRIDE
        return ResolutionState.RESOLVED if asset_id in declared else ResolutionState.PROBABLE


def _split_provenance(raw: dict[str, Any], *, context: str) -> dict[str, Any]:
    """Lift the four provenance fields out of a raw override entry.

    Raises:
        ConfigurationError: If any provenance field is missing. Enforced at load
            time so an unexplained override cannot be merged.
    """
    missing = [field for field in _PROVENANCE_FIELDS if not raw.get(field)]
    if missing:
        raise ConfigurationError(
            "override entry is missing required provenance fields",
            context=context,
            missing=missing,
        )
    body = {k: v for k, v in raw.items() if k not in _PROVENANCE_FIELDS}
    body["provenance"] = {
        "reason": _collapse(raw["reason"]),
        "source": str(raw["source"]),
        "date": _stringify_date(raw["date"]),
        "author": str(raw["author"]),
    }
    return body


@lru_cache(maxsize=4)
def load_override_config(path: Path | None = None) -> OverrideConfig:
    """Load and validate ``config/asset_overrides.yaml``.

    Raises:
        ConfigurationError: If an entry is malformed or lacks provenance.
    """
    resolved = path or (config_dir() / "asset_overrides.yaml")
    document = _load_yaml(resolved)
    try:
        return OverrideConfig(
            schema_version=str(document.get("schema_version", "1.0.0")),
            canonical_assets=tuple(
                CanonicalAssetOverride(**_split_provenance(e, context=f"canonical_assets[{i}]"))
                for i, e in enumerate(document.get("canonical_assets") or ())
            ),
            aliases=tuple(
                AliasOverride(**_split_provenance(e, context=f"aliases[{i}]"))
                for i, e in enumerate(document.get("aliases") or ())
            ),
            never_merge=tuple(
                NeverMergeRule(**_split_provenance(e, context=f"never_merge[{i}]"))
                for i, e in enumerate(document.get("never_merge") or ())
            ),
            relationships=tuple(
                RelationshipOverride(**_split_provenance(e, context=f"relationships[{i}]"))
                for i, e in enumerate(document.get("relationships") or ())
            ),
        )
    except ConfigurationError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as a configuration error
        raise ConfigurationError(
            "invalid override configuration", path=str(resolved), detail=str(exc)
        ) from exc


def clear_config_cache() -> None:
    """Drop every cached configuration document.

    Used by tests that write temporary configuration files.
    """
    load_exchange_registry.cache_clear()
    load_collection_config.cache_clear()
    load_retention_config.cache_clear()
    load_override_config.cache_clear()
