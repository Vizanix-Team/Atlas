import { h } from "./dom";

export interface Column<T> {
  key: string;
  label: string;
  /** Text shown in the cell. */
  render: (row: T) => string | Node;
  /** Numeric/string value used for sorting; null sorts last. */
  sort?: (row: T) => number | string | null;
  align?: "right";
  /** Hidden below the small breakpoint so narrow screens stay readable. */
  secondary?: boolean;
  title?: string;
}

export function dataTable<T>(
  columns: Column<T>[],
  rows: T[],
  opts: { caption: string; initialSort?: string; descending?: boolean; limit?: number } = { caption: "" },
): HTMLElement {
  let sortKey = opts.initialSort ?? "";
  let descending = opts.descending ?? true;
  const wrap = h("div", { class: "table-wrap", tabindex: 0, role: "region", "aria-label": opts.caption });
  const limit = opts.limit ?? 200;

  function draw(): void {
    const col = columns.find((c) => c.key === sortKey);
    let ordered = rows.slice();
    if (col?.sort) {
      const get = col.sort;
      ordered.sort((a, b) => {
        const x = get(a);
        const y = get(b);
        if (x === null && y === null) return 0;
        if (x === null) return 1;
        if (y === null) return -1;
        const cmp = typeof x === "string" && typeof y === "string" ? x.localeCompare(y) : Number(x) - Number(y);
        return descending ? -cmp : cmp;
      });
    }
    ordered = ordered.slice(0, limit);
    const head = h(
      "tr",
      {},
      ...columns.map((c) => {
        const active = c.key === sortKey;
        const th = h(
          "th",
          {
            scope: "col",
            class: [c.align === "right" ? "num" : "", c.secondary ? "secondary" : ""].join(" ").trim(),
            "aria-sort": active ? (descending ? "descending" : "ascending") : "none",
            title: c.title,
          },
          c.sort
            ? h(
                "button",
                {
                  type: "button",
                  class: "sort",
                  onClick: () => {
                    if (sortKey === c.key) descending = !descending;
                    else {
                      sortKey = c.key;
                      descending = true;
                    }
                    draw();
                  },
                },
                c.label,
                active ? (descending ? " ↓" : " ↑") : "",
              )
            : c.label,
        );
        return th;
      }),
    );
    const body = ordered.map((row) =>
      h(
        "tr",
        {},
        ...columns.map((c) =>
          h("td", { class: [c.align === "right" ? "num" : "", c.secondary ? "secondary" : ""].join(" ").trim() }, c.render(row)),
        ),
      ),
    );
    const table = h(
      "table",
      {},
      h("caption", { class: "sr-only" }, opts.caption),
      h("thead", {}, head),
      h("tbody", {}, ...body),
    );
    wrap.replaceChildren(table);
    if (rows.length > limit) wrap.append(h("p", { class: "muted small pad" }, `Showing ${limit} of ${rows.length} rows.`));
  }
  draw();
  return wrap;
}
