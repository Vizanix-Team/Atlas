# Data quality

Quality is a first-class part of the data, not a footnote. Prefer the individual dimensions below to any single "confidence" number: Atlas deliberately publishes none.

## Missing is not zero

Every null has a reason (`not_collected`, `not_supported`, `insufficient_coverage`, `insufficient_history`, `invalid_input`, `not_applicable`), declared per metric in the [dictionary](METRICS.md). `funding = 0` means the venue reported zero; a null funding means Atlas could not say.

## Per-asset dimensions

Each asset state carries: source and expected venue counts, `coverage_ratio`, freshest and oldest observation age, included/excluded venue counts with reasons, `ambiguous_identity_count`, `is_partial`, and the generation, schema and methodology versions. Coverage is also broken down per metric family (price sources, volume sources, funding sources, and so on), so one large venue does not define what "covered" means.

## Validation and quarantine

Observations are validated at the boundary and **quarantined with a reason, never coerced**: missing or implausible timestamp, non-finite or non-positive price, negative quantity, high below low, best bid above best ask (crossed book), unresolved quote currency, stale data, inactive instrument, duplicate instrument, unknown contract multiplier, undocumented funding semantics, schema failure. Contract expiry dates use a separate ten-year plausibility window from observation timestamps (a one-day clock-skew window would drop every dated future and option).

Quarantine counts by reason are published per generation (`quality_events`, capped at 200 detailed events per reason; counts are always exact).

## Exclusion reasons

`stale`, `extreme_deviation`, `invalid_quote_conversion`, `unresolved_instrument`, `ambiguous_identity`, `crossed_book`, `schema_failure`, `missing_timestamp`, `non_positive_price`, `missing_price`, `inactive_instrument`, `unknown_contract_multiplier`, `undocumented_funding_semantics`, `duplicate_instrument`, `spot_preferred`. Each excluded observation stays in provenance with its reason.

## Adapter health and schema drift

Per venue per run: status (`success`, `degraded`, `failed`, `disabled`, `unavailable_from_collector_network`), duration, request/timeout/rate-limit/HTTP/application error counts, parse failures, bytes received, circuit-breaker state, clock skew, and unknown enum values. A venue that changes its schema shows up as parse failures and unknown enum values for that venue only; it cannot stop another venue or the whole run.

## Publish gate

A generation becomes `latest` only if it passes: schema validity, manifest validity, checksums re-computed from disk, per-table row counts reconciled with the manifest, and anomaly guards:

| Guard | Default |
|---|---|
| Minimum successful venues | 4 |
| Minimum successful venue ratio | 0.35 |
| Max asset-count drop vs previous | 50 % |
| Max instrument-count drop vs previous | 50 % |
| Max total-size change vs previous | 4× |
| Max adapter parse-failure ratio | 25 % |

Thresholds are configuration (`config/collection.yaml`, `publishing.anomaly_guards`). A failing generation is reported and **not** published; the previous generation stays canonical. Twelve venues of fourteen succeeding is a normal degraded generation; one of sixteen is not the market.

## What quality does *not* mean

Passing these checks does not make a value true. A venue can report plausible, consistent, wrong numbers, and reported volume can be inflated. See [LIMITATIONS](LIMITATIONS.md).
