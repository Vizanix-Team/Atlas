# Operations

## Bootstrap

A fresh repository has no data releases and scheduled workflows do not run until the workflow files are on the default branch. To start:

1. Merge to `main`. In **Settings → Actions → General**, allow workflows to run and set **Workflow permissions** to allow the jobs' `contents: write` (the workflows request only what each job needs).
2. In **Settings → Pages**, set the source to **GitHub Actions**.
3. Run **Collect** manually (Actions → Collect → Run workflow). Success creates `atlas-data-<id>` and `data-latest` releases. The first run needs at least 4 successful venues or the validity gate refuses to publish.
4. Run **Pages** manually to deploy the site with real data (until then it shows the fixture, clearly labelled).
5. **Relay venues.** Bybit and Binance derivatives are collected by the [relay](RELAY.md), not by the runners. Until a relay is serving them (or `ATLAS_RELAY_URL` points at yours), they appear as `failed` in every generation and the rest of the market is unaffected.
6. Check the job summaries. Then leave the schedule to run. Set `ATLAS_REPOSITORY=owner/name` (or edit `DEFAULT_OWNER`/`DEFAULT_REPO` in `sdk/remote.py`) so `Atlas.latest()` points at your repository.

Repository topics to set: `cryptocurrency`, `market-data`, `quantitative-finance`, `algorithmic-trading`, `order-book`, `crypto-data`, `market-microstructure`, `derivatives`, `python`, `parquet`, `duckdb`, `polars`, `github-actions`, `open-data`.

## The live layer service

The live layer ([STREAMING](STREAMING.md)) is a long-running process, not a workflow. The project's own instance runs next to the relay, as its own unprivileged user, from `systemd`:

```ini
[Service]
User=atlas
Environment=ATLAS_CACHE_DIR=/opt/atlas-stream/cache
ExecStart=/opt/atlas-stream/venv/bin/atlas stream serve --host 127.0.0.1 --port 8787
Restart=always
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/atlas-stream/cache
MemoryMax=1G
```

A second unit and timer run a health check every minute: it restarts the service after two consecutive failures to answer `/v1/health` or after it reports `"status": "down"` (every feed dropped or no asset priced). `Restart=always` covers crashes; the check covers a process that is alive but not serving.

Reading it:

| Signal | Meaning |
|---|---|
| `status: ok` | Every configured feed is connected and assets are priced |
| `status: degraded` | At least one feed is down, or the last universe refresh failed (the previous universe keeps serving) |
| `status: down` | No feed connected, or no asset has a live price |
| a feed's `parse_failures` rising | That venue changed its message format |
| a feed's `refused_by_venue: true` | The venue refuses this network (403, 429, 451) |

After a code change: install the new wheel into the service's virtual environment and `systemctl restart atlas-stream`. The universe is reloaded from the newest generation every 30 minutes without a restart.

## Keeping the schedule honest (optional dispatcher)

GitHub throttles `schedule:` triggers on low-traffic repositories, so Collect may run every few hours instead of every 15 minutes. A `workflow_dispatch` is not throttled. A tiny timer on any always-on host can fire Collect on time:

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $ATLAS_DISPATCH_TOKEN" -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/<owner>/<repo>/actions/workflows/collect.yml/dispatches \
  -d '{"ref":"main"}'
```

The token should be a fine-grained personal access token for this one repository with only **Actions: read and write**: it can start workflows and nothing else (it cannot read secrets or write contents). Keep it in a root-only environment file. The workflow's own concurrency group stops an overlapping run from stacking up. The live layer does not need this; it exists for the dataset's cadence.

## Working state between runs

Besides generations, the `data-latest` release holds small auxiliary assets, all written after a generation is published and none of them allowed to fail a run:

| Asset | What it is | Read by |
|---|---|---|
| `latest.json` | The pointer to the canonical generation. | SDK, CLI, Pages. |
| `books-plan.json` | The order-book plan (tier B) for the next run. | Every collector, including the relay. |
| `history-a.parquet`, `history-b.parquet` | The history store, written alternately so one is always intact. | The aggregate job. |

A run that cannot read the history continues without it **and does not save**, so a flaky read can never erase accumulated history. A corrupt history asset is ignored and the other one is used. Deleting both resets the windowed metrics to "insufficient history" and nothing else.

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
