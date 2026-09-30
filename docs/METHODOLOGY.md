# Methodology

Methodology version `1.0.0`. Every published generation records the methodology version that produced it, so two generations are never silently compared across a formula change. Changing a formula bumps this version and requires a rationale, the formula, tests and a compatibility note (see [CONTRIBUTING](../CONTRIBUTING.md)).

The per-metric definitions (unit, formula, minimum coverage, meaning of null) are in the generated [metric dictionary](METRICS.md). This document explains the *why*.

## Principles

1. **Measure, don't guess.** If an input is unobservable, the output is null, not zero and not a default.
2. **Explain, don't obscure.** Every derived number has recorded inputs. Prefer several honest dimensions over one score.
3. **Partial failure is normal.** One venue failing never makes a metric wrong; it makes its coverage smaller, and coverage is published.
4. **No signals.** Atlas describes state. It never labels anything buy, sell, bullish or bearish.

## Numerical policy

Values are IEEE-754 `float64`. Nothing is rounded until presentation. Non-finite values (`NaN`, `±inf`) are rejected at the validation boundary and never stored. Aggregations sort their inputs before reducing, and weighted statistics break ties by a stable key, so the same inputs always give bit-identical outputs (tested).

## Time

All internal timestamps are UTC. Atlas keeps these apart and never merges them: exchange event time, exchange server time, collector receive time, collection start/finish, snapshot effective time, and publication time. A run's *snapshot effective time* is its start floored to the 15-minute slot, so a late-starting job is attributed to the window it was scheduled for. Observation age and staleness are measured against when collection *finished*, not against the floored slot label (which sits before every observation in the run).

## Quote currencies and USD conversion

USDT, USDC, DAI and USD are not assumed equal. Atlas builds a graph of observed conversion rates (for example a `USDT/USD` market on a USD venue) and finds the shortest observed path from a quote currency to USD. Every USD-normalised value keeps the original quote currency, the rate, the observation time, the source venue, whether the conversion was direct or derived, and the measured deviation from parity.

A conversion whose rate is more than `max_stablecoin_deviation_bps` (500) from parity is refused rather than used: a rate that far from 1.0 is more likely a broken market than a real depeg, and publishing USD figures through it would corrupt every total derived from it. The measured deviation is still reported. If no conversion is observable the USD value is empty and the observation is excluded from the reference price with reason `invalid_quote_conversion`.

## Global reference price

Atlas publishes `reference_price`, not "price": there is no single true price for an asset on sixteen venues in several quote currencies, only a defensible estimate and the record of how it was reached.

**Inputs.** For each venue instrument that passes validation: a USD price (mid if a two-sided quote exists, otherwise last, recorded as `price_source`), its age, and a volume used for weighting.

**Why a weighted median, not a mean.** A mean is unboundedly sensitive to one bad input: a venue quoting a different asset under the same ticker, or a halted market's stale last price, moves it arbitrarily. A weighted median bounds every venue's influence by its weight, independent of how wrong its price is.

**Weights.** For venue observation *i*:

```
w_i = ( V_i ^ 0.5 ) × freshness_i × source_i
```

