import { h } from "../dom";
import { loadAssets, loadOverview, type AssetRow } from "../data";
import { bps, price, usd } from "../format";
import { dataTable } from "../table";
import { errorBox, fixtureBanner, pageHead } from "./common";

export function assetColumns() {
  return [
    {
      key: "symbol",
      label: "Asset",
      sort: (r: AssetRow) => r.symbol,
      render: (r: AssetRow) =>
        r.file ? h("a", { href: `#/asset/${encodeURIComponent(r.file)}` }, r.symbol, r.name ? h("span", { class: "muted" }, ` ${r.name}`) : null) : h("span", {}, r.symbol, r.name ? h("span", { class: "muted" }, ` ${r.name}`) : null),
    },
    { key: "price", label: "Reference price", align: "right" as const, sort: (r: AssetRow) => r.price, render: (r: AssetRow) => price(r.price) },
    { key: "venues", label: "Venues", align: "right" as const, sort: (r: AssetRow) => r.venues, render: (r: AssetRow) => String(r.venues) },
    { key: "volume", label: "Reported 24h volume", align: "right" as const, sort: (r: AssetRow) => r.volume_usd, render: (r: AssetRow) => usd(r.volume_usd) },
    { key: "disp", label: "Dispersion", align: "right" as const, secondary: true, sort: (r: AssetRow) => r.dispersion_bps, render: (r: AssetRow) => bps(r.dispersion_bps) },
    { key: "res", label: "Identity", secondary: true, sort: (r: AssetRow) => r.resolution, render: (r: AssetRow) => r.resolution },
  ];
}

export async function assets(main: HTMLElement): Promise<void> {
  main.replaceChildren(pageHead("Assets", "Every canonical asset Atlas resolved. An asset is a cross-venue object, not one exchange's ticker."));
  try {
    const [{ assets: rows }, o] = await Promise.all([loadAssets(), loadOverview()]);
    const results = h("div", {});
    const input = h("input", { type: "search", id: "q", placeholder: "Search symbol or name", autocomplete: "off" });
    const multi = h("input", { type: "checkbox", id: "multi" });
    const draw = () => {
      const q = input.value.trim().toLowerCase();
      const filtered = rows.filter(
        (r) => (!multi.checked || r.venues >= 2) && (q === "" || r.symbol.toLowerCase().includes(q) || (r.name ?? "").toLowerCase().includes(q)),
      );
      results.replaceChildren(
        h("p", { class: "muted small", "aria-live": "polite" }, `${filtered.length} assets`),
        dataTable(assetColumns(), filtered, { caption: "Assets", initialSort: "volume", limit: 300 }),
      );
    };
    input.addEventListener("input", draw);
    multi.addEventListener("change", draw);
    main.append(
      fixtureBanner(o) ?? "",
      h("div", { class: "controls" }, h("label", { for: "q" }, "Search "), input, h("label", { class: "check" }, multi, " Only assets on 2+ venues")),
      results,
      h("p", { class: "muted small" }, "A ticker is a convenience, not an identity: two venues can list different tokens under one ticker, and Atlas keeps them apart unless evidence says they are the same."),
    );
    draw();
  } catch (error) {
    main.append(errorBox(error));
  }
}
