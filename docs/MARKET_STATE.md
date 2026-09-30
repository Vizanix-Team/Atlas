# Universal Market State

`MarketState` is the core object: everything observable about one canonical asset across venues in one generation, as nested typed sections rather than hundreds of flat fields. The JSON Schema is published in [`schemas/json/market_state.schema.json`](../schemas/json/market_state.schema.json) so other projects can produce or consume it without importing this package.

| Section | Contents | Status |
|---|---|---|
| `asset_id`, `symbol`, timestamps, versions | Identity and when/how it was computed | Populated |
| `reference_price` | Value, method, venue counts, min/max qualified price, provenance | Populated |
| `returns` | Changes over 1h/24h/7d | Needs history |
| `volume` | Reported spot / perpetual / futures volume (USD) | Populated where venues report |
| `volatility` | Realised volatility, ATR | Needs history |
| `liquidity` | Spread, depth bands, per-venue liquidity, concentration | Library ready; not collected yet |
| `structure` | Spot/perp/futures venue counts | Populated |
| `funding`, `open_interest`, `basis` | Normalised derivatives state | Populated where venues report |
| `dispersion` | Price dispersion (robust) | Populated |
| `fragmentation` | Effective venue count, concentration | Populated |
| `technical` | RSI, EMA and similar (numbers only) | Needs history |
| `pressure`, `crowding` | Component *vectors*, never a single label | Needs history |
| `genome` | Behavioural fingerprint with lifecycle `insufficient_history` / `warming_up` / `available` | Insufficient history at launch |
| `quality` | Coverage, freshness, exclusions, partial flag | Populated |

## Vectors, not verdicts

`pressure` and `crowding` are vectors of named components (for example funding deviation, OI change, basis deviation), each explicitly present or absent. Atlas does not collapse them into "High" or a `/100` score.

## Provenance

`state.reference_price` carries references to the venue observations behind it (venue, instrument, price, weight, inclusion, exclusion reason, conversion used), so a published number can be audited from the dataset alone. The per-venue decomposition is also published as the `venue_asset_state` table.

## Flat form

Parquet stores each state as one flat row: nested sections become `section__field` columns (for example `reference_price__value`), and sequences become JSON-string columns. `atlas.asset(x).state()` rebuilds the nested tree; MQL uses friendly names (`reference_price`) mapped to those columns.

## Market-wide state

A market-wide view needs a *universe*. Universes are defined in `config/collection.yaml` (all resolved, multi-venue, liquid, top-N by depth, spot-only, derivative-active) with documented criteria and counts. The website's overview and `atlas market` summarise the assets in the current dataset; liquidity-ranked universes depend on order-book collection (see [roadmap](ROADMAP.md)).
