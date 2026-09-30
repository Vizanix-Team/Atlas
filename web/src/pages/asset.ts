import { h } from "../dom";
import { loadAssetDetail, loadOverview, type VenueAssetRow } from "../data";
import { barChart } from "../charts";
import { bps, num, pct, price, signedPct, usd } from "../format";
import { dataTable } from "../table";
import { DOCS } from "../links";
import { errorBox, fixtureBanner, pageHead } from "./common";

type State = Record<string, unknown>;

function n(state: State, key: string): number | null {
  const v = state[key];
  return typeof v === "number" ? v : null;
}

function section(title: string, rows: [string, string, string?][]): HTMLElement {
  return h(
    "section",
    { class: "card" },
    h("h2", {}, title),
    h("dl", {}, ...rows.flatMap(([k, v, tip]) => [h("dt", { title: tip }, k), h("dd", {}, v)])),
  );
}

export async function asset(main: HTMLElement, file: string): Promise<void> {
  main.replaceChildren(pageHead("Asset"));
  try {
    const [detail, overview] = await Promise.all([loadAssetDetail(file), loadOverview()]);
    const s = detail.state;
    const symbol = String(s["symbol"] ?? "?");
    const venues = detail.venues;
    const included = venues.filter((v) => v.included_in_reference_price);
    const excluded = venues.filter((v) => v.included_in_reference_price === false);

    main.replaceChildren(
      pageHead(`${symbol}${s["name"] ? ` · ${String(s["name"])}` : ""}`, "One asset, decomposed by venue. The reference price is a robust cross-venue estimate, not a single exchange's last trade."),
      fixtureBanner(overview) ?? "",
      h("p", { class: "muted small" }, `Identity: ${String(s["resolution_state"])} · ${String(s["asset_id"])}`),
      h(
        "div",
        { class: "price-line" },
        h("span", { class: "big" }, price(n(s, "reference_price__value"))),
        h("span", { class: "muted" }, ` reference price · method ${String(s["reference_price__method"] ?? "n/a")} · ${included.length} venues included, ${excluded.length} excluded`),
      ),
      h(
        "div",
        { class: "grid-3" },
        section("Volume", [
          ["Reported 24h (all)", usd(n(s, "reported_volume_24h_usd")), "Reported by venues; not independently verified."],
          ["Reported spot", usd(n(s, "volume__reported_spot_volume_24h_usd"))],
          ["Reported perpetual", usd(n(s, "volume__reported_perp_volume_24h_usd"))],
        ]),
        section("Derivatives", [
          ["Funding, 8h median", signedPct(n(s, "funding__rate_8h_median")), "Each venue's native interval is normalised to an 8-hour equivalent."],
          ["Open interest", usd(n(s, "open_interest__total_usd"))],
          ["Perp basis (median)", bps(n(s, "basis__perp_basis_bps_median")), "(derivative reference − spot reference) / spot reference × 10,000"],
        ]),
        section("Dispersion and quality", [
          ["Price dispersion", bps(n(s, "dispersion__price_dispersion_bps")), "Robust dispersion of venue prices."],
          ["Best spread", bps(n(s, "liquidity__best_spread_bps"))],
          ["Coverage ratio", pct(n(s, "quality__coverage__coverage_ratio"), 0), "Share of expected venues that contributed."],
          ["Partial data", s["is_partial"] === true ? "yes" : "no"],
          ["Venues", num(n(s, "venue_count"), 0)],
        ]),
      ),
      h("h2", {}, "Venue decomposition"),
      barChart(
        venues.filter((v) => v.deviation_bps !== null).map((v) => ({ label: v.venue_slug, value: v.deviation_bps as number })),
        { title: "Venue price deviation from the reference price, in basis points", format: (v) => `${v.toFixed(1)} bps`, diverging: true },
      ),
      dataTable<VenueAssetRow>(
        [
          { key: "venue", label: "Venue", sort: (r) => r.venue_slug, render: (r) => r.venue_slug },
          { key: "price", label: "Price (USD)", align: "right", sort: (r) => r.price_usd, render: (r) => price(r.price_usd) },
          { key: "dev", label: "Deviation", align: "right", sort: (r) => r.deviation_bps, render: (r) => bps(r.deviation_bps) },
          { key: "vol", label: "Reported volume", align: "right", secondary: true, sort: (r) => r.reported_volume_24h_usd, render: (r) => usd(r.reported_volume_24h_usd) },
          { key: "w", label: "Weight", align: "right", secondary: true, sort: (r) => r.reference_weight, render: (r) => pct(r.reference_weight, 1), title: "Capped weight in the reference price" },
          { key: "src", label: "Price source", secondary: true, sort: (r) => r.price_source, render: (r) => r.price_source ?? "n/a" },
          { key: "inc", label: "In reference price", render: (r) => (r.included_in_reference_price ? "yes" : `no${r.exclusion_reason ? ` (${r.exclusion_reason})` : ""}`) },
        ],
        venues,
        { caption: "Per-venue observations", initialSort: "vol" },
      ),
      h("p", { class: "muted small" }, h("a", { href: `${DOCS}/METHODOLOGY.md` }, "How these numbers are computed")),
      h("p", {}, h("a", { href: "#/assets" }, "← All assets")),
    );
  } catch (error) {
    main.append(errorBox(error));
  }
}
