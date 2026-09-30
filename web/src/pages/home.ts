import { h } from "../dom";
import { loadOverview, type TopEntry } from "../data";
import { bps, num, usd } from "../format";
import { dataTable } from "../table";
import { DOCS } from "../links";
import { errorBox, fixtureBanner, freshness, kpi } from "./common";

function topTable(caption: string, entries: TopEntry[], fmt: (v: number) => string, valueLabel: string): HTMLElement {
  return dataTable<TopEntry>(
    [
      { key: "symbol", label: "Asset", render: (r) => h("a", { href: "#/assets" }, r.symbol) },
      { key: "value", label: valueLabel, align: "right", render: (r) => fmt(r.value), sort: (r) => r.value },
      { key: "venues", label: "Venues", align: "right", secondary: true, render: (r) => String(r.venues), sort: (r) => r.venues },
    ],
    entries,
    { caption, initialSort: "value" },
  );
}

export async function home(main: HTMLElement): Promise<void> {
  const hero = h(
    "section",
    { class: "hero" },
    h("h1", {}, "The semantic layer for global crypto markets."),
    h("p", { class: "lede" }, "Exchanges provide events. Atlas provides market state. Atlas turns fragmented public exchange observations into a reproducible, cross-venue market state, with coverage, quality and provenance shown next to every number."),
    h("p", {}, h("a", { class: "button", href: "#/assets" }, "Explore assets"), " ", h("a", { class: "button ghost", href: `${DOCS}/METHODOLOGY.md` }, "Read the methodology")),
  );
  main.replaceChildren(hero);
  try {
    const o = await loadOverview();
    main.append(
      fixtureBanner(o) ?? "",
      freshness(o),
      h(
        "section",
        { class: "kpis", "aria-label": "Dataset summary" },
        kpi("Assets", num(o.counts.assets, 0), `${num(o.qualified_assets.multi_venue, 0)} on 2+ venues`),
        kpi("Instruments", num(o.counts.instruments, 0)),
        kpi("Venues contributing", num(o.counts.venues_contributing, 0)),
        kpi("Reported 24h volume", usd(o.reported_volume_24h_usd.total), "as reported by venues", o.reported_volume_24h_usd.note),
        kpi("Median price dispersion", bps(o.median_dispersion_bps_multi_venue), "assets on 2+ venues", "Robust cross-venue spread of reference-price inputs."),
      ),
      h(
        "section",
        { class: "grid-2" },
        h("div", {}, h("h2", {}, "Largest reported volume, on 2+ venues"), topTable("Assets on 2+ venues by reported 24h volume", o.top_reported_volume, usd, "Reported 24h volume")),
        h("div", {}, h("h2", {}, "Widest cross-venue price dispersion"), topTable("Assets by price dispersion, on 3+ venues", o.top_price_dispersion, bps, "Dispersion")),
      ),
      h("h2", {}, "Collection status"),
      h(
        "ul",
        { class: "status-list" },
        ...Object.entries(o.system.venues_by_status).map(([status, venues]) => h("li", {}, h("strong", {}, `${status}: `), venues.join(", "))),
      ),
      h("p", { class: "muted small" }, o.system.history_note),
    );
  } catch (error) {
    main.append(errorBox(error));
  }
}
