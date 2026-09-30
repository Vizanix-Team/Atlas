import { h } from "./dom";

export interface Route { path: string; label: string }

export const NAV: Route[] = [
  { path: "/", label: "Home" },
  { path: "/assets", label: "Assets" },
  { path: "/market", label: "Market" },
  { path: "/scanner", label: "Scanner" },
  { path: "/exchanges", label: "Exchanges" },
  { path: "/methodology", label: "Methodology" },
  { path: "/dataset", label: "Dataset" },
  { path: "/about", label: "About" },
];

function currentTheme(): "light" | "dark" {
  const stored = localStorage.getItem("atlas-theme");
  if (stored === "light" || stored === "dark") return stored;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function applyTheme(theme: "light" | "dark"): void {
  document.documentElement.dataset["theme"] = theme;
}

export function buildShell(): { main: HTMLElement; setActive: (path: string) => void } {
  applyTheme(currentTheme());
  const links = NAV.map((r) => h("a", { href: `#${r.path}`, "data-path": r.path }, r.label));
  const themeBtn = h(
    "button",
    {
      type: "button",
      class: "theme",
      "aria-label": "Toggle colour theme",
      onClick: () => {
        const next = document.documentElement.dataset["theme"] === "dark" ? "light" : "dark";
        localStorage.setItem("atlas-theme", next);
        applyTheme(next);
      },
    },
    "Theme",
  );
  const header = h(
    "header",
    { class: "site-header" },
    h(
      "div",
      { class: "inner" },
      h("a", { class: "brand", href: "#/" }, h("span", { class: "mark", "aria-hidden": "true" }), "Vizanix Atlas"),
      h("nav", { "aria-label": "Primary" }, ...links),
      themeBtn,
    ),
  );
  const main = h("main", { id: "main", tabindex: -1 });
  const footer = h(
    "footer",
    { class: "site-footer" },
    h(
      "div",
      { class: "inner" },
      h("p", {}, "Vizanix Atlas is a research and infrastructure project, not investment advice. Venue data is reported by the venues, may contain errors, and is sampled on a schedule (not real time)."),
      h("p", {}, "Maintained by Vizanix. Code: Apache-2.0. Data terms: see the Dataset and Exchanges pages."),
    ),
  );
  document.getElementById("app")!.replaceChildren(header, main, footer);
  return {
    main,
    setActive: (path) => {
      for (const a of links) {
        const active = a.dataset["path"] === path;
        if (active) a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
      }
    },
  };
}
