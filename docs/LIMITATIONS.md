# Limitations

Atlas earns trust by saying plainly what it cannot do. Read this before relying on any number.

## It is not real time

Snapshots are scheduled every 15 minutes at best. GitHub Actions can delay or drop scheduled runs (especially at busy times, and inactive repositories can have scheduled workflows disabled). Delays change the effective slot; drops become recorded gaps. Do not use Atlas for execution, latency-sensitive decisions, or anything where minutes matter.

## Public data only

Atlas reads unauthenticated public endpoints. It has no order flow, no private or account information, no trades-by-participant, and no view of anything a venue does not publish. It never uses API keys and never tries to defeat a block: a venue that refuses GitHub's network is `unavailable_from_collector_network`. Bybit (geo-blocked from the network used to build 0.1.0), BitMEX (unrepresentative catalogue) and Binance derivatives (HTTP 451) are disabled with recorded reasons. Binance derivatives are among the largest, so **funding and open-interest coverage is materially incomplete** relative to the whole market.

## Exchange APIs differ, and lie

Units, timestamps, contract multipliers, funding semantics and even the meaning of "24h volume" vary by venue and sometimes by product on one venue. Atlas normalises what it has verified against live payloads and excludes what it cannot. It can still be wrong: a venue can publish plausible, internally consistent, incorrect data. Passing validation is not correctness.

## Reported volume is not real volume

It is what venues report, unverified. Wash trading and incentive programmes can inflate it enormously: in a real run a single venue reported ~$19.5 B of 24-hour volume for a tokenised-equity spot pair. Atlas labels it, keeps spot and derivatives apart, ranks headline lists among assets on two or more venues, and does not detect manipulation.

## Identity is conservative, so coverage is thin outside majors

Only ~169 of ~12,100 assets in a real run were pooled across two or more venues. Most tickers are venue-scoped because Atlas will not merge on a ticker without evidence. Majors resolve well; the long tail mostly does not yet ([asset resolution](ASSET_RESOLUTION.md)). Ambiguous tickers raise instead of guessing.

## Some headline capabilities are not live yet

- **Order-book liquidity** (depth bands, impact estimates, liquidity surface) is implemented as library code with tests, but scheduled runs collect tier A only. Selecting a liquid universe needs a cross-venue pass before order books are fetched, which the current one-job-per-venue matrix does not do.
- **History-dependent metrics** (returns, percentiles, realised volatility, technical indicators, pressure/crowding vectors, genome, similarity search) need accumulated snapshots. Atlas starts with none, never fabricates any, and leaves these null until minimum observation counts are met.
- **Roll-ups** (hourly, monthly) are configured but not implemented; only daily compaction exists.
- **Options** are ingested from OKX and Deribit but do not yet produce surfaces, skew or term structure.
- **DEXs** are not supported.

## Some venues do not publish what Atlas needs

Coinbase, Kraken, Bitstamp and Bitfinex publish base volume only, so no reported USD volume is produced for them (they still price and weight). Spread is only available in bulk from venues whose bulk ticker carries best bid/ask.

## Thin markets

With one or two venues a median is barely robust. Single-venue assets are labelled (`single_venue_spot`); large dispersion in thin markets is common and reported, not smoothed. A small number of two-venue assets in a real run showed several-hundred bps disagreement.

## Storage and retention

Data lives in GitHub Releases. Release count and size are managed by daily compaction and guarded housekeeping, which are dry-run by default. Long-term roll-ups are not implemented, so long-run history is bounded by retention settings until they are.

## Legal

Code is Apache-2.0. Redistribution rights in venue data are venue-specific and **have not been reviewed by a lawyer**; see the [data policy](DATA_POLICY.md).

## Correctness of this software

Unit, contract and integration tests run against recorded fixtures and never depend on live venues. Live behaviour is checked by running the pipeline against real APIs, which found bugs unit tests missed (see the changelog); more will exist. Please report data-quality problems via the *data quality* issue template.
