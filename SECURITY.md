# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a vulnerability. Use GitHub's private reporting: the repository's **Security** tab, then **Report a vulnerability**. Include what you found, how to reproduce it, and what you think the impact is. We aim to acknowledge reports within a few days; this is a small project, so please be patient and keep the report private until a fix is released.

Supported versions: the latest `0.x` release and `main`.

## Threat model

Atlas has no user accounts, no credentials to steal and no server. Its risks are integrity risks:

| Risk | Mitigation |
|---|---|
| **Malicious or malformed exchange payloads** (oversized bodies, deeply nested JSON, wrong types, hostile strings) | Response-size caps and JSON-depth limits at the HTTP boundary; every payload is parsed into typed Pydantic models; failures are typed (`SchemaMismatch`) and recorded per venue, never silently coerced. Venue text is rendered on the website through `textContent`, never `innerHTML`. |
| **Data poisoning** (a venue reporting extreme prices or volumes) | Validation and quarantine, robust weighted median with per-venue weight caps, stale/extreme exclusion with recorded reasons. Reported volume is labelled as reported. |
| **Corrupted or partial publication** | Publication order (upload, verify, manifest, `latest` pointer last), SHA-256 for every file, a validity gate with anomaly guards, last-known-good retention. `atlas verify` and the SDK re-verify checksums on read. |
| **Compromised dependency or action** | Third-party GitHub Actions are pinned to full commit SHAs; Dependabot watches Python, npm and Actions; dependency review and CodeQL run on pull requests. Updates never publish production data without passing CI. |
| **Workflow abuse** | Workflows default to `contents: read`; only the aggregation, compaction and maintenance jobs get `contents: write`, and only Pages deployment gets `pages`/`id-token`. No `pull_request_target`; fork pull requests run CI without secrets. Data-publishing workflows run only from `main`. |
| **Housekeeping deleting the wrong thing** | Dry-run by default; tag allow/deny lists (software `v*` releases are protected); minimum age; a verified compacted replacement required; per-run deletion cap; every deletion logged. |
| **Supply-chain confusion between code and data** | Software releases (`v*`) and data releases (`atlas-data-*`, `data-latest`) use disjoint tags. |

## What Atlas never does

It does not use API keys, sign requests, touch private endpoints, hold funds, or evaluate remote text as code. It does not bypass geographic or network restrictions: a blocked venue is reported as unavailable.

## Hardening checklist for contributors

- Never add a code path that evaluates strings from a venue (no `eval`, `exec`, `pickle`, dynamic imports).
- New network calls go through `ExchangeHttpClient` (timeouts, size cap, depth cap, rate limiting).
- New workflow steps pin actions by SHA and request the least permission.
- Do not commit fixtures containing headers, cookies or tokens.
