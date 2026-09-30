# Disclaimer

Vizanix Atlas is a research and infrastructure tool. It is **not investment advice**, a recommendation, an offer, or a solicitation, and nothing it publishes should be read as a signal to buy, sell or hold anything.

- **Exchange data can be wrong.** Atlas reads public endpoints and validates what it can, but venues publish errors, stale quotes, mislabelled units and unreliable volume. A value that passes Atlas's checks is not thereby correct.
- **Data is delayed.** Snapshots are scheduled (every 15 minutes at best) and GitHub can delay or skip scheduled runs. Atlas is not real time and must not be used for execution or latency-sensitive decisions.
- **No guarantee of completeness.** Venues, assets and instruments can be missing, unresolved or ambiguous, and the dataset can be partial. Coverage is reported in the data itself; read it.
- **Derived metrics are methodological estimates.** Reference prices, dispersion, basis, funding normalisation and any liquidity estimate depend on documented but debatable choices ([methodology](docs/METHODOLOGY.md)). "Observable order-book impact" is not slippage: fees, latency, hidden liquidity and market response are not modelled.
- **Reported volume is not verified volume.** Atlas does not detect wash trading.
- **Inclusion is not endorsement.** Listing a venue means Atlas can read its public data, not that Vizanix vouches for it, its solvency, its legality in your jurisdiction, or the quality of its markets.
- **Your use is your responsibility**, including compliance with venue terms and applicable law. See the [data policy](docs/DATA_POLICY.md).

The software is provided "as is", without warranty of any kind, as stated in the [Apache-2.0 license](LICENSE).
