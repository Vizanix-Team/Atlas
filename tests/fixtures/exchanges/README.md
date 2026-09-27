# Exchange fixtures

Every file in this directory is a **trimmed capture of a real response** from the
venue's official public API. Fixtures exist so that adapter parsers can be tested
without contacting a live exchange (see `docs/EXCHANGE_ADAPTERS.md`).

## Capture metadata

| Field | Value |
| --- | --- |
| Captured at | 2026-09-27, approximately 17:30 UTC |
| Captured from | Public endpoints only, unauthenticated |
| Egress region | United States (observed CloudFront PoP `IAD61`) |
| Trimming | Records reduced to a representative subset; order books truncated to 15 levels per side |
| Sanitisation | No request or response headers retained; no credentials were used or captured |

Fixtures are **reformatted** (indented, key-sorted) so that diffs stay readable.
Field names, field presence and value semantics are unmodified.

## Why records were kept

Each fixture retains BTC, ETH and a stablecoin pair where the venue lists them,
plus deliberately awkward records:

- `okx/instruments_swap.json` keeps `BTC-USD-SWAP` (inverse, `ctVal` denominated
  in USD) alongside `BTC-USDT-SWAP` (linear, `ctVal` denominated in BTC).
- `deribit/book_summary_btc.json` keeps options with `null` `last`, `high` and
  `low`, which is the venue's representation of "no trades in the window". These
  must never be read as zero.
- `deribit/instruments_btc.json` keeps a `future_combo` record, which Atlas
  excludes from asset aggregation because a calendar spread is not an outright
  instrument.
- `bitfinex/conf_currency_maps.json` keeps the venue's own currency label and
  symbol maps, which is the evidence Atlas uses to resolve Bitfinex's
  venue-specific contractions (`UST` is Tether, `ALG` is Algorand).
- `krakenfutures/historical_funding_*.json` keeps consecutive hourly funding
  records for one linear and one inverse perpetual. These are the records used
  to verify Kraken Futures' absolute-to-relative funding conversion; see
  `docs/METHODOLOGY.md`.

## Refreshing a fixture

Fixtures are checked in deliberately and are not regenerated automatically. When
a venue changes its schema, capture a new response, trim it the same way, and
record the new capture date here. Schema drift that breaks a parser should be
visible as a failing contract test, not silently absorbed.
