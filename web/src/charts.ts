import { h, svg } from "./dom";

export interface Bar { label: string; value: number; note?: string }

/** Horizontal bar chart. The same numbers are always available in an adjacent table. */
export function barChart(
  bars: Bar[],
  opts: { title: string; format: (v: number) => string; diverging?: boolean },
): HTMLElement {
  const rowH = 26;
  const labelW = 92;
  const width = 440;
  const plotW = width - labelW - 92;
  const max = Math.max(1e-12, ...bars.map((b) => Math.abs(b.value)));
  const mid = opts.diverging ? labelW + plotW / 2 : labelW;
  const scale = (opts.diverging ? plotW / 2 : plotW) / max;
  const height = bars.length * rowH + 8;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": opts.title, class: "chart" });
  const titleEl = svg("title");
  titleEl.textContent = opts.title;
  root.appendChild(titleEl);
  if (opts.diverging) root.appendChild(svg("line", { x1: mid, x2: mid, y1: 0, y2: height, class: "axis" }));
  bars.forEach((b, i) => {
    const y = i * rowH + 4;
    const w = Math.abs(b.value) * scale;
    const x = opts.diverging && b.value < 0 ? mid - w : mid;
    const label = svg("text", { x: labelW - 8, y: y + 15, "text-anchor": "end", class: "label" });
    label.textContent = b.label;
    const rect = svg("rect", { x, y, width: Math.max(w, 1), height: rowH - 8, rx: 2, class: b.value < 0 ? "bar neg" : "bar" });
    const value = svg("text", { x: labelW + plotW + 8 + (opts.diverging ? 0 : 0), y: y + 15, class: "value" });
    value.textContent = opts.format(b.value);
    root.append(label, rect, value);
  });
  return h("figure", { class: "chart-figure" }, root);
}
