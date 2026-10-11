# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/). Versions are separate concepts: the **software** version (this file), the **schema** version, the **methodology** version, and dataset **generation IDs**. `0.x` releases may make breaking changes; they are listed here.

## [Unreleased]

## [0.2.0]

Bybit and Binance derivatives, tier B and the history store wired into scheduled runs, the publication bug that stopped every collect run fixed, and the package prepared for PyPI as `vizanix-atlas`.

### Added
- **Bybit adapter** (V5: spot, linear, inverse) and **Binance derivatives adapter** (USD-M and COIN-M), written against payloads captured from the live APIs on 2026-10-11, with unit semantics cross-checked against the captures (inverse volume in contracts against base-coin turnover; COIN-M `contractSize`; Binance open interest published only per symbol, so the highest-volume contracts are sampled). Tokenised equities, ETFs, commodities and FX are skipped.
- **Relay** for venues that refuse the runners' network (`egress: relay` in `config/exchanges.yaml`): `atlas relay-publish` on a non-US host, `atlas fetch-relay` in the collect workflow. Pulled results are checked for https, a SHA-256 manifest, schema, venue identity and freshness; anything unusable becomes an explicit `failed` result. See `docs/RELAY.md`.
- **Tier B order books in scheduled runs.** Each published generation also publishes `books-plan.json`; the next run's collectors (runners and relay) fetch those books in the same pass as tier A (`atlas collect --books-plan`). Venues with no quote volume in bulk are ranked for selection by the asset's total volume.
- **History store** (`storage/history.py`): the 600 most liquid assets for 31 days in two alternating release assets, feeding the windowed metrics, funding/basis percentiles, pressure, crowding and the genome. Never overwritten from a failed read, never back-filled, read strictly before the snapshot being built.
- `vizanix-atlas` console script alongside `atlas`; `py.typed`; a text-first `PYPI_README.md`; trusted-publishing release workflow with a clean-environment smoke test of the built wheel on Linux, macOS and Windows (`docs/RELEASING.md`).
- README graphics generated from the repository's own configuration (`scripts/render_readme_assets.py`).

### Changed
- BitMEX stays disabled, now for the real reason: the exchange **ceased trading in September 2026**. The earlier suspicion of a network artefact is ruled out, and the recorded responses are test fixtures.
- Publication, history and the order-book plan run in one event loop, closed there.
- The HTTP user agent reports the package version.

### Fixed
- **Every scheduled collect run failed** at publication: downloading a release asset returns a 302 to signed storage, which the publisher did not follow (`PublicationFailure: could not download asset`), and the cleanup then raised `Event loop is closed`, hiding the cause. The test double now answers with a 302 like real GitHub, and a test checks that the token is not sent to the storage host.
- An ISO currency code that is only ever traded and never quoted against (RON is Ronin) was resolved as a currency, producing a nonsensical `asset:fiat:ron` with a -9,990 bp basis.

### Exchange adapters
- Enabled (18): the sixteen of 0.1.0, plus Bybit and Binance derivatives (relay).
- Disabled: BitMEX (closed).

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
