import { h } from "../dom";
import { loadAssets, loadOverview, type AssetRow } from "../data";
import { bps, price, signedPct, usd } from "../format";
import { dataTable } from "../table";
import { DOCS } from "../links";
import { errorBox, fixtureBanner, pageHead } from "./common";

interface Filters { minVenues: number; minVolume: number; minDisp: number; maxDisp: number; funding: "any" | "positive" | "negative" | "available" }

function field(id: string, label: string, value: string, step = "any"): [HTMLElement, HTMLInputElement] {
  const input = h("input", { id, type: "number", value, step, min: "0", inputmode: "decimal" });
  return [h("div", { class: "field" }, h("label", { for: id }, label), input), input];
}

function toMql(f: Filters): string {
  const where = [`venue_count >= ${f.minVenues}`];
  if (f.minVolume > 0) where.push(`reported_volume_24h_usd >= ${f.minVolume}`);
  if (f.minDisp > 0) where.push(`price_dispersion_bps >= ${f.minDisp}`);
  if (Number.isFinite(f.maxDisp)) where.push(`price_dispersion_bps <= ${f.maxDisp}`);
  if (f.funding === "positive") where.push("funding_rate_8h_median > 0");
  if (f.funding === "negative") where.push("funding_rate_8h_median < 0");
  if (f.funding === "available") where.push("funding_rate_8h_median IS NOT NULL");
  return `SELECT asset, reference_price, venue_count, reported_volume_24h_usd, price_dispersion_bps, funding_rate_8h_median\nFROM market\nWHERE ${where.join("\n  AND ")}\nORDER BY reported_volume_24h_usd DESC\nLIMIT 100;`;
}

export async function scanner(main: HTMLElement): Promise<void> {
  main.replaceChildren(pageHead("Scanner", "Filter the observable market by structural properties. Filters describe data (venue counts, volumes, dispersion, funding sign); they are not recommendations."));
  try {
    const [{ assets: rows }, o] = await Promise.all([loadAssets(), loadOverview()]);
    const [f1, minVenues] = field("minv", "Minimum venues", "2", "1");
    const [f2, minVol] = field("minvol", "Minimum reported 24h volume (USD)", "0");
    const [f3, minDisp] = field("mind", "Minimum dispersion (bps)", "0");
    const [f4, maxDisp] = field("maxd", "Maximum dispersion (bps)", "");
    const select = h(
      "select",
      { id: "fund" },
      h("option", { value: "any" }, "Any"),
      h("option", { value: "available" }, "Funding available"),
      h("option", { value: "positive" }, "Positive funding"),
      h("option", { value: "negative" }, "Negative funding"),
    );
    const f5 = h("div", { class: "field" }, h("label", { for: "fund" }, "Funding (8h median)"), select);
    const out = h("div", {});
    const mql = h("pre", { class: "code", "aria-label": "Equivalent MQL query" });

    const run = () => {
      const f: Filters = {
        minVenues: Number(minVenues.value) || 1,
        minVolume: Number(minVol.value) || 0,
        minDisp: Number(minDisp.value) || 0,
        maxDisp: maxDisp.value === "" ? Number.POSITIVE_INFINITY : Number(maxDisp.value),
        funding: select.value as Filters["funding"],
      };
      const hits = rows.filter(
        (r) =>
          r.venues >= f.minVenues &&
          (r.volume_usd ?? 0) >= f.minVolume &&
          (f.minDisp === 0 || (r.dispersion_bps !== null && r.dispersion_bps >= f.minDisp)) &&
          (!Number.isFinite(f.maxDisp) || (r.dispersion_bps !== null && r.dispersion_bps <= f.maxDisp)) &&
          (f.funding === "any" ||
            (f.funding === "available" && r.funding_8h !== null) ||
            (f.funding === "positive" && (r.funding_8h ?? 0) > 0) ||
            (f.funding === "negative" && (r.funding_8h ?? 0) < 0)),
      );
      mql.textContent = toMql(f);
      out.replaceChildren(
        h("p", { class: "muted small", "aria-live": "polite" }, `${hits.length} of ${rows.length} assets match`),
        dataTable<AssetRow>(
          [
            { key: "symbol", label: "Asset", sort: (r) => r.symbol, render: (r) => (r.file ? h("a", { href: `#/asset/${encodeURIComponent(r.file)}` }, r.symbol) : r.symbol) },
            { key: "price", label: "Reference price", align: "right", sort: (r) => r.price, render: (r) => price(r.price) },
            { key: "venues", label: "Venues", align: "right", sort: (r) => r.venues, render: (r) => String(r.venues) },
            { key: "vol", label: "Reported volume", align: "right", sort: (r) => r.volume_usd, render: (r) => usd(r.volume_usd) },
            { key: "disp", label: "Dispersion", align: "right", sort: (r) => r.dispersion_bps, render: (r) => bps(r.dispersion_bps) },
            { key: "fund", label: "Funding (8h)", align: "right", secondary: true, sort: (r) => r.funding_8h, render: (r) => signedPct(r.funding_8h) },
          ],
          hits,
          { caption: "Filter results", initialSort: "vol", limit: 200 },
        ),
      );
    };
    for (const el of [minVenues, minVol, minDisp, maxDisp, select]) el.addEventListener("input", run);
    main.append(
      fixtureBanner(o) ?? "",
      h("form", { class: "filters", onSubmit: (e: Event) => e.preventDefault() }, f1, f2, f3, f4, f5),
      h("h2", {}, "Equivalent Market Query Language"),
      h("p", { class: "muted small" }, "The same filter as an MQL query. Run it locally against the downloaded dataset with ", h("code", {}, "atlas query"), ". ", h("a", { href: `${DOCS}/MQL.md` }, "MQL reference")),
      mql,
      out,
    );
    run();
  } catch (error) {
    main.append(errorBox(error));
  }
}
