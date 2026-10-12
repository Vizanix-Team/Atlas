// Live prices: one Server-Sent Events connection to the Atlas live service, shared by every
// page. The tables on the site are scheduled snapshots; this layer streams the reference
// price between them and says so wherever it appears.

import { h } from "./dom";
import { price as formatPrice } from "./format";

export interface LiveRecord {
  asset_id: string;
  symbol: string;
  price: number;
  venues: number;
  dispersion_bps: number | null;
  volume_24h_usd: number | null;
  vs_generation_bps: number | null;
  identity: string;
  as_of: number;
}

export type LiveStatus = "connecting" | "live" | "stale" | "offline";

interface Snapshot { assets: LiveRecord[]; generation_id?: string }

const DEFAULT_BASE = "https://vizanix.tech/atlas-stream";
// Updates are re-announced at least every 10 s per asset, so silence this long means the
// stream is broken even if the socket looks open.
const STALE_AFTER_MS = 45_000;
// A hidden tab closes its connection after this long, and reopens when shown again.
const HIDDEN_CLOSE_MS = 60_000;

function baseUrl(): string {
  const configured = document.querySelector('meta[name="atlas-live-url"]')?.getAttribute("content");
  return (configured || DEFAULT_BASE).replace(/\/+$/, "");
}

class LiveFeed {
  readonly records = new Map<string, LiveRecord>();
  status: LiveStatus = "connecting";
  generationId: string | null = null;
  private lastMessage = 0;
  private source: EventSource | null = null;
  private hiddenTimer: number | undefined;
  private readonly onChange = new Set<(ids: string[]) => void>();
  private readonly onStatus = new Set<() => void>();
  private started = false;

  start(): void {
    if (this.started || typeof EventSource === "undefined") {
      if (typeof EventSource === "undefined") this.setStatus("offline");
      return;
    }
    this.started = true;
    this.connect();
    window.setInterval(() => this.checkStale(), 5_000);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        this.hiddenTimer = window.setTimeout(() => this.disconnect(), HIDDEN_CLOSE_MS);
      } else {
        window.clearTimeout(this.hiddenTimer);
        if (!this.source) this.connect();
      }
    });
  }

  subscribe(listener: (ids: string[]) => void): () => void {
    this.onChange.add(listener);
    return () => this.onChange.delete(listener);
  }

  subscribeStatus(listener: () => void): () => void {
    this.onStatus.add(listener);
    return () => this.onStatus.delete(listener);
  }

  private connect(): void {
    this.setStatus("connecting");
    const source = new EventSource(`${baseUrl()}/v1/stream`);
    this.source = source;
    source.addEventListener("snapshot", (event) => this.receive(event as MessageEvent<string>, true));
    source.addEventListener("update", (event) => this.receive(event as MessageEvent<string>, false));
    // EventSource reconnects on its own (the server asks for 3 s); reflect the gap honestly.
    source.onerror = () => this.setStatus(this.records.size ? "stale" : "offline");
  }

  private disconnect(): void {
    this.source?.close();
    this.source = null;
    this.setStatus("offline");
  }

  private receive(event: MessageEvent<string>, snapshot: boolean): void {
    let payload: Snapshot;
    try {
      payload = JSON.parse(event.data) as Snapshot;
    } catch {
      return;
    }
    this.lastMessage = Date.now();
    if (snapshot) {
      this.records.clear();
      this.generationId = payload.generation_id ?? null;
    }
    const changed: string[] = [];
    for (const record of payload.assets ?? []) {
      if (typeof record.price !== "number" || !Number.isFinite(record.price)) continue;
      this.records.set(record.asset_id, record);
      changed.push(record.asset_id);
    }
    this.setStatus("live");
    if (changed.length) for (const listener of this.onChange) listener(changed);
  }

  private checkStale(): void {
    if (this.source && this.status === "live" && Date.now() - this.lastMessage > STALE_AFTER_MS) {
      this.setStatus("stale");
    }
  }

  private setStatus(status: LiveStatus): void {
    if (this.status === status) return;
    this.status = status;
    for (const listener of this.onStatus) listener();
  }
}

export const live = new LiveFeed();

// ---------------------------------------------------------------- DOM helpers

const cells = new Map<string, Set<HTMLElement>>();
let wired = false;

function paint(element: HTMLElement, record: LiveRecord): void {
  const previous = Number(element.dataset["price"]);
  element.textContent = formatPrice(record.price);
  element.dataset["price"] = String(record.price);
  element.title = `Live reference price · ${record.venues} venue${record.venues === 1 ? "" : "s"}${
    record.dispersion_bps === null ? "" : ` · dispersion ${record.dispersion_bps.toFixed(1)} bps`
  }${record.identity === "probable" ? " · identity: probable" : ""}`;
  if (Number.isFinite(previous) && previous !== record.price) {
    const direction = record.price > previous ? "tick-up" : "tick-down";
    element.classList.remove("tick-up", "tick-down");
    // Force a reflow so the animation restarts on rapid consecutive ticks.
    void element.offsetWidth;
    element.classList.add(direction);
  }
}

function wire(): void {
  if (wired) return;
  wired = true;
  live.subscribe((ids) => {
    for (const id of ids) {
      const record = live.records.get(id);
      const set = cells.get(id);
      if (!record || !set) continue;
      for (const element of set) {
        if (!element.isConnected) set.delete(element);
        else paint(element, record);
      }
      if (set.size === 0) cells.delete(id);
    }
  });
}

