# Vizanix Atlas

**The semantic layer for global crypto markets.** One reproducible, typed market state per asset across 18 venues, with coverage, quality and provenance shown next to every number.

Crypto trades on hundreds of venues that disagree about symbols, quote currencies, contract sizes, funding intervals and what "24h volume" means. Atlas turns those public observations into one market state per asset: a robust cross-venue reference price, dispersion, reported volume, funding, open interest, basis and liquidity, each with the venues behind it and the ones left out.

> Exchanges provide events. Atlas provides market state.

## Install

```bash
pip install vizanix-atlas
```

Installs the `atlas` command (also `vizanix-atlas`) and the `vizanix_atlas` package. Python 3.11+, no credentials, no Docker.

## Use

```python
from vizanix_atlas import Atlas

atlas = Atlas.latest()          # downloads, caches and checksum-verifies the newest generation

btc = atlas.asset("BTC")
btc.reference_price             # capped, volume-weighted robust median, not one exchange's last trade
btc.venue_count                 # venues that contributed
btc.state().open_interest       # contracts converted with each venue's own contract size
btc.raw()                       # every published field
```

```bash
atlas status
atlas asset BTC
atlas market --top 15
atlas query "SELECT asset, venue_count FROM market ORDER BY venue_count DESC LIMIT 10"
```

## Live prices

```bash
atlas live BTC ETH SOL
```

```python
from vizanix_atlas.sdk.live import LiveClient

live = LiveClient()                         # the project's instance, or $ATLAS_LIVE_URL
live.price("BTC")                           # reference price in USD, current to the second
for update in live.stream(["BTC", "ETH"]):  # Server-Sent Events, reconnects on its own
    print(update["symbol"], update["price"], update["venues"])
```

Run your own with `pip install "vizanix-atlas[stream]"` and `atlas stream serve`.

## What it is, and is not

- **Public data only.** No accounts, no API keys, no private endpoints.
- **Datasets are scheduled; the live layer is not.** Generations are best effort every 15 minutes at most and gaps are recorded, never filled. The live layer streams a current reference price between them. It is a moving estimate, not a dataset, and not for execution.
- **Reported volume is not verified volume.** It is labelled as reported everywhere.
- **Conservative identity.** Two tokens sharing a ticker stay apart unless evidence says they are one.
- **Not investment advice.**

## Links

- Source, graphics and full documentation: <https://github.com/Vizanix-Team/Atlas>
- Methodology and limitations: <https://github.com/Vizanix-Team/Atlas/tree/main/docs>
- Changelog: <https://github.com/Vizanix-Team/Atlas/blob/main/CHANGELOG.md>
- Issues: <https://github.com/Vizanix-Team/Atlas/issues>

Code is Apache-2.0. A code license is not a data license: venue-derived data is governed by each venue's terms.

Maintained by [Vizanix](https://vizanix.tech).
