# Asset resolution

**A shared ticker is not a shared asset.** `UST` is Tether on Bitfinex and has been TerraUSD elsewhere; `ARB`, `MASK`, `ONE`, `PAY` and `MAGIC` have each been used by unrelated projects. Merging on ticker would silently combine unrelated tokens into one price, one volume and one liquidity picture. A false merge is far more dangerous than an unresolved identity, so Atlas prefers *ambiguous* to *wrong*.

## Identifiers

Deterministic, stable and schema-versioned:

| Kind | Example |
|---|---|
| Venue | `venue:okx` |
| Native asset | `asset:native:bitcoin:BTC` |
| EVM token | `asset:evm:1:0xdac17f958d2ee523a2206206994597c13d831ec7` (chain id, lower-cased address) |
| Fiat | `asset:fiat:usd` |
| Unresolved, venue-scoped | `asset:unresolved:kraken:ARB` |
| Instrument | `instrument:bybit:linear-perp:BTCUSDT` |

## Resolution order

The first rule that settles an identity wins:

1. **Manual override** in `config/asset_overrides.yaml`. Every entry carries `reason`, `source`, `date` and `author`; an entry without them fails to load.
2. **Contract address** published by the venue. Decisive: it names the asset.
3. **ISO 4217 code** for government currencies.
4. A ticker claimed by **exactly one declared canonical asset**.
5. A **curated native-chain ticker** for coins with no contract address.
6. An **asset name at least two venues agree on**, plus a matching ticker.
7. Otherwise: a **venue-scoped identity**, which is never aggregated with any other venue's asset.

Name normalisation only lower-cases and strips punctuation. It does not drop words like "coin" or "token", because "USD Coin" must not collapse to "usd" and match the US dollar.

## States

| State | Meaning | Aggregated across venues? |
|---|---|---|
| `resolved` | Settled by evidence above | Yes |
| `manual_override` | Settled by a curated override | Yes |
| `probable` | Strong but incomplete evidence | No |
| `ambiguous` | More than one plausible identity | No |
| `unresolved` | No evidence; venue-scoped identity | No |

## Never-merge rules

`never_merge` entries (currently `UST`, `ARB`, `MASK`, `PAY`, `ONE`, `MAGIC`), each with reason, source, date and author, force venue-scoped identities for those tickers regardless of other evidence.

## Wrapped and bridged assets

Relationships are typed edges (`wrapped_of`, `bridged_representation_of`, `tracks`, `settles_in`, `quoted_in`, `underlying`). Wrapped and bridged assets are never collapsed into the native asset: WBTC `wrapped_of` BTC is an edge, not a merge.

## Consequences you will see

- ~169 of ~12,100 assets in a real run appear on two or more venues. Most of the rest are venue-scoped: correct but under-pooled. This is the price of never guessing, and coverage of pooled assets grows as evidence (contract addresses, curated mappings) is added.
- `Atlas.asset("ARB")` raises `AmbiguousAsset` listing the candidate IDs instead of choosing one. `atlas search` is for discovery; a ticker is a convenience, not an identity. Use the full asset ID when you need certainty.

## Adding a mapping

Open an *asset resolution* issue or PR that edits `config/asset_overrides.yaml` with the evidence (contract address, chain, a venue's own asset metadata). See [CONTRIBUTING](../CONTRIBUTING.md).
