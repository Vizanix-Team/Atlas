# Data model

Pydantic v2 models in `src/vizanix_atlas/models/` are the schema. JSON Schemas are generated into [`schemas/json/`](../schemas/json) and the published Parquet layouts into [`schemas/arrow/`](../schemas/arrow); CI fails if they drift (`python scripts/generate.py --check`).

## Entities

| Entity | Notes |
|---|---|
| `Venue` | Slug, capabilities, rate limit, docs and terms URLs, enabled flag (a disabled venue must record why) |
| `CanonicalAsset`, `AssetAlias`, `AssetRelationship` | Identity, evidence-backed aliases, typed graph edges |
| `Instrument` | Spot / perpetual / future / option. Cross-field validators: options need strike, type and expiry; spot rejects a contract multiplier; perpetuals reject an expiry. Contract multipliers are never guessed |
| `Raw*` observations | Ticker, derivative (mark/index/funding/OI), order book, FX; every timestamp field kept distinct |
| `MarketState`, `VenueAssetState` | The asset-level state and its per-venue decomposition |
| `QualityEvent`, `Provenance`, `ProvenanceEntry`, `Coverage` | Quality records |
| `GenerationManifest`, `FileEntry`, `LatestPointer`, `DailyCompactionReport`, `SystemHealth` | Publication |

All models are frozen and reject unknown fields.

## Published tables (Parquet, Zstandard)

`assets`, `asset_aliases`, `instruments`, `adapter_health`, `quality_events`, `venue_asset_state`, and `asset_market_state` (sharded into `assets-NN.parquet`, 64 shards by default). Bulk data is Parquet; JSON is used only for manifests, pointers and small summaries.

Not yet published: `asset_relationships`, and separate `derivative_observations` / `liquidity_observations` tables (their content currently lives inside `venue_asset_state` and the state row).

## Versions (kept separate)

| Version | Changes when |
|---|---|
| Software | any release (`0.1.0`) |
| Schema | published tables or manifest shape change (`1.0.0`) |
| Methodology | a formula changes (`1.0.0`) |
| Dataset format | physical layout changes (`1.0.0`) |
| Generation ID | every generation (`20260930T010000Z`) |

## Missing data

`null` is always distinguishable from zero. Metric-level meaning of null is in the [dictionary](METRICS.md).

## Extensibility

New fields are added deliberately with a schema bump and a regenerated schema diff. There is no catch-all `metadata: dict` field.
