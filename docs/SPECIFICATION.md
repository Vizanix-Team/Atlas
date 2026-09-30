# Atlas dataset specification (draft, schema 1.0.0)

Enough to write an independent producer or consumer without importing the Python package. The normative machine-readable forms are the JSON Schemas in [`schemas/json/`](../schemas/json).

## Identifiers

`venue:<slug>` · `asset:native:<chain>:<SYMBOL>` · `asset:evm:<chainId>:<0x-lowercase-address>` · `asset:fiat:<iso4217-lowercase>` · `asset:unresolved:<venue>:<SYMBOL>` · `instrument:<venue>:<class>:<native-symbol>`. Identifiers are opaque to consumers except for equality. A venue-scoped `asset:unresolved:*` identity MUST NOT be pooled with another venue's.

## Generation

A generation is a set of files plus `manifest.json`, identified by `generation_id`, published as GitHub Release `atlas-data-<generation_id>`. Its `manifest.json` (`GenerationManifest`) lists every file with `filename`, `size_bytes`, `sha256`, `row_count`, `table`, `generation_id`, `shard_index` where applicable. A consumer MUST verify `sha256` of every file it uses and MUST NOT treat a file as valid because it exists.

## Latest pointer

Release `data-latest` carries `latest.json` (`LatestPointer`): `generation_id`, `manifest_sha256`, `release_tag`, `generated_at`, `previous_generation_id`. It is written last in publication; a generation not referenced by it is not canonical. Consumers resolve the pointer, fetch and verify the manifest, then fetch only the files they need.

## Sharding

`asset_market_state` rows are split into `shard_count` files (`assets-NN.parquet`). An asset's shard is `int(sha256(asset_id)[0:8], big-endian) mod shard_count`.

## Columns

Nested state sections are flattened with `__` (`reference_price__value`); sequences of models are JSON strings. Layouts: [`schemas/arrow/`](../schemas/arrow). Enums are lower-case strings; consumers MUST tolerate unknown enum values (venues add them).

## Data quality semantics

Null means unknown, never zero. `included_in_reference_price` and `exclusion_reason` account for every venue observation. `is_partial` is true when coverage is below full. Values are `float64`; timestamps are ISO-8601 UTC strings in manifests and epoch milliseconds or timestamps in tables as documented per column.

## Compatibility

`0.x`: breaking changes are allowed but recorded in the changelog with a schema-version bump. From `1.0` of the *schema* (current), a removed or retyped field is a major bump, enforced by the golden-schema test.

## Non-normative

The methodology behind each derived value is in [METHODOLOGY](METHODOLOGY.md); a producer claiming Atlas compatibility SHOULD state its methodology version.
