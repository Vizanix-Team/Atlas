# Contributing

Thank you for helping. Atlas values correctness, explainability and honesty about limits over feature count. Read [ARCHITECTURE](docs/ARCHITECTURE.md) and [METHODOLOGY](docs/METHODOLOGY.md) first; they explain *why* the code is the way it is.

## Setup

```bash
git clone https://github.com/Vizanix/Atlas && cd Atlas
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests
mypy src
python scripts/generate.py --check
pytest
# frontend (optional)
cd web && npm ci && npm run build
```

No Docker and no credentials are needed. **Tests never call real exchanges**; do not add a test that does. Optional live checks belong in a separate, non-required workflow.

## Pull requests

CI must pass (lint, format, types, generated files, tests, frontend build). Keep changes focused. Update docs and the changelog. If you change a metric, schema or methodology, regenerate (`python scripts/generate.py`) and say so in the PR: that diff is what reviewers read. Do not weaken a test to make it pass; do not skip or quarantine tests to get green.

## Adding an exchange adapter

Checklist (also in the PR template):

- [ ] Read the **current official documentation**; link it in `config/exchanges.yaml`. Never write an endpoint, field, unit or semantic from memory.
- [ ] Public, unauthenticated endpoints only. No keys, no private or undocumented APIs, no proxies, no geo/CDN bypass.
- [ ] Bulk endpoints for instruments and tickers; document per-endpoint rate limits and set conservative limits.
- [ ] Instrument discovery is complete (all active spot/perp/dated/option products the venue offers) and units are normalised; contract multipliers are taken from the venue, never guessed.
- [ ] Timestamps mapped to UTC (event / server / receive kept separate); funding interval and semantics recorded per instrument.
- [ ] Error semantics mapped to the typed taxonomy; geo-blocks reported as `GeoRestricted`, not bypassed.
- [ ] Capture a real response, trim it, strip all headers, and record origin and date in `tests/fixtures/exchanges/README.md`. Keep deliberately awkward records (nulls, inverse contracts, odd enums).
- [ ] Implement `adapters/<slug>.py`, register it in `adapters/registry.py`, and wire fixtures in `tests/contract/wiring.py`. The universal contract tests then run against it.
- [ ] Data terms reviewed; record the terms URL and any attribution; leave redistribution conservative.
- [ ] Declare capabilities in the registry; they are asserted by tests.
- [ ] Enable it (`enabled: true`) only after everything passes. If it cannot be verified, add a disabled entry with the concrete reason instead.

## Adding or changing a metric

Declare it once in `core/metrics_registry.py` (unit, scope, formula, inputs, minimum coverage/history, what null means, caveats). Implement it in `analytics/`, unit-test it (including missing-data behaviour and determinism), regenerate docs, and update `docs/METHODOLOGY.md`. Avoid opaque scores; prefer components. Never name a metric `volume` or `score`; put the unit and scope in the name.

## Changing methodology

Requires: a rationale, the formula, tests, a backwards-compatibility note, and a bump of `METHODOLOGY_VERSION` in `core/versions.py`, with a changelog entry under *Methodology*.

## Changing the schema

Regenerate `schemas/`, bump `SCHEMA_VERSION` for any removed or retyped field, and add a changelog entry under *Schema*.

## Identity mappings

Edit `config/asset_overrides.yaml`. Every entry needs `reason`, `source`, `date` and `author`. Provide evidence (contract address, chain, the venue's own metadata). Prefer leaving an asset unresolved to guessing.

## Fixtures and docs

Fixtures must be sanitised (no headers, cookies or tokens) and documented. Documentation should explain *why*, use notation for formulas with a worked example, and state limits plainly. Avoid marketing language and any signal wording (buy, sell, bullish, bearish, opportunity).

## Code style

Typed Python (mypy strict), small modules, no god objects or catch-all utils, no silent exception handling, no untyped dicts where a model fits, no mysterious constants (explain each in a comment), comments only for the *why*. The formatter is Ruff.

## Conduct and security

See [CODE_OF_CONDUCT](CODE_OF_CONDUCT.md). Report vulnerabilities privately per [SECURITY](SECURITY.md).