- `V_i` is USD volume used only for weighting (the venue's reported quote volume converted to USD; where a venue publishes only base volume, `base volume × last price × conversion` is used, and that derived figure is never published as a volume).
- `freshness_i` decays linearly from 1 at age 0 to 0.25 at the staleness limit (30 minutes).
- `source_i` scores a two-sided mid above a last trade.

Weights are normalised and then **capped** at 35 % each (excess is redistributed), so the largest venue's self-reported volume cannot become the price. A venue with no usable volume gets a small non-zero weight, so it still counts as a source.

**Exclusions.** Nothing is silently dropped. Every observation is recorded as included or as excluded with a reason (`stale`, `extreme_deviation`, `invalid_quote_conversion`, `unresolved_instrument`, `spot_preferred`, and the others in the [quality doc](QUALITY.md)); `included + excluded` always equals what was considered. Deviation is judged against a provisional robust centre computed from the observations themselves, with a 2,000 bps limit: wide enough to keep genuinely fragmented microcaps, narrow enough to drop a venue quoting a different asset.

**Spot preferred.** Derivatives contribute only when no spot venue qualifies; the method is then `derivative_fallback`. With one qualifying venue the method is `single_venue_spot`, so the weakness is visible.

**Options** are never price sources: an option's price is a premium, not the underlying's price.

### Worked example

Five spot venues quote BTC in USD, all fresh mids. Volumes and quotes:

| Venue | Price | Reported 24h USD volume | √volume | Share before cap | Weight after 35 % cap |
|---|---:|---:|---:|---:|---:|
| a | 84,400 | 4.0 B | 63,246 | 46.9 % | **35.0 %** |
| b | 84,410 | 1.0 B | 31,623 | 23.5 % | 25.0 % |
| c | 84,395 | 250 M | 15,811 | 11.7 % | 14.9 % |
| d | 84,405 | 100 M | 10,000 | 7.4 % | 11.2 % |
| e | 91,000 | 200 M | 14,142 | 10.5 % | 13.8 % |

Ordering by price: c (84,395, cum. 14.9 %), a (84,400, 49.9 %), d (84,405, 61.1 %), b, e. The weighted median is the first price where cumulative weight reaches 50 %: **84,405**. Venue *e*, 7.8 % above the rest, is inside the deviation limit so it is included, and it barely moves the result (a simple mean would be 85,722). Push *e* to 200,000 and it exceeds the 2,000 bps limit: it is excluded with reason `extreme_deviation` (recorded, not dropped) and the reference price becomes 84,400. This example is computed by the real `compute_reference_price` (the numbers above are its output).

## Dispersion

Robust statistics only:

- `price_dispersion_bps`: weighted median absolute deviation of venue prices from the reference price, in bps (here 0.59 bps).
- `p10_p90_spread_bps`: the 10th to 90th percentile spread (here 470 bps): the number that shows venue *e* is far from the pack while the MAD says the bulk agree.
- `max_absolute_deviation_bps`, min/max qualified price, contributing venue count.

Read them together. Wide dispersion may be real fragmentation, a stale quote, or a thin market; Atlas reports it and does not interpret it.

## Volume

`reported_*_volume_24h_usd` is what venues publish, converted with the conversion graph. Atlas does not verify it or detect wash trading. Spot, perpetual and dated-futures volume are separate fields, and instruments are de-duplicated. A venue that publishes only base volume (Coinbase, Kraken, Bitstamp, Bitfinex) contributes no published USD volume rather than an inferred one.

## Derivatives

**Funding.** The venue's native rate and interval are preserved (`funding_rate_raw`, `funding_interval_hours`). The 8-hour equivalent is `rate × 8 / interval_hours`. The simple annualisation is `rate_8h × 3 × 365`, clearly labelled simple and never compounded. Funding semantics (relative per interval, or absolute in quote units, as on Kraken Futures) are per-instrument metadata; an undocumented semantic excludes the observation rather than guessing.

**Open interest.** Raw value and unit are preserved. Base and USD equivalents are computed only when the contract multiplier and a reference price are reliable; otherwise null. A missing contract multiplier is never guessed.

**Basis.** `basis_bps = (derivative_reference − spot_reference) / spot_reference × 10,000`. The derivative reference is the mark price where published, and the source is stated; methodologies are never mixed invisibly.

## Fragmentation

`effective_venue_count = 1 / HHI` over venue volume shares, plus volume and liquidity concentration (largest venue share, HHI). These describe structure; they carry no "good" or "bad" grade.

## Liquidity (library implemented, not yet collected in scheduled runs)

From sampled order books: `spread_bps`, depth within 5/10/25/50/100 bps of mid, and *observable order-book impact* for notional sizes (10 k / 100 k / 1 M USD). This is not slippage: fees, latency, hidden liquidity, cancellations and market response are not modelled. Scheduled runs currently collect tier A only ([roadmap](ROADMAP.md)).

## History, warm-up and windows (needs accumulated history)

Windowed metrics (returns, realised volatility, funding percentiles, RSI/ATR/EMA, pressure and crowding vectors, genome) declare a minimum observation count and a maximum tolerated missingness (`config/collection.yaml`, `history`). Below the minimum the value is null and the window coverage is exposed; a 30-day percentile computed from two days is never presented as a 30-day percentile. **Atlas launches with no history and does not bootstrap any.** Gaps from missed runs are recorded and never interpolated.

## Technical indicators

A small `technical` section (RSI, EMA, ATR, and so on) exists for convenience. Outputs are numbers, never labels. Atlas will never output "strong buy" or similar.

## Reproducibility

A generation records its methodology, schema and software versions, the commit SHA, and the input collector runs. The state builder is a pure function of its inputs, so a generation can be recomputed under a new methodology if the source observations are retained.
