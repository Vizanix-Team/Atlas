import { h } from "../dom";
import { loadDataset } from "../data";
import { num, utc } from "../format";
import { dataTable } from "../table";
import { DOCS, REPO } from "../links";
import { errorBox, pageHead } from "./common";

const p = (text: string) => h("p", {}, text);

export function methodology(main: HTMLElement): void {
  main.replaceChildren(
    pageHead("Methodology", "How Atlas turns venue observations into market state. The full, versioned specification lives in the repository."),
    h("h2", {}, "Reference price"),
    p("The reference price is a robust weighted median of venue prices converted to USD, not a simple average. Each venue's weight grows with reported volume but is capped so no single venue dominates. Observations that are stale, extreme, unconvertible or crossed are excluded, and every exclusion keeps its reason."),
    p("Atlas calls it a reference price, not a true price. When no qualifying spot venue exists, a derivative fallback may be used and is labelled as such."),
    h("h2", {}, "Quote currencies"),
    p("USDT, USDC, USD and other quote currencies are never assumed equal. Atlas builds a conversion graph from observed rates and keeps the original quote currency, the rate and its source next to every converted value. If no conversion is observable, the USD value is left empty."),
    h("h2", {}, "Volume"),
    p("Volume is reported trading volume: what venues publish, converted with the same graph. Atlas does not verify it, and it separates spot and perpetual volume rather than adding them silently."),
    h("h2", {}, "Dispersion"),
    p("Price dispersion is a robust spread of venue prices around the reference price, in basis points (weighted MAD and a P10 to P90 range). Wide dispersion describes data, and may reflect real fragmentation, stale quotes or venue-specific conditions."),
    h("h2", {}, "Funding, open interest and basis"),
    p("Funding rates keep their native interval and are also shown as an 8-hour equivalent; annualisation is simple, not compounded. Open interest is normalised to USD only when the contract multiplier and a reference price are reliable. Basis is (derivative reference − spot reference) / spot reference × 10,000, with the price source stated."),
    h("h2", {}, "History and warm-up"),
    p("Windowed metrics (changes, percentiles, volatility, behaviour fingerprints) need accumulated history. Atlas starts empty and never fabricates a past: these fields stay empty until enough scheduled snapshots exist, and a missed run is recorded as a gap rather than filled in."),
    h("h2", {}, "Not modelled"),
    p("Order-book impact estimates are labelled observable estimates: fees, latency, hidden liquidity and market response are not modelled. Nothing here is a signal or a recommendation."),
    h("p", {}, h("a", { href: `${DOCS}/METHODOLOGY.md` }, "Full methodology"), " · ", h("a", { href: `${DOCS}/LIMITATIONS.md` }, "Limitations"), " · ", h("a", { href: `${DOCS}/METRICS.md` }, "Metric dictionary")),
  );
}

export async function dataset(main: HTMLElement): Promise<void> {
  main.replaceChildren(pageHead("Dataset", "Every published generation is a set of Parquet files with a manifest and SHA-256 checksums, downloadable from GitHub Releases."));
  try {
    const d = await loadDataset();
    main.append(
      h("dl", { class: "card" }, ...([["Generation", d.generation_id], ["Published", utc(d.published_at)], ["Schema version", d.schema_version], ["Methodology version", d.methodology_version], ["Software version", d.software_version], ["Format version", d.dataset_format_version], ["Total size", `${num(d.total_bytes / 1048576, 1)} MiB`]] as [string, string][]).flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
      h("h2", {}, "Use it locally"),
      h("pre", { class: "code" }, "pip install vizanix-atlas\natlas status\natlas asset BTC\natlas query \"SELECT asset, venue_count FROM market ORDER BY venue_count DESC LIMIT 10\"\natlas verify <downloaded-dataset-dir>"),
      h("p", {}, "MQL runs on your machine against downloaded Parquet files. No Vizanix server is involved."),
      h("h2", {}, "Files"),
      dataTable(
        [
          { key: "f", label: "File", sort: (r: (typeof d.files)[number]) => r.filename, render: (r) => r.filename },
          { key: "t", label: "Table", secondary: true, sort: (r) => r.table, render: (r) => r.table ?? "" },
          { key: "r", label: "Rows", align: "right" as const, sort: (r) => r.rows, render: (r) => (r.rows === null ? "" : num(r.rows, 0)) },
          { key: "s", label: "Size", align: "right" as const, sort: (r) => r.size_bytes, render: (r) => `${num(r.size_bytes / 1024, 0)} KiB` },
          { key: "h", label: "SHA-256", secondary: true, render: (r) => h("code", { class: "small" }, r.sha256.slice(0, 16) + "…") },
        ],
        d.files,
        { caption: "Published files", initialSort: "s" },
      ),
      h("p", { class: "muted small" }, "Code is Apache-2.0. A code license is not a data license: read ", h("a", { href: `${DOCS}/DATA_POLICY.md` }, "the data policy"), " before redistributing venue-derived data."),
    );
  } catch (error) {
    main.append(errorBox(error));
  }
}

export function about(main: HTMLElement): void {
  main.replaceChildren(
    pageHead("About", "Vizanix Atlas defines what a global crypto market state is."),
    p("Atlas is an open, GitHub-native project: collection, aggregation, publication and this website all run on GitHub Actions, Releases and Pages. It needs no server, database or API key, and if every Vizanix-owned server disappeared Atlas would keep working."),
    p("It is a research and infrastructure tool. It does not trade, hold funds, manage accounts or give investment advice, and it does not use private endpoints."),
    p("Maintained by Vizanix, which builds algorithmic trading infrastructure: exchange integrations, execution and market-data systems, and quantitative tooling."),
    h("p", {}, h("a", { href: REPO }, "Source repository"), " · ", h("a", { href: `${DOCS}/LIMITATIONS.md` }, "Limitations"), " · ", h("a", { href: `${REPO}/blob/main/DISCLAIMER.md` }, "Disclaimer"), " · ", h("a", { href: `${REPO}/blob/main/SECURITY.md` }, "Security")),
  );
}
