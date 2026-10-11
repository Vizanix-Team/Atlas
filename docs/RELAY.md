# The relay

Some venues refuse the network GitHub-hosted runners use. Bybit answered HTTP 403 from its CDN edge, and Binance's futures hosts (`fapi`, `dapi`) answered HTTP 451, to the United States address they were probed from (`tests/fixtures/exchanges/AVAILABILITY.md`), and neither has a market-data-only mirror. GitHub-hosted runners also egress from United States ranges, so the same refusal is what to expect there. Atlas does not disguise where it runs and does not use proxies. Instead those venues are **collected from a network where they answer normally**, with the same adapters and the same result format, and the scheduled workflow *pulls* the validated result.

A venue is a relay venue when `config/exchanges.yaml` says `egress: relay`. Today: `bybit` and `binance_derivatives`. Every other venue is `direct` and is collected by the workflow's own runners.

## Shape

```text
relay host (non-US)                         GitHub Actions
-------------------                         ---------------
systemd timer, every 15 min (:02 :17 :32 :47)
  atlas collect --exchange bybit
  atlas collect --exchange binance_derivatives
  atlas relay-publish                  ->   https://<host>/atlas-collector/{bybit,binance_derivatives}.json
                                            https://<host>/atlas-collector/manifest.json
                                                              |
                                       collect.yml, job `relay` (:07 :22 :37 :52)
                                         atlas fetch-relay --exchange bybit
                                              |  https only, size cap, SHA-256 vs manifest,
                                              |  schema, venue identity, freshness
                                              v
                                         artifact collected-bybit  ->  aggregate job
```

The relay holds no GitHub credential and the workflow holds no credential for the relay: the files are public, read-only market data.

## What `fetch-relay` checks

| Check | Why |
|---|---|
| The venue is declared `egress: relay` | A relay can never supply, or overwrite, a venue the runners collect themselves. |
| The URL is `https`, and no redirect leaves `https` | No downgrade. |
| The body is under 32 MiB | A bounded read, whatever the host serves. |
| The body's SHA-256 equals the manifest's | Catches a torn or cached download. One retry after a few seconds, because the relay can update between the two requests. |
| The body parses as a `CollectionResult` for that venue | Schema and identity. |
| It is at most 20 minutes old, and not from the future | A stale result is never published as if it were current; a future date means the relay clock is wrong. |

Any failure produces an explicit `failed` result for the venue, with the reason in `health.notes`, so the aggregation records the gap instead of losing the venue. The validity gate and the anomaly guards then apply to relayed data exactly as they do to direct data.

## Running a relay

Any host in a jurisdiction where the venues answer normally, with Python 3.11+ and a web server that serves one directory.

```bash
python -m venv /opt/atlas-collector/venv
/opt/atlas-collector/venv/bin/pip install vizanix-atlas
```

A minimal run script (the project's own relay runs this as an unprivileged user from a `systemd` timer):

```bash
#!/bin/bash
set -u
WORK=$(mktemp -d) ; trap 'rm -rf "$WORK"' EXIT
export ATLAS_BOOKS_PLAN="https://github.com/<owner>/<repo>/releases/download/data-latest/books-plan.json"
for venue in bybit binance_derivatives; do
  atlas collect --exchange "$venue" --output "$WORK" || echo "collect failed for $venue"
done
atlas relay-publish --input "$WORK" --output /var/www/atlas-collector
```

`relay-publish` validates each result, writes each file atomically (temporary file, then rename) and writes `manifest.json` last. A venue whose result is missing or invalid is left out of the manifest and its previous file is removed, so it cannot go stale under a fresh manifest.

Point the workflow at your relay with `ATLAS_RELAY_URL` (or `--url`). The default is the project's own relay at `https://vizanix.tech/atlas-collector`.

## Order books on the relay

The tier B plan (which markets get a book snapshot) is published by each generation next to `latest.json`. The relay reads it the same way the runners do, so relay venues get their books in the same pass as their tier A data. With no plan (the first run, or GitHub unreachable) only tier A is collected.

## Trust, stated plainly

A relay is a host the project operates, and pulling a file from it is trust. The checks above bound what a faulty or compromised relay can do (it cannot touch direct venues, cannot serve something stale as fresh, cannot serve a malformed result), but a relay that serves *plausible, current, well-formed* wrong numbers for its own venues would pass them. The defences for that are the same as for any venue: cross-venue dispersion, the reference price's exclusion rules and the anomaly guards. Atlas records which venues were relayed in the health notes so the provenance is visible in every generation.
