# Venue availability observed during fixture capture

Recorded 2026-09-27 from a United States egress address (CloudFront PoP `IAD61`).
This is an observation from one network, not a judgement about any venue. See
`docs/LIMITATIONS.md` and `docs/DATA_SOURCES.md`.

| Venue | Endpoint probed | Result | Interpretation |
| --- | --- | --- | --- |
| Binance (spot, `data-api.binance.vision`) | `/api/v3/time` | `200` | Usable |
| Binance (`api.binance.com`, `api1`, `api-gcp`) | `/api/v3/time` | `451` | Venue-side jurisdiction restriction |
| Binance USD-M futures (`fapi.binance.com`) | `/fapi/v1/time` | `451` | Venue-side jurisdiction restriction |
| Binance COIN-M futures (`dapi.binance.com`) | `/dapi/v1/time` | `451` | Venue-side jurisdiction restriction |
| Bybit (`api.bybit.com`, `api.bytick.com`, `api.bybit.nl`) | `/v5/market/time` | `403` | CloudFront country block |
| BitMEX | `/api/v1/instrument/active` | `200`, non-representative body | Returned 12 unlisted placeholder pairs; `XBTUSD` reported `state: Settled`; `activeIntervals` empty |
| OKX | `/api/v5/public/time` | `200` | Usable |
| Coinbase Exchange | `/time` | `200` | Usable |
| Kraken | `/0/public/Time` | `200` | Usable |
| Kraken Futures | `/derivatives/api/v3/instruments` | `200` | Usable |
| Bitget | `/api/v2/public/time` | `200` | Usable |
| Gate.io | `/api/v4/spot/time` | `200` | Usable |
| KuCoin | `/api/v1/timestamp` | `200` | Usable |
| MEXC | `/api/v3/time` | `200` | Usable |
| HTX | `/v1/common/timestamp` | `200` | Usable |
| Crypto.com | `/exchange/v1/public/get-instruments` | `200` | Usable |
| Deribit | `/api/v2/public/get_time` | `200` | Usable |
| Hyperliquid | `/info` (POST) | `200` | Usable |
| dYdX v4 indexer | `/v4/perpetualMarkets` | `200` | Usable |
| Bitstamp | `/api/v2/ticker/` | `200` | Usable |
| Bitfinex | `/v2/platform/status` | `200` | Usable |

The `451` and `403` responses were returned by the venues' own edge (CloudFront),
not by any intermediary: the session's egress proxy reported no relay failures for
those requests. Atlas therefore treats them as
`collection_status = unavailable_from_collector_network` rather than as adapter
faults, and never retries past a circuit-breaker trip.

GitHub-hosted runners egress from Microsoft Azure ranges, which are not the same
addresses used for this capture. A venue unavailable here may be available from
Actions and the reverse is also possible. `atlas exchanges --probe` re-measures
availability from wherever it is run.
