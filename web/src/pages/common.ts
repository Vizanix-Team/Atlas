import { h } from "../dom";
import type { Overview } from "../data";
import { age, utc } from "../format";

export function pageHead(title: string, lede?: string): HTMLElement {
  return h("div", { class: "page-head" }, h("h1", {}, title), lede ? h("p", { class: "lede" }, lede) : null);
}

export function fixtureBanner(o: Overview): HTMLElement | null {
  return o.fixture
    ? h("div", { class: "banner", role: "note" }, "Fixture data: this build ships a tiny sample so the site can run without a published dataset. These are not market values.")
    : null;
}

export function freshness(o: Overview): HTMLElement {
  return h(
    "p",
    { class: "muted small" },
    `Snapshot effective ${utc(o.snapshot_effective_time)} (${age(o.snapshot_effective_time)}) · generation ${o.generation_id} · scheduled snapshots, not real time`,
  );
}

export function kpi(label: string, value: string, note?: string, title?: string): HTMLElement {
  return h("div", { class: "kpi", title }, h("div", { class: "kpi-label" }, label), h("div", { class: "kpi-value" }, value), note ? h("div", { class: "kpi-note" }, note) : null);
}

export function errorBox(error: unknown): HTMLElement {
  const message = error instanceof Error ? error.message : String(error);
  return h("div", { class: "banner error", role: "alert" }, `Could not load data. ${message}`);
}
