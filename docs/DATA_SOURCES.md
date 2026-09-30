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
| Binance Derivatives | disabled | n/a | [docs](https://developers.binance.com/docs/derivatives) | not recorded | n/a | no |
| BitMEX | disabled | n/a | [docs](https://www.bitmex.com/app/apiOverview) | not recorded | n/a | no |
| Bybit | disabled | n/a | [docs](https://bybit-exchange.github.io/docs/v5/intro) | not recorded | n/a | no |

"Terms URL recorded on" is the date the URL was recorded in `config/exchanges.yaml`. **It is not a legal review.** Terms change; maintainers must re-check them before treating any redistribution as cleared ([DATA_POLICY](DATA_POLICY.md)).

## Disabled adapters (and why)

- **Bybit** — every API host returned HTTP 403 from a CloudFront edge ("configured to block access from your country") from the network used to build 0.1.0, so no payload could be captured and no parser could honestly be written. It may work from other networks; it is disabled until a fixture can be captured.
- **BitMEX** — reachable, but the catalogue seen was not representative (12 placeholder spot pairs dated 2200, empty `activeIntervals`, the flagship `XBTUSD` reported as settled). Enabling an adapter on that would publish wrong data.
- **Binance derivatives** — `fapi`/`dapi` returned HTTP 451 (jurisdiction restriction) with no market-data-only mirror. Binance is a very large derivatives venue, so funding and open-interest coverage is materially lower without it.

Spot Binance is collected through Binance's documented market-data host (`data-api.binance.vision`).

## Network availability

Availability was observed from one United States egress on 2026-09-27 (`tests/fixtures/exchanges/AVAILABILITY.md`). It is an observation from one network, not a judgement about any venue. Availability from GitHub-hosted runners is unknown until scheduled runs happen; a venue that blocks them is reported as `unavailable_from_collector_network` and never worked around.

## Attribution

Where a venue's terms require attribution, it is recorded in `config/exchanges.yaml` (`attribution`) and shown on the Exchanges page. Currently none is recorded; that field must be filled in if a venue's terms require it.