/** A price element that updates in place whenever the asset ticks. */
export function liveCell(assetId: string, fallback = "n/a"): HTMLElement {
  wire();
  const element = h("span", { class: "live-price", "data-asset": assetId }, fallback);
  let set = cells.get(assetId);
  if (!set) {
    set = new Set();
    cells.set(assetId, set);
  }
  set.add(element);
  const record = live.records.get(assetId);
  if (record) paint(element, record);
  return element;
}

function bpsText(value: number | null): string {
  return value === null || !Number.isFinite(value) ? "n/a" : `${value >= 0 ? "+" : ""}${value.toFixed(1)} bps`;
}

/**
 * One asset's live line for its detail page: price, venue count, dispersion, and how far the
 * stream has moved from the published snapshot. Updates in place; says so when it cannot.
 */
export function liveStrip(assetId: string, snapshotPrice: number | null): HTMLElement {
  const price = liveCell(assetId, "waiting for the stream…");
  price.classList.add("big");
  const detail = h("span", { class: "muted" });
  const strip = h("div", { class: "live-strip", "aria-live": "off" }, h("span", { class: "live-tag" }, "LIVE"), price, detail);
  const draw = (): void => {
    const record = live.records.get(assetId);
    if (!record) {
      detail.textContent = live.status === "offline" ? " live feed offline; showing the snapshot only" : "";
      return;
    }
    const drift = snapshotPrice ? ((record.price - snapshotPrice) / snapshotPrice) * 10_000 : null;
    detail.textContent = ` ${record.venues} venues · dispersion ${record.dispersion_bps === null ? "n/a" : `${record.dispersion_bps.toFixed(1)} bps`} · ${bpsText(drift)} vs snapshot`;
  };
  draw();
  const stop = live.subscribe(() => {
    if (!strip.isConnected) stop();
    else draw();
  });
  live.subscribeStatus(draw);
  return strip;
}

/** The most liquid assets by reported volume, streaming. */
export function liveBoard(limit = 10): HTMLElement {
  const body = h("tbody", {});
  const note = h("p", { class: "muted small" });
  const table = h(
    "table",
    {},
    h("caption", { class: "sr-only" }, "Live reference prices"),
    h(
      "thead",
      {},
      h(
        "tr",
        {},
        h("th", { scope: "col" }, "Asset"),
        h("th", { scope: "col", class: "num" }, "Live price"),
        h("th", { scope: "col", class: "num secondary" }, "Venues"),
        h("th", { scope: "col", class: "num secondary" }, "Dispersion"),
        h("th", { scope: "col", class: "num" }, "vs snapshot"),
      ),
    ),
    body,
  );
  const wrap = h("div", { class: "table-wrap" }, table);
  const draw = (): void => {
    const rows = [...live.records.values()]
      .sort((a, b) => (b.volume_24h_usd ?? 0) - (a.volume_24h_usd ?? 0))
      .slice(0, limit);
    body.replaceChildren(
      ...rows.map((r) =>
        h(
          "tr",
          {},
          h("td", {}, r.symbol),
          h("td", { class: "num" }, liveCell(r.asset_id)),
          h("td", { class: "num secondary" }, String(r.venues)),
          h("td", { class: "num secondary" }, r.dispersion_bps === null ? "n/a" : `${r.dispersion_bps.toFixed(1)} bps`),
          h("td", { class: "num" }, bpsText(r.vs_generation_bps)),
        ),
      ),
    );
    note.textContent =
      live.status === "live"
        ? "Streamed from venue WebSockets. “vs snapshot” is the move since the latest published generation."
        : live.status === "connecting"
          ? "Connecting to the live feed…"
          : "The live feed is unavailable right now; the tables below are the last scheduled snapshot.";
  };
  draw();
  // Rows are rebuilt about once a second (ordering and the venue/dispersion columns change
  // slowly); the price cells themselves update on every tick.
  const timer = window.setInterval(() => {
    if (!wrap.isConnected) window.clearInterval(timer);
    else draw();
  }, 1_000);
  return h("section", { class: "live-board", "aria-label": "Live prices" }, h("h2", {}, "Live now"), wrap, note);
}

const STATUS_TEXT: Record<LiveStatus, string> = {
  connecting: "Connecting…",
  live: "Live",
  stale: "Live feed paused",
  offline: "Live feed offline",
};

/** The header indicator. Colour is never the only signal: the state is also written out. */
export function livePill(): HTMLElement {
  const pill = h("a", { class: "live-pill", href: "#/", "data-state": live.status, "aria-live": "polite" });
  const dot = h("span", { class: "dot", "aria-hidden": "true" });
  const label = h("span", {});
  pill.append(dot, label);
  const draw = (): void => {
    pill.dataset["state"] = live.status;
    label.textContent = live.status === "live" ? `Live · ${live.records.size} assets` : STATUS_TEXT[live.status];
    pill.title =
      live.status === "live"
        ? "Reference prices stream from venue WebSockets between the scheduled snapshots."
        : "Live prices are unavailable; the tables show the last scheduled snapshot.";
  };
  draw();
  live.subscribeStatus(draw);
  live.subscribe(() => {
    if (live.status === "live") label.textContent = `Live · ${live.records.size} assets`;
  });
  return pill;
}
