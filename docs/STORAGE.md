# Storage and publication

GitHub Releases are the persistent data layer. There is no external database or server.

## Release naming

Data and software use disjoint tags so a data release can never be mistaken for a software release, and data releases are never marked as GitHub's implicit "latest release" (`make_latest=false`, and generation releases are pre-releases).

| Tag | Contents |
|---|---|
| `v0.1.0`, ... | Software releases (sdist/wheel) |
| `data-latest` | One small file, `latest.json`, the pointer to the current generation |
| `atlas-data-<generation_id>` | One generation's Parquet files and `manifest.json` |
| `atlas-data-YYYY-MM-DD` | A day's compacted files, `daily-report.json` and a manifest |

Clients use the stable, predictable asset URL `https://github.com/<owner>/<repo>/releases/download/<tag>/<file>`, not the REST API, so reads need no token and don't consume API rate limit.

## Files in a generation

`assets`, `asset_aliases`, `instruments`, `adapter_health`, `quality_events`, `venue_asset_state` (single Parquet files) and `assets-00 … assets-63` (the sharded `asset_market_state`), plus `manifest.json`. Parquet with Zstandard (level 9), 64 K-row groups. A real 16-venue generation is 70 files and ~7.4 MB.

## Sharding

`shard = int(sha256(asset_id)[0:8]) mod shard_count`. A client that wants one asset downloads the small `assets` table (to resolve the ticker) and one shard. `shard_count` is configuration; 64 is a starting value chosen for a ~12 K-asset universe (~100 KB per shard), not a fixed constant.

## Publication sequence

GitHub Releases has no transactions, so Atlas controls the *order* of writes so that no reachable intermediate state looks like a valid generation:

1. Build every file locally; hash it.
2. **Validity gate**: schema, manifest, checksums re-computed from disk, row-count reconciliation, anomaly guards.
3. Upload every file to `atlas-data-<generation_id>`.
4. Re-hash local files to catch corruption between hashing and upload.
5. Publish `manifest.json`.
6. Publish `data-latest/latest.json` (**last**).

A failure anywhere before 6 leaves the previous generation canonical. Clients trust the manifest, not file existence; a half-uploaded generation is a normal intermediate state and is invisible. Re-running a slot replaces same-named assets, so retries are idempotent.

## Client caching

Files are cached at `$ATLAS_CACHE_DIR` or `~/.cache/vizanix-atlas/<generation_id>/`. Every read re-checks the SHA-256 against the manifest; a mismatching cached file is treated as absent and re-downloaded. The generation ID is part of the cache key, so a new generation never reuses stale bytes.

## Daily compaction

`atlas compact-day` (03:17 UTC by default) discovers the previous UTC day's generations by their own slot labels, computes expected slots from the schedule, and classifies each slot as successful, **missing** (never interpolated), **duplicate** (not compacted, to avoid double counting), or **invalid**. It concatenates each shard across successful slots (adding `slot_label` and `generation_id`) and uploads the result, then **re-downloads and re-hashes every uploaded file** before writing a report with `compacted: true`.

## Retention and housekeeping

Configured in `config/retention.yaml`. `atlas housekeeping` is a dry run unless `--apply` is passed, and even then a file is deleted only if all hold: its tag matches an allowed prefix; it does not match a protected prefix (`v`); it is older than `raw_snapshot_days` and the minimum age; its generation is outside the most recent `keep_recent_generations` (3); a day release with `compacted: true` covers its day; and the run has not reached `max_deletions_per_run` (200). Manifests are kept for lineage. Software releases are unreachable by this code. Hourly/monthly roll-ups are configured but **not implemented yet**.

## Size guards

Configured hard budgets (`config/collection.yaml`, `publishing`): 32 MiB per file, 256 MiB per generation, 5 M rows, 80 assets per release. A schema change that inflates output fails the run instead of publishing. Measured numbers are in [BENCHMARKS](BENCHMARKS.md).

## Recovery

See [OPERATIONS](OPERATIONS.md#recovery).
