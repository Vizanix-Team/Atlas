# Data sources

Every source is a venue's own public, unauthenticated market-data endpoint. Atlas does not depend on third-party aggregators (CoinGecko, CoinMarketCap and similar) as a source of truth.

| Venue | Status | Products | Documentation | Terms | Terms URL recorded on | Republishes raw observations |
|---|---|---|---|---|---|---|
| Binance | enabled | spot | [docs](https://developers.binance.com/docs/binance-spot-api-docs) | [link](https://www.binance.com/en/terms) | 2026-09-27 | no |
| Bitfinex | enabled | spot | [docs](https://docs.bitfinex.com/docs/rest-public) | [link](https://www.bitfinex.com/legal/terms) | 2026-09-27 | no |
| Bitget | enabled | spot, perpetual, future | [docs](https://www.bitget.com/api-doc/common/intro) | [link](https://www.bitget.com/en/support/articles/360007255754) | 2026-09-27 | no |
| Bitstamp | enabled | spot | [docs](https://www.bitstamp.net/api/) | [link](https://www.bitstamp.net/terms-of-use/) | 2026-09-27 | no |
| Coinbase Exchange | enabled | spot | [docs](https://docs.cdp.coinbase.com/exchange/docs/welcome) | [link](https://www.coinbase.com/legal/market_data) | 2026-09-27 | no |
| Crypto.com Exchange | enabled | spot, perpetual, future | [docs](https://exchange-docs.crypto.com/exchange/v1/rest-ws/index.html) | [link](https://crypto.com/exchange/document/terms-and-conditions) | 2026-09-27 | no |
| Deribit | enabled | spot, perpetual, future, option | [docs](https://docs.deribit.com/) | [link](https://www.deribit.com/kb/deribit-rules) | 2026-09-27 | no |
| dYdX v4 | enabled | perpetual | [docs](https://docs.dydx.xyz/) | [link](https://dydx.trade/terms) | 2026-09-27 | no |
| Gate.io | enabled | spot, perpetual | [docs](https://www.gate.com/docs/developers/apiv4/) | [link](https://www.gate.com/docs/developers/apiv4/) | 2026-09-27 | no |
| HTX | enabled | spot | [docs](https://huobiapi.github.io/docs/spot/v1/en/) | [link](https://www.htx.com/en-us/support/) | 2026-09-27 | no |
| Hyperliquid | enabled | spot, perpetual | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api) | [link](https://hyperliquid.gitbook.io/hyperliquid-docs) | 2026-09-27 | no |
| Kraken | enabled | spot | [docs](https://docs.kraken.com/api/docs/rest-api/get-ticker-information) | [link](https://www.kraken.com/legal) | 2026-09-27 | no |
| Kraken Futures | enabled | perpetual, future | [docs](https://docs.kraken.com/api/docs/futures-api/trading/get-tickers) | [link](https://www.kraken.com/legal) | 2026-09-27 | no |
| KuCoin | enabled | spot | [docs](https://www.kucoin.com/docs-new/) | [link](https://www.kucoin.com/legal/terms-of-use) | 2026-09-27 | no |
| MEXC | enabled | spot | [docs](https://mexcdevelop.github.io/apidocs/spot_v3_en/) | [link](https://www.mexc.com/user-agreement) | 2026-09-27 | no |
| OKX | enabled | spot, perpetual, future, option | [docs](https://www.okx.com/docs-v5/en/) | [link](https://www.okx.com/docs-v5/en/#overview) | 2026-09-27 | no |
| Binance Derivatives | enabled (relay) | perpetual, future | [docs](https://developers.binance.com/docs/derivatives) | [link](https://www.binance.com/en/terms) | 2026-10-11 | no |
| Bybit | enabled (relay) | spot, perpetual, future | [docs](https://bybit-exchange.github.io/docs/v5/intro) | [link](https://www.bybit.com/en/help-center/article/Terms-of-Service) | 2026-10-11 | no |
| BitMEX | disabled (closed) | n/a | [docs](https://www.bitmex.com/app/apiOverview) | not recorded | n/a | no |

"Terms URL recorded on" is the date the URL was recorded in `config/exchanges.yaml`. **It is not a legal review.** Terms change; maintainers must re-check them before treating any redistribution as cleared ([DATA_POLICY](DATA_POLICY.md)).

## Relay venues

**Bybit** and **Binance derivatives** (USD-M `fapi`, COIN-M `dapi`) refused the United States address they were probed from (HTTP 403 from Bybit's CDN edge, HTTP 451 from Binance), and neither has a market-data-only mirror. GitHub-hosted runners also egress from United States ranges, so the same refusal is what to expect there. Both were disabled in 0.1.0 because no payload could be captured from the network used to build it.

From 0.2.0 they are enabled and collected from a non-US network with the same adapters and result format; the workflow pulls the validated result. This was checked on 2026-10-11: every endpoint the adapters use answered HTTP 200 from a European network, the payloads were recorded as fixtures, and unit semantics (for example Bybit's inverse `volume24h` in contracts against `turnover24h` in the base coin, or Binance COIN-M `volume` in contracts of 100 USD against `baseVolume` in BTC) were cross-checked against the captures. See [the relay](RELAY.md) for what is checked on pull.

Two coverage notes that follow from the venues' own APIs:

- Binance publishes open interest only **per symbol**. Atlas samples the highest-volume contracts (15 USD-M, 10 COIN-M) and leaves open interest empty for the rest, never zero. That keeps the venue inside the tier A request ceiling.
- Tokenised equities, ETFs, commodities and FX listed as contracts (Bybit `symbolType` `stock`/`ETF`/`commodity`/`forex`/`xstocks`, Binance `TRADIFI_PERPETUAL`) are skipped.

Spot Binance is collected through Binance's documented market-data host (`data-api.binance.vision`).

## Disabled adapters (and why)

- **BitMEX** — **closed.** BitMEX's own announcements (`bitmex.com/blog/bitmex-closure`, and "[Notice] BitMEX Exchange Now Closed" in `/api/v1/announcement`) state that the exchange ceased all trading in September 2026. Re-checked on 2026-10-11 from an unrestricted network: `/api/v1/instrument/active` lists only 12 unlisted placeholder spot pairs dated 2200, every other instrument (XBTUSD included, expiry 2026-09-16) is `Settled`, the order book is empty and the last XBTUSD trade is that settlement. An earlier suspicion that this was a network artefact is ruled out. There is no live market, so an adapter would publish nothing true; the recorded responses are test fixtures that prove it (`tests/fixtures/exchanges/bitmex/`).

## Network availability

Availability depends on where Atlas runs, and it is an observation, not a judgement about a venue. The 2026-09-27 observations from one United States egress are in `tests/fixtures/exchanges/AVAILABILITY.md`; the 2026-10-11 observations from a European egress are described above. A venue that refuses a collector network is reported as `unavailable_from_collector_network` and is never reached through a proxy or any other disguise. Venues declared `egress: relay` are the ones collected from a network where they answer normally.

## Attribution

Where a venue's terms require attribution, it is recorded in `config/exchanges.yaml` (`attribution`) and shown on the Exchanges page. Currently none is recorded; that field must be filled in if a venue's terms require it.
