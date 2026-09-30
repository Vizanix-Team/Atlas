import { h } from "../dom";
import { loadAssets, loadOverview, type AssetRow } from "../data";
import { bps, price, signedPct, usd } from "../format";
import { dataTable } from "../table";
import { errorBox, fixtureBanner, freshness, pageHead } from "./common";

export async function market(main: HTMLElement): Promise<void> {
  main.replaceChildren(pageHead("Market", "The observable market as one table. Empty cells mean the metric was not available, never zero."));
  try {
    const [{ assets: rows }, o] = await Promise.all([loadAssets(), loadOverview()]);
    main.append(
      fixtureBanner(o) ?? "",
      freshness(o),
      dataTable<AssetRow>(
        [
          { key: "symbol", label: "Asset", sort: (r) => r.symbol, render: (r) => (r.file ? h("a", { href: `#/asset/${encodeURIComponent(r.file)}` }, r.symbol) : r.symbol) },
          { key: "price", label: "Reference price", align: "right", sort: (r) => r.price, render: (r) => price(r.price) },
          { key: "venues", label: "Venues", align: "right", sort: (r) => r.venues, render: (r) => String(r.venues) },
          { key: "vol", label: "Reported volume", align: "right", sort: (r) => r.volume_usd, render: (r) => usd(r.volume_usd), title: "Reported 24h volume in USD as published by venues" },
          { key: "spot", label: "Spot volume", align: "right", secondary: true, sort: (r) => r.spot_volume_usd, render: (r) => usd(r.spot_volume_usd) },
          { key: "perp", label: "Perpetual volume", align: "right", secondary: true, sort: (r) => r.perp_volume_usd, render: (r) => usd(r.perp_volume_usd) },
          { key: "disp", label: "Dispersion", align: "right", secondary: true, sort: (r) => r.dispersion_bps, render: (r) => bps(r.dispersion_bps), title: "Robust spread of venue prices around the reference price" },
          { key: "fund", label: "Funding (8h, median)", align: "right", secondary: true, sort: (r) => r.funding_8h, render: (r) => signedPct(r.funding_8h), title: "Median funding rate, normalised to an 8-hour equivalent" },
          { key: "oi", label: "Open interest", align: "right", secondary: true, sort: (r) => r.oi_usd, render: (r) => usd(r.oi_usd) },
          { key: "basis", label: "Perp basis", align: "right", secondary: true, sort: (r) => r.basis_bps, render: (r) => bps(r.basis_bps) },
        ],
        rows,
        { caption: "Market table", initialSort: "vol", limit: 500 },
      ),
    );
  } catch (error) {
    main.append(errorBox(error));
  }
}
