# Operations

## Bootstrap

A fresh repository has no data releases and scheduled workflows do not run until the workflow files are on the default branch. To start:

1. Merge to `main`. In **Settings → Actions → General**, allow workflows to run and set **Workflow permissions** to allow the jobs' `contents: write` (the workflows request only what each job needs).
2. In **Settings → Pages**, set the source to **GitHub Actions**.
3. Run **Collect** manually (Actions → Collect → Run workflow). Success creates `atlas-data-<id>` and `data-latest` releases. The first run needs at least 4 successful venues or the validity gate refuses to publish.
4. Run **Pages** manually to deploy the site with real data (until then it shows the fixture, clearly labelled).
5. Check the job summaries. Then leave the schedule to run. Set `ATLAS_REPOSITORY=owner/name` (or edit `DEFAULT_OWNER`/`DEFAULT_REPO` in `sdk/remote.py`) so `Atlas.latest()` points at your repository.

Repository topics to set: `cryptocurrency`, `market-data`, `quantitative-finance`, `algorithmic-trading`, `order-book`, `crypto-data`, `market-microstructure`, `derivatives`, `python`, `parquet`, `duckdb`, `polars`, `github-actions`, `open-data`.

## Running the pipeline locally

```bash
atlas collect --exchange okx --output .local-data/collected      # repeat per venue
atlas aggregate .local-data/collected                             # summary only
atlas build-dataset --input .local-data/collected --output .local-data/dataset
atlas verify .local-data/dataset
```

## Reading the health of the system

Each generation records per-venue status, request counts, parse failures and notes (`adapter_health` table, Exchanges page). `atlas status` shows the current generation and its venue outcome. A venue with rising `parse_failures` or a "possible upstream schema change" note has probably changed its API.

## Gaps and missed runs

A slot with no generation is a gap. Compaction records it under `missing_windows`; nothing fills it. Backfilled data, if ever added, must carry `observation_origin = historical_backfill`, distinct from `scheduled_snapshot`. Re-running a slot replaces same-named assets and does not create a second logical snapshot.

## Recovery

| Situation | Action |
|---|---|
| `latest` points at a bad generation | Manually re-point `data-latest`: build a `latest.json` for the previous good generation (its `manifest.json` gives the id and hash) and upload it with `gh release upload data-latest latest.json --clobber`. Every generation stays immutable in its own release. Then investigate why the gate let it through. (There is no CLI command for this yet.) |
| Generation partly uploaded | Nothing to do: `latest` was never advanced. Re-run the slot |
| Failed compaction | Re-run `compact-day --day YYYY-MM-DD`; uploads replace same-named files. No source file is deleted until the day is verified |
| Housekeeping deleted too much | It cannot delete manifests, `v*` releases, or unverified days; restore from the daily release |
| Stale Pages | Re-run the Pages workflow |
| Schema migration interrupted | Generations are immutable and versioned; publish a new generation under the new schema and keep the old until clients migrate |

## Dependabot and updates

Dependency and action updates arrive as pull requests and must pass CI. They never publish data.

## Verifying a download

```bash
atlas verify <dataset-dir>   # Manifest / Checksums / Row counts
```
