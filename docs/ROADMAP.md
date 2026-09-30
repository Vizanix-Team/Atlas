# Roadmap

No dates. Items move only when they are done and tested. Status is stated honestly: *built* means implemented and tested; *designed* means specified in the code's data model or config but not yet computed from real data.

## Phase 1 — Universal normalisation *(built)*
Public adapters for 16 venues, instrument normalisation, conservative identity resolution, quote-conversion graph, validation and quarantine.

## Phase 2 — Global market state *(built, expanding)*
Reference price, dispersion, reported volume, funding/OI/basis, fragmentation, sharded Parquet publication with a validity gate, SDK, CLI, MQL, static website.
Next: run the scheduled workflows and measure GitHub-hosted runner behaviour; widen asset pooling (more contract-address and curated evidence); per-venue redistribution gating once terms are reviewed.

## Phase 3 — Liquidity surface and advanced derivatives *(library built, not collected)*
Order-book depth, impact estimates and the liquidity surface exist as tested library code. Missing: a two-phase collection (tier A across venues, then deterministic tier B selection, then book fetches) so books are requested only for a cross-venue liquid universe. Also: a `derivative_observations` table; option surface, term structure and skew from Deribit/OKX data; liquidation feeds where semantics are compatible.

## Phase 4 — MQL and historical metrics *(MQL built; history designed)*
Needs accumulated snapshots. Then: returns, realised volatility, funding/basis percentiles with explicit window coverage, and `CHANGE` / `PERCENTILE` / `COVERAGE` MQL functions backed by real history; hourly and monthly roll-ups; a storage-growth report.

## Phase 5 — Market Genome and similarity *(designed)*
Behavioural fingerprints with lifecycle `insufficient_history → warming_up → available` (28 days of history before `available`), and `similar_to` over documented normalised features. Never presented as investment similarity.

## Phase 6 — DEX and options expansion *(not started)*
AMM pools are not forced into order-book semantics; options get their own capability-driven schema.

## Housekeeping items
- `redistributes_raw_observations` enforcement and a legal review of venue terms.
- Bybit, BitMEX and Binance derivatives adapters, if verifiable fixtures can be captured.
- Golden-schema compatibility test that compares against the previous released schema (today CI checks that generated schemas match the committed ones).
- In-browser MQL (DuckDB-WASM) is optional and not planned before the Python implementation is settled.

## Non-goals
Not an exchange emulator, backtesting engine, execution engine, paper-trading system, trading bot, brokerage, wallet, portfolio manager, or advice product; no private endpoints, accounts, or API-key management; no token, NFT or blockchain component; no Vizanix-hosted backend.
