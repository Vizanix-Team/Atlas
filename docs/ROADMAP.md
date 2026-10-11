# Roadmap

No dates. Items move only when they are done and tested. Status is stated honestly: *built* means implemented and tested; *designed* means specified in the code's data model or config but not yet computed from real data.

## Phase 1 — Universal normalisation *(built)*
Public adapters for 18 venues (16 collected by CI runners, Bybit and Binance derivatives through the [relay](RELAY.md)), instrument normalisation, conservative identity resolution, quote-conversion graph, validation and quarantine. BitMEX closed in September 2026 and stays listed, disabled.

## Phase 2 — Global market state *(built, expanding)*
Reference price, dispersion, reported volume, funding/OI/basis, fragmentation, sharded Parquet publication with a validity gate, SDK, CLI, MQL, static website.
Next: run the scheduled workflows and measure GitHub-hosted runner behaviour; widen asset pooling (more contract-address and curated evidence); per-venue redistribution gating once terms are reviewed.

## Phase 3 — Liquidity and advanced derivatives *(tier B built; surfaces not)*
Order-book depth bands and impact estimates are collected as tier B: after each published generation a deterministic plan names the liquid markets to snapshot, and every collector fetches those books in the same pass as its tier A data. Still to do: the liquidity *surface*; a `derivative_observations` table; option surface, term structure and skew from Deribit/OKX data; liquidation feeds where semantics are compatible.

## Phase 4 — MQL and historical metrics *(MQL and history store built; accumulating)*
A history store (the 600 most liquid assets, 31 days, two alternating release assets) now feeds returns, realised volatility, funding/basis percentiles, pressure and crowding. It accumulates from the first published generation, and each metric appears once its minimum observations exist. Still to do: `CHANGE` / `PERCENTILE` / `COVERAGE` MQL functions backed by that history; hourly and monthly roll-ups; a storage-growth report.

## Phase 5 — Market Genome and similarity *(designed)*
Behavioural fingerprints with lifecycle `insufficient_history → warming_up → available` (28 days of history before `available`), and `similar_to` over documented normalised features. Never presented as investment similarity.

## Phase 6 — DEX and options expansion *(not started)*
AMM pools are not forced into order-book semantics; options get their own capability-driven schema.

## Housekeeping items
- `redistributes_raw_observations` enforcement and a legal review of venue terms.
- A second relay host, so that a single relay outage does not remove Bybit and Binance derivatives from a generation.
- Per-contract open interest for Binance in bulk, if the venue ever publishes it.
- Golden-schema compatibility test that compares against the previous released schema (today CI checks that generated schemas match the committed ones).
- In-browser MQL (DuckDB-WASM) is optional and not planned before the Python implementation is settled.

## Non-goals
Not an exchange emulator, backtesting engine, execution engine, paper-trading system, trading bot, brokerage, wallet, portfolio manager, or advice product; no private endpoints, accounts, or API-key management; no token, NFT or blockchain component; no Vizanix-hosted backend.
