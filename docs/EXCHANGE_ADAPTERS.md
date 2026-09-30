# Exchange adapters

An adapter turns one venue's public API into normalised `Raw*` observations. It is plumbing: correctness beats connector count, and a venue that cannot be verified is disabled with a reason rather than shipped half-working.

## The contract

`ExchangeAdapter` (`adapters/base.py`) is an abstract base with capability-gated methods:

| Method | Returns |
|---|---|
| `discover_instruments()` | Every eligible active public instrument, from bulk endpoints |
| `fetch_tickers()` | A summary quote for every instrument, from bulk endpoints |
| `fetch_derivatives()` | Mark, index, funding and open interest, where the venue exposes them |
| `fetch_order_book(symbol, depth)` | One book snapshot |
| `server_time_ms()` | Optional clock probe (for skew diagnostics) |

Calling a method the venue does not support raises `UnsupportedCapability`; capabilities are declared in `config/exchanges.yaml` and asserted by contract tests. No venue-specific field name may cross the adapter boundary.

## Rules

- **Public, unauthenticated endpoints only.** No keys, no private APIs, no scraping of undocumented endpoints, no proxy rotation, no geo or CDN bypass, no browser impersonation.
- **Bulk first.** Never one request per symbol where a bulk endpoint exists. Tier A has a per-venue request ceiling (default 40) that degrades a venue that exceeds it.
- **Units normalised at the adapter**: timestamps to UTC epoch milliseconds (mapping event / server / receive time separately), sizes and volumes to documented units, funding to its native interval with explicit semantics. Never guess a contract multiplier.
- **Symbol collisions**: a venue that reuses one symbol across product lines (Gate.io and Bitget do) must set `instrument_class` on every observation; the index refuses to guess when it cannot disambiguate.
- **Errors are typed** (`NetworkTimeout`, `HttpError`, `RateLimited`, `GeoRestricted`, `ExchangeApplicationError`, `SchemaMismatch`, ...) and recorded per venue; bare `except Exception` is not used in the collection path.
- **Network safety**: timeouts, a response-body cap, JSON depth limit, token-bucket rate limiting with `Retry-After` support, exponential backoff with jitter, bounded concurrency and a per-run circuit breaker.

## Adding a venue

Follow the checklist in [CONTRIBUTING](../CONTRIBUTING.md#adding-an-exchange-adapter). In short: read the current official docs; capture a real, trimmed, header-free fixture and record its origin and date; implement and normalise; add the venue to `adapters/registry.py` and `tests/contract/wiring.py`; the universal contract tests then run against it automatically; review its data terms; declare capabilities; enable only after tests pass.

## Fixtures

`tests/fixtures/exchanges/<venue>/` holds trimmed real responses (`README.md` records capture time, egress region and what was kept, including deliberately awkward records such as null-valued option rows that must never be read as zero). Tests never call real venues.

## Verified behaviours worth knowing

Recorded in `config/exchanges.yaml` per venue: OKX volume units and option families requiring `instFamily`; Kraken Futures reporting funding as an absolute amount (converted to relative using the mark price, and cross-checked against the venue's own historical funding endpoint); Deribit's single `currency=any` discovery request and per-settlement-currency book summaries; Bitfinex `UST` meaning Tether; Crypto.com's `vv` field being USD turnover rather than quote volume.

## Status

Enabled (16): OKX, Binance (spot, via the market-data mirror), Coinbase, Kraken, Kraken Futures, Bitget, Gate.io, KuCoin, MEXC, HTX, Crypto.com, Deribit, Hyperliquid, dYdX v4, Bitstamp, Bitfinex. Disabled with reasons: Bybit, BitMEX, Binance derivatives ([DATA_SOURCES](DATA_SOURCES.md)).
