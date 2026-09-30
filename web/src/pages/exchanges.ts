import { h } from "../dom";
import { loadVenues, type VenueHealth } from "../data";
import { num } from "../format";
import { dataTable } from "../table";
import { DOCS, safeHttpUrl } from "../links";
import { errorBox, pageHead } from "./common";

export async function exchanges(main: HTMLElement): Promise<void> {
  main.replaceChildren(pageHead("Exchanges", "Venues are data sources, not infrastructure. Each row shows what the latest collection actually returned."));
  try {
    const doc = await loadVenues();
    main.append(
      dataTable<VenueHealth>(
        [
          { key: "venue", label: "Venue", sort: (r) => r.display_name, render: (r) => r.display_name },
          { key: "status", label: "Status", sort: (r) => r.status, render: (r) => r.status },
          { key: "inst", label: "Instruments", align: "right", sort: (r) => r.instrument_count, render: (r) => num(r.instrument_count, 0) },
          { key: "tick", label: "Tickers", align: "right", sort: (r) => r.ticker_count, render: (r) => num(r.ticker_count, 0) },
          { key: "deriv", label: "Derivative obs.", align: "right", secondary: true, sort: (r) => r.derivative_observation_count, render: (r) => num(r.derivative_observation_count, 0) },
          { key: "books", label: "Order books", align: "right", secondary: true, sort: (r) => r.order_book_count, render: (r) => num(r.order_book_count, 0) },
          { key: "parse", label: "Parse failures", align: "right", secondary: true, sort: (r) => r.parse_failures, render: (r) => num(r.parse_failures, 0) },
          { key: "ms", label: "Duration", align: "right", secondary: true, sort: (r) => r.duration_ms, render: (r) => `${num(r.duration_ms / 1000, 1)} s` },
          {
            key: "terms",
            label: "Terms",
            secondary: true,
            render: (r) => {
              const url = safeHttpUrl(r.terms_url);
              return url ? h("a", { href: url, rel: "noopener noreferrer" }, "terms") : "n/a";
            },
          },
        ],
        doc.venues,
        { caption: "Venue collection status", initialSort: "inst" },
      ),
      h("h2", {}, "Disabled adapters"),
      h("p", { class: "muted small" }, "Disabled adapters are listed with the reason, instead of being hidden or shipped half-working."),
      doc.disabled.length
        ? h("ul", {}, ...doc.disabled.map((d) => h("li", {}, h("strong", {}, d.display_name), `: ${d.reason ?? "no reason recorded"}`)))
        : h("p", {}, "None."),
      h("p", { class: "muted small" }, "Data sources and their terms: ", h("a", { href: `${DOCS}/DATA_SOURCES.md` }, "docs/DATA_SOURCES.md"), "."),
    );
  } catch (error) {
    main.append(errorBox(error));
  }
}
