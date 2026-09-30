# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/). Versions are separate concepts: the **software** version (this file), the **schema** version, the **methodology** version, and dataset **generation IDs**. `0.x` releases may make breaking changes; they are listed here.

## [Unreleased]

## [0.1.0]

First usable release.

### Added
- Universal Market Schema (Pydantic v2), published JSON Schemas and Parquet layouts (`schemas/`), generated and drift-checked in CI.
- Asset graph with conservative identity resolution and a version-controlled override file with per-entry provenance.
- Quote-conversion graph built from observed rates, with a stablecoin depeg guard.
- Analytics: robust weighted-median reference price with weight caps and recorded exclusions, dispersion, reported volume (spot, perpetual, futures kept apart), funding normalised to 8h, open interest, basis, market fragmentation.
- Sharded Parquet dataset, manifest with per-file SHA-256, validity gate with anomaly guards, last-known-good publication, filesystem and GitHub Releases publishers.
- Daily compaction with explicit gap recording and round-trip verification; guarded housekeeping (dry-run by default).
- Python SDK (`Atlas.latest/from_local/from_generation`) with lazy, checksum-verified downloads; `atlas` CLI; Market Query Language (lexer, parser, semantic validation against the metric registry, safe DuckDB compilation).
- Static website (TypeScript/Vite) and `atlas export-web`.
- GitHub Actions: CI, collect, compact-daily, maintenance, discovery probe, Pages, security, release.

### Exchange adapters
- Enabled (16): OKX, Binance (spot), Coinbase, Kraken, Kraken Futures, Bitget, Gate.io, KuCoin, MEXC, HTX, Crypto.com, Deribit, Hyperliquid, dYdX v4, Bitstamp, Bitfinex.
- Disabled with documented reasons: Bybit (geo-blocked from the build network), BitMEX (unrepresentative catalogue), Binance derivatives (HTTP 451).

### Fixed (found by running against live venues)
- Contract expiry dates were rejected by the observation-freshness window, silently dropping most options and dated futures.
- Gate.io and Bitget reuse one symbol string for spot and derivative products; observations attached to the wrong instrument. Lookups now carry an instrument class and refuse to guess.
- Crypto.com's `vv` field is USD turnover, not quote volume, and was double-converted.
- Options were treated as reference-price candidates; an option premium is not the underlying's price.
- USD-native venues (which publish only base volume) received minimum reference weight.
- Per-venue summary rows used whichever instrument a venue listed first, showing unconvertible pairs or option premiums. Rows now use a deterministic representative.
- Expected daily compaction slots did not match the floored generation slots.

### Methodology
- `1.0.0`. See [docs/METHODOLOGY.md](docs/METHODOLOGY.md).

### Schema
- `1.0.0`.

### Known gaps
- Order-book (tier B/C) liquidity is not collected in scheduled runs yet.
- Historical/windowed metrics, crowding, market genome and similarity search need accumulated history and are not computed yet.
