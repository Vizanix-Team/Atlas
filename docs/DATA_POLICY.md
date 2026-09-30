# Data policy

## Code and data are different things

The **code** is Apache-2.0. That license says nothing about **data**. Venue-derived data is governed by each venue's own terms, and Atlas cannot grant rights it does not hold.

## What Atlas publishes

Atlas does not publish raw ticks, raw order books, or raw API responses. It publishes derived, aggregated state: per-asset market state, and a per-venue summary row per asset (a representative price, reported 24h volume, funding, open interest, and how the venue entered the reference price). The per-venue rows are the most sensitive part: they are venue-derived observations at 15-minute granularity.

## Current status: not cleared

`config/exchanges.yaml` sets `redistributes_raw_observations: false` for every venue, the conservative default, and records terms URLs. **No legal review of any venue's terms has been done**, and the published per-venue summary rows may go beyond what some venues' terms allow. Until a maintainer reviews each venue, treat the published dataset as **for research use, with no data license granted by this project**, and do not redistribute it commercially. If you are a venue and object to the use of your public data, open an issue and it will be removed.

Maintainers should, before promoting the dataset as open data: review each venue's terms (record the reviewer and date), decide per venue whether per-venue rows may be published, and gate `venue_asset_state` by venue accordingly. The `redistributes_raw_observations` flag is recorded per venue but **is not enforced in code yet**: today every venue is `false` while per-venue rows are still published, which is exactly the inconsistency this review must resolve. A venue whose terms are unclear should default to being excluded from per-venue publication while still contributing to aggregates.

## Attribution

Recorded per venue where required (`attribution`), shown on the Exchanges page, and to be carried by any derived work.

## Personal data

None is collected. Atlas reads public market data only.

## Your responsibilities

Comply with the terms of every venue whose data you use and with the law where you are. Atlas's checks are not compliance advice.
