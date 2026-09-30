# GitHub-native architecture

Atlas uses only GitHub: a repository, Actions (GitHub-hosted runners), Releases, Pages and the built-in `GITHUB_TOKEN`. It needs no server, database, secret, or private exchange key. Exchanges are data sources, not infrastructure. If every Vizanix-owned server disappeared, Atlas would keep working.

## Workflows

| Workflow | Trigger | Permissions | Purpose |
|---|---|---|---|
| `ci.yml` | PR, push to `main` | `contents: read` | Lint, format, types, generated-file drift, tests on 3.11-3.13, CLI smoke, frontend typecheck/build. **Never contacts a real exchange.** |
| `collect.yml` | cron `7,22,37,52 * * * *`, manual | read; `aggregate` job `contents: write` | `plan` lists enabled venues → matrix of one `collect` job per venue (`fail-fast: false`) → one `aggregate` job builds, gates and publishes |
| `compact-daily.yml` | cron 02:17 UTC, manual | `contents: write` | Daily compaction |
| `maintenance.yml` | cron 03:43 UTC, manual | `contents: write` | Housekeeping (dry run unless `apply=true`) |
| `discovery.yml` | weekly | read | Adapter-drift probe: collects every venue and writes a table to the job summary. Publishes nothing |
| `pages.yml` | hourly, push to `web/**`, manual | `pages: write`, `id-token: write` on deploy only | Export latest dataset, build site, deploy |
| `security.yml` | PR, push, weekly | `security-events: write` on CodeQL | CodeQL (Python, Actions), dependency review |
| `release.yml` | tag `v*` | `contents: write` on publish only | Build and publish software release |

Cron minutes avoid `:00`, where GitHub queues the most scheduled work. `collect` and the maintenance workflows use `concurrency` groups without cancelling in-progress runs.

## Security posture

- Third-party actions are **pinned to full commit SHAs** (with the version in a comment) and updated by Dependabot.
- Default permission is `contents: read`; write is granted per job.
- No `pull_request_target`; fork PRs get no secrets and no write token. Checkout uses `persist-credentials: false`.
- Publishing runs only on `main`/schedule. Website deployment cannot touch dataset releases.

## What GitHub imposes (verify current limits before relying on them)

Scheduled workflows can be delayed or skipped and may be disabled after 60 days of repository inactivity; runner hours, storage and release-asset limits apply and change over time. Jobs are designed to finish in minutes: a full 16-venue collection takes ~10 s of wall time and aggregation ~17 s ([BENCHMARKS](BENCHMARKS.md)). Whether every venue is reachable from GitHub-hosted runners is **unknown until the first scheduled runs**; blocked venues are reported honestly, never proxied around.

## Abstraction

Business logic never imports GitHub code. `Publisher` is a protocol (`FilesystemPublisher` locally, `GitHubReleasePublisher` in Actions). The same commands run on a laptop: `atlas collect`, `atlas build-dataset`, `atlas verify`. GitHub is the production orchestrator, not a hard dependency of the analytics.
