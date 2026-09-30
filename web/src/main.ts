import "./style.css";
import { buildShell } from "./shell";
import { home } from "./pages/home";
import { assets } from "./pages/assets";
import { asset } from "./pages/asset";
import { market } from "./pages/market";
import { scanner } from "./pages/scanner";
import { exchanges } from "./pages/exchanges";
import { about, dataset, methodology } from "./pages/static";

const shell = buildShell();

async function route(): Promise<void> {
  const hash = location.hash.replace(/^#/, "") || "/";
  const [, first = "", ...rest] = hash.split("/");
  const path = `/${first}`;
  shell.setActive(first === "asset" ? "/assets" : path);
  switch (first) {
    case "":
      document.title = "Vizanix Atlas";
      await home(shell.main);
      break;
    case "assets":
      document.title = "Assets · Vizanix Atlas";
      await assets(shell.main);
      break;
    case "asset":
      document.title = "Asset · Vizanix Atlas";
      await asset(shell.main, decodeURIComponent(rest.join("/")).replace(/[^a-z0-9._-]/gi, ""));
      break;
    case "market":
      document.title = "Market · Vizanix Atlas";
      await market(shell.main);
      break;
    case "scanner":
      document.title = "Scanner · Vizanix Atlas";
      await scanner(shell.main);
      break;
    case "exchanges":
      document.title = "Exchanges · Vizanix Atlas";
      await exchanges(shell.main);
      break;
    case "methodology":
      document.title = "Methodology · Vizanix Atlas";
      methodology(shell.main);
      break;
    case "dataset":
      document.title = "Dataset · Vizanix Atlas";
      await dataset(shell.main);
      break;
    case "about":
      document.title = "About · Vizanix Atlas";
      about(shell.main);
      break;
    default:
      shell.main.replaceChildren(Object.assign(document.createElement("p"), { textContent: "Page not found." }));
  }
  shell.main.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", () => void route());
void route();
