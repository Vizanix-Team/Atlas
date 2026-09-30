# Benchmarks

Measured, not targeted. All numbers below come from one real run on 2026-09-30 in a 4-vCPU, 16 GB Linux container from a cloud container's egress (not a GitHub-hosted runner), Python 3.11, against the 16 live public venue APIs. Re-measure on your own hardware; GitHub-hosted runner numbers will be added after the first scheduled runs.

## Collection (16 venues, tier A)

| Metric | Value |
|---|---|
| Wall time, all venues concurrently | ~9.8 s |
| Venues succeeded | 16 / 16 |
| Instruments discovered | 21,804 (after de-duplication) |
| Raw response bytes received | ~62.5 MB |
| Requests per venue | 3 to 19 (ceiling 40) |
| Serialised `CollectionResult` JSON | ~48 MB |

Per venue (instruments / tickers): Deribit 5,520 / 7,633; Bitget 4,240 / 4,248; OKX 3,484 / 4,814; Gate.io 3,253 / 3,255; MEXC 1,819 / 1,903; Kraken 1,355 / 1,445; Binance spot 1,367 / 3,716; Kucoin 996 / 996; Crypto.com 982 / 982; HTX 594 / 598; Coinbase 512 / 513; Krakenfutures 300 / 300; Bitstamp 256 / 257; Bitfinex 194 / 287; Hyperliquid 178 / 178; dYdX 78 / 78.

## Aggregation and dataset build (`atlas build-dataset`)

| Metric | Value |
|---|---|
| Wall time (resolve, normalise, analytics, write, gate, publish locally) | ~16.7 s |
| Peak memory | ~1.18 GiB |
| Assets with a published state | 12,113 |
| Files | 70 (64 shards + 6 tables) |
| Dataset size (Zstd Parquet) | 7.4 MB |
| Largest files | `venue_asset_state` 607 KB, `instruments` 439 KB, `assets` 140 KB, one shard ≈ 97 KB |

## Client operations (local, warm files)

| Command | Wall time | Notes |
|---|---:|---|
| `atlas --help` | 0.65 s | Python start-up + imports dominate |
| `atlas asset BTC` | 0.61 s | Reads the `assets` table and one shard |
| `atlas query` (all 64 shards) | 0.68 s | DuckDB over 64 Parquet files |
| `atlas verify` | 0.58 s | Re-hashes all 70 files |

Peak child RSS across these client commands stayed under 150 MiB.

## Website

Export for the site: 204 JSON files, ~6.2 MB (budget 25 MB); JS bundle ~27 kB (9.7 kB gzipped), CSS ~5 kB.

## Test suite

~357 tests run in ~30 s on the same machine (the fake-venue failure tests spend most of it in retry back-off).

## Not yet measured

GitHub-hosted runner timings; release-asset upload times; growth in release count and total bytes over weeks (a `StorageReport` model exists for this, but nothing produces it yet); order-book collection (not enabled).
