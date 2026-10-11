"""Render the README graphics (SVG) from the repository's own data.

Run ``python scripts/render_readme_assets.py`` after changing ``config/exchanges.yaml`` or the
snapshot in ``docs/assets/data/``. The venue coverage matrix is read from the exchange
configuration, so it cannot drift from what the code actually collects. Standard library plus
PyYAML only: no plotting stack, and the output is deterministic text that diffs cleanly.

``--check`` re-renders in memory and fails if a committed file differs, which CI uses to keep
the README honest.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import yaml

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"

# Palette: one deep navy, one teal for "works", violet for "relay", amber for attention.
BG0, BG1, CARD, LINE = "#0A1020", "#101B33", "#131F3A", "#243455"
TEXT, MUTED, DIM = "#E6EDF3", "#8FA0B8", "#566786"
TEAL, BLUE, VIOLET, AMBER, ROSE = "#2DD4BF", "#60A5FA", "#A78BFA", "#FBBF24", "#FB7185"
FONT = "Inter, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
MONO = "'JetBrains Mono', 'SF Mono', Consolas, 'Liberation Mono', monospace"


def t(
    x: float,
    y: float,
    text: str,
    *,
    size: int = 14,
    fill: str = TEXT,
    weight: int = 400,
    anchor: str = "start",
    family: str = FONT,
    opacity: float = 1.0,
    extra: str = "",
) -> str:
    """One text element, escaped."""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}" opacity="{opacity}" '
        f"{extra}>{escape(text)}</text>"
    )


def rect(
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    r: float = 10,
    fill: str = CARD,
    stroke: str | None = None,
    sw: float = 1.0,
    opacity: float = 1.0,
    dash: str = "",
) -> str:
    stroke_attr = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r}" '
        f'fill="{fill}" opacity="{opacity}"{stroke_attr}{dash_attr}/>'
    )


def svg(width: int, height: int, body: str, *, title: str, desc: str) -> str:
    defs = f"""
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{BG0}"/><stop offset="1" stop-color="{BG1}"/>
    </linearGradient>
    <linearGradient id="brand" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="{TEAL}"/><stop offset="1" stop-color="{BLUE}"/>
    </linearGradient>
    <linearGradient id="spoke" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="{TEAL}" stop-opacity="0.05"/>
      <stop offset="1" stop-color="{TEAL}" stop-opacity="0.75"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.5" r="0.5">
      <stop offset="0" stop-color="{TEAL}" stop-opacity="0.35"/>
      <stop offset="1" stop-color="{TEAL}" stop-opacity="0"/>
    </radialGradient>
    <pattern id="dots" width="28" height="28" patternUnits="userSpaceOnUse">
      <circle cx="2" cy="2" r="1" fill="{LINE}" opacity="0.55"/>
    </pattern>
    <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7"
            orient="auto-start-reverse">
      <path d="M0 0 L10 5 L0 10 z" fill="{MUTED}"/>
    </marker>
    <marker id="arrow-teal" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7"
            orient="auto-start-reverse">
      <path d="M0 0 L10 5 L0 10 z" fill="{TEAL}"/>
    </marker>
    <marker id="arrow-violet" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7"
            markerHeight="7" orient="auto-start-reverse">
      <path d="M0 0 L10 5 L0 10 z" fill="{VIOLET}"/>
    </marker>
  </defs>"""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="t d">\n'
        f'  <title id="t">{escape(title)}</title><desc id="d">{escape(desc)}</desc>{defs}\n'
        f'  <rect width="{width}" height="{height}" fill="url(#bg)"/>\n{body}\n</svg>\n'
    )


# --------------------------------------------------------------------------- data


def load_venues() -> list[dict]:
    """Read every declared venue from the exchange configuration."""
    raw = yaml.safe_load((ROOT / "config" / "exchanges.yaml").read_text(encoding="utf-8"))
    entries = next(
        value
        for value in raw.values()
        if isinstance(value, list) and value and isinstance(value[0], dict) and "id" in value[0]
    )
    venues = []
    for entry in entries:
        caps = entry.get("capabilities") or {}
        enabled = bool(entry.get("enabled", True))
        egress = entry.get("egress", "direct")
        venues.append(
            {
                "slug": entry["id"],
                "name": entry.get("display_name", entry["id"]),
                "enabled": enabled,
                "egress": egress,
                "spot": bool(caps.get("spot")),
                "perp": bool(caps.get("perpetual")),
                "future": bool(caps.get("future")),
                "option": bool(caps.get("option")),
                "book": bool(caps.get("orderbook")),
                "deriv": bool(caps.get("funding") or caps.get("open_interest")),
            }
        )
    order = {"direct": 0, "relay": 1}
    return sorted(
        venues,
        key=lambda v: (not v["enabled"], order.get(v["egress"], 2), v["name"].lower()),
    )


def human(value: float) -> str:
    """Compact dollar amount."""
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= limit:
            return (
                f"${value / limit:.2f}{suffix}"
                if suffix == "B"
                else f"${value / limit:.1f}{suffix}"
            )
    return f"${value:.0f}"


# ---------------------------------------------------------------------------- hero


def hero(venues: list[dict]) -> str:
    w, h = 1280, 380
    live = [v for v in venues if v["enabled"]]
    count = len(live)
    cx, cy, radius = 985, 190, 138
    parts = [f'<rect width="{w}" height="{h}" fill="url(#dots)" opacity="0.5"/>']
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="230" fill="url(#glow)"/>')

    nodes = [*live, *[v for v in venues if not v["enabled"]]]
    for index, venue in enumerate(nodes):
        angle = -math.pi / 2 + 2 * math.pi * index / len(nodes)
        x, y = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        closed = not venue["enabled"]
        colour = DIM if closed else (VIOLET if venue["egress"] == "relay" else TEAL)
        dash = ' stroke-dasharray="3 5"' if closed else ""
        stroke = DIM if closed else "url(#spoke)"
        parts.append(
            f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{cx}" y2="{cy}" stroke="{stroke}" '
            f'stroke-width="1.4"{dash}/>'
        )
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="{BG0}" stroke="{colour}" '
            f'stroke-width="2"{dash}/>'
        )
        if not closed:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{colour}"/>')

    hexagon = " ".join(
        f"{cx + 52 * math.cos(math.pi / 6 + i * math.pi / 3):.1f},"
        f"{cy + 52 * math.sin(math.pi / 6 + i * math.pi / 3):.1f}"
        for i in range(6)
    )
    parts.append(
        f'<polygon points="{hexagon}" fill="{CARD}" stroke="url(#brand)" stroke-width="2.5"/>'
    )
    parts.append(t(cx, cy - 4, "one", size=13, fill=MUTED, anchor="middle"))
    parts.append(t(cx, cy + 16, "market state", size=14, fill=TEXT, weight=700, anchor="middle"))

    parts.append(t(64, 128, "Vizanix", size=30, fill=MUTED, weight=600, extra='letter-spacing="6"'))
    parts.append(t(60, 205, "Atlas", size=96, fill="url(#brand)", weight=800))
    parts.append(
        t(64, 250, "The semantic layer for global crypto markets.", size=26, fill=TEXT, weight=500)
    )
    parts.append(
        t(64, 284, "Exchanges provide events. Atlas provides market state.", size=18, fill=MUTED)
    )

    chips = [f"{count} venues", "spot · perp · futures · options", "reproducible", "open data"]
    x = 64
    for chip in chips:
        width = 22 + 8.4 * len(chip)
        parts.append(rect(x, 312, width, 34, r=17, fill=CARD, stroke=LINE))
        parts.append(t(x + width / 2, 334, chip, size=14, fill=TEXT, weight=600, anchor="middle"))
        x += width + 12

    parts.append(rect(cx - 150, h - 36, 8, 8, r=4, fill=TEAL))
    parts.append(t(cx - 136, h - 28, "collected by CI runners", size=12, fill=MUTED))
    parts.append(rect(cx + 18, h - 36, 8, 8, r=4, fill=VIOLET))
    parts.append(t(cx + 32, h - 28, "collected by the relay", size=12, fill=MUTED))
    return svg(
        w,
        h,
        "\n".join(parts),
        title="Vizanix Atlas",
        desc=f"{count} venues feed one reproducible market state per asset.",
    )


# --------------------------------------------------------------------- architecture


def arrow(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    colour: str = MUTED,
    dash: str = "",
    marker: str = "arrow",
    curve: float = 0.0,
) -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    if curve:
        mx, my = (x1 + x2) / 2 + curve, (y1 + y2) / 2
        path = f"M{x1} {y1} Q{mx} {my} {x2} {y2}"
    else:
        path = f"M{x1} {y1} L{x2} {y2}"
    return (
        f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="2"{dash_attr} '
        f'marker-end="url(#{marker})"/>'
    )


def box(
    x: float, y: float, w: float, h: float, title: str, lines: list[str], *, accent: str = TEAL
) -> str:
    out = [rect(x, y, w, h, r=14, fill=CARD, stroke=LINE)]
    out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="6" height="{h:.1f}" rx="3" fill="{accent}"/>')
    out.append(t(x + 22, y + 32, title, size=17, weight=700))
    for index, line in enumerate(lines):
        out.append(t(x + 22, y + 58 + index * 21, line, size=13.5, fill=MUTED))
    return "\n".join(out)


def architecture(venues: list[dict]) -> str:
    w, h = 1280, 700
    direct = sum(1 for v in venues if v["enabled"] and v["egress"] == "direct")
    relay = [v["name"] for v in venues if v["enabled"] and v["egress"] == "relay"]
    p = [f'<rect width="{w}" height="{h}" fill="url(#dots)" opacity="0.35"/>']
    p.append(t(64, 62, "How a snapshot is made", size=26, weight=800))
    p.append(
        t(
            64,
            88,
            "Every 15 minutes, best effort. A failed venue degrades a generation; it never fails one.",
            size=15,
            fill=MUTED,
        )
    )

    p.append(
        box(
            64,
            120,
            520,
            112,
            f"CI runners: {direct} direct venues",
            [
                "One matrix job per venue, public endpoints only.",
                "Bulk requests, per-venue rate limits, a request ceiling.",
            ],
            accent=TEAL,
        )
    )
    p.append(
        box(
            696,
            120,
            520,
            112,
            "Relay collector (non-US network)",
            [
                " · ".join(relay) + " refuse US runners.",
                "Same adapters, same result format, pulled over HTTPS.",
            ],
            accent=VIOLET,
        )
    )

    p.append(arrow(324, 232, 560, 292, colour=TEAL, marker="arrow-teal"))
    p.append(arrow(956, 232, 720, 292, colour=VIOLET, marker="arrow-violet"))
    p.append(t(972, 262, "checksum · schema · venue · freshness", size=12, fill=VIOLET))

    p.append(
        box(
            64,
            292,
            1152,
            104,
            "Aggregate",
            [
                "Identity resolution (conservative)  →  quote-conversion graph  →  validation and quarantine"
                "  →  Universal Market State",
                "Reference price · dispersion · volume · funding · open interest · basis · liquidity · history metrics",
            ],
            accent=BLUE,
        )
    )

    p.append(arrow(640, 396, 640, 440))
    p.append(
        box(
            64,
            440,
            380,
            96,
            "Validity gate",
            [
                "Anomaly guards against the last generation.",
                "Refused? The previous one stays canonical.",
            ],
            accent=AMBER,
        )
    )
    p.append(arrow(444, 488, 520, 488, colour=TEAL, marker="arrow-teal"))
    p.append(
        box(
            520,
            440,
            696,
            96,
            "GitHub Releases",
            [
                "atlas-data-<id>: sharded Parquet + manifest with per-file SHA-256",
                "data-latest: latest.json · books-plan.json · history store",
            ],
            accent=TEAL,
        )
    )

    p.append(arrow(868, 536, 868, 584))
    p.append(rect(64, 584, 1152, 70, r=14, fill=CARD, stroke=LINE))
    consumers = [
        "Python SDK",
        "atlas CLI",
        "MQL (local DuckDB)",
        "Static website",
        "pip install vizanix-atlas",
    ]
    x = 92
    for name in consumers:
        width = 26 + 8.6 * len(name)
        p.append(rect(x, 604, width, 30, r=15, fill=BG1, stroke=LINE))
        p.append(t(x + width / 2, 624, name, size=14, weight=600, anchor="middle"))
        x += width + 14

    # Feedback loops: what one run leaves for the next.
    p.append(arrow(1216, 488, 1248, 488, colour=BLUE, dash="5 5"))
    p.append(
        f'<path d="M1248 488 L1248 176 L1216 176" fill="none" stroke="{BLUE}" stroke-width="2" '
        f'stroke-dasharray="5 5" marker-end="url(#arrow)"/>'
    )
    p.append(
        t(
            1236,
            340,
            "books plan",
            size=12,
            fill=BLUE,
            anchor="middle",
            extra='transform="rotate(-90 1236 340)"',
        )
    )
    # The history store loops from the releases, under the gate box, back into the aggregate.
    p.append(
        f'<path d="M620 536 L620 560 L40 560 L40 344 L64 344" fill="none" stroke="{BLUE}" '
        f'stroke-width="2" stroke-dasharray="5 5" marker-end="url(#arrow)"/>'
    )
    p.append(t(330, 553, "history store", size=12, fill=BLUE, anchor="middle"))
    return svg(
        w,
        h,
        "\n".join(p),
        title="Atlas architecture",
        desc="Collection from runners and a relay, aggregation, validity gate, GitHub Releases, consumers.",
    )


# ------------------------------------------------------------------------ coverage


def coverage(venues: list[dict]) -> str:
    row = 29
    top = 150
    w = 1100
    h = top + row * len(venues) + 74
    p = [f'<rect width="{w}" height="{h}" fill="url(#dots)" opacity="0.35"/>']
    live = [v for v in venues if v["enabled"]]
    p.append(t(48, 58, "Venue coverage", size=26, weight=800))
    p.append(
        t(
            48,
            86,
            f"{len(live)} venues collected from public endpoints. Read from config/exchanges.yaml.",
            size=15,
            fill=MUTED,
        )
    )

    columns = [
        ("Spot", "spot"),
        ("Perp", "perp"),
        ("Futures", "future"),
        ("Options", "option"),
        ("Order book", "book"),
        ("Funding / OI", "deriv"),
    ]
    x0, step = 330, 100
    for index, (label, _) in enumerate(columns):
        p.append(
            t(x0 + index * step, top - 26, label, size=13, fill=MUTED, weight=600, anchor="middle")
        )
    p.append(t(w - 150, top - 26, "Collected by", size=13, fill=MUTED, weight=600, anchor="middle"))
    p.append(f'<line x1="40" y1="{top - 10}" x2="{w - 40}" y2="{top - 10}" stroke="{LINE}"/>')

    for index, v in enumerate(venues):
        y = top + index * row + 18
        if index % 2 == 0:
            p.append(rect(40, y - 19, w - 80, row, r=6, fill=CARD, opacity=0.55))
        dim = not v["enabled"]
        p.append(t(56, y, v["name"], size=15, fill=DIM if dim else TEXT, weight=600))
        for col, (_, key) in enumerate(columns):
            cx = x0 + col * step
            if dim:
                p.append(
                    f'<line x1="{cx - 5}" y1="{y - 5}" x2="{cx + 5}" y2="{y - 5}" stroke="{DIM}" stroke-width="2"/>'
                )
            elif v[key]:
                p.append(f'<circle cx="{cx}" cy="{y - 5}" r="6" fill="{TEAL}"/>')
            else:
                p.append(
                    f'<circle cx="{cx}" cy="{y - 5}" r="5" fill="none" stroke="{LINE}" stroke-width="1.6"/>'
                )
        if dim:
            label, colour = "closed", ROSE
        elif v["egress"] == "relay":
            label, colour = "relay", VIOLET
        else:
            label, colour = "runner", TEAL
        px = w - 150
        p.append(rect(px - 38, y - 19, 76, 22, r=11, fill=BG1, stroke=colour))
        p.append(t(px, y - 3, label, size=12.5, fill=colour, weight=700, anchor="middle"))

    foot = top + row * len(venues) + 30
    p.append(f'<circle cx="56" cy="{foot}" r="6" fill="{TEAL}"/>')
    p.append(t(70, foot + 5, "supported", size=13, fill=MUTED))
    p.append(f'<circle cx="170" cy="{foot}" r="5" fill="none" stroke="{LINE}" stroke-width="1.6"/>')
    p.append(t(184, foot + 5, "not offered by the venue", size=13, fill=MUTED))
    closed = [v["name"] for v in venues if not v["enabled"]]
    if closed:
        p.append(
            t(
                w - 48,
                foot + 5,
                f"{', '.join(closed)}: ceased trading in September 2026.",
                size=13,
                fill=ROSE,
                anchor="end",
            )
        )
    return svg(
        w,
        h,
        "\n".join(p),
        title="Venue coverage",
        desc="Which markets, order books and derivative data each venue provides, and who collects it.",
    )


# ------------------------------------------------------------------------ snapshot


def snapshot_chart() -> str:
    data = json.loads((ASSETS / "data" / "btc-snapshot.json").read_text(encoding="utf-8"))
    rows = data["venues"]
    rows_max = max(
        sum(1 for r in rows if r.get(key) is not None)
        for key in ("open_interest_usd", "funding_8h")
    )
    panel_h = 84 + 30 * rows_max
    w, h = 1100, 116 + panel_h + 64
    p = [f'<rect width="{w}" height="{h}" fill="url(#dots)" opacity="0.35"/>']
    p.append(t(48, 58, "One asset, every venue", size=26, weight=800))
    p.append(
        t(
            48,
            86,
            f"BTC from a real run, generation {data['generation_id']}. "
            "9 of the 18 venues, so totals here are partial.",
            size=15,
            fill=MUTED,
        )
    )

    def panel(x: float, title: str, key: str, fmt, colour: str, scale: float = 1.0) -> list[str]:
        values = [(r["venue"], r[key] * scale) for r in rows if r.get(key) is not None]
        values.sort(key=lambda item: -abs(item[1]))
        out = [
            rect(x, 116, 490, panel_h, r=14, fill=CARD, stroke=LINE),
            t(x + 24, 152, title, size=17, weight=700),
        ]
        peak = max((abs(v) for _, v in values), default=1) or 1
        for index, (venue, value) in enumerate(values):
            y = 184 + index * 30
            bar = max(2.0, 250 * abs(value) / peak)
            out.append(t(x + 24, y + 14, venue.replace("_", " "), size=14, fill=MUTED))
            out.append(rect(x + 150, y, bar, 20, r=6, fill=colour, opacity=0.9))
            out.append(
                t(
                    x + 150 + bar + 10,
                    y + 15,
                    fmt(value),
                    size=13.5,
                    fill=TEXT,
                    weight=600,
                    family=MONO,
                )
            )
        return out

    p += panel(48, "Open interest (USD)", "open_interest_usd", human, TEAL)
    p += panel(
        562,
        "Funding per 8 hours (basis points)",
        "funding_8h",
        lambda v: f"{v:+.2f} bp",
        BLUE,
        10_000,
    )
    p.append(
        t(
            48,
            116 + panel_h + 36,
            "Venues without derivatives (Coinbase, Kraken, Bitstamp, KuCoin) are absent from both panels "
            "by design: Atlas shows nothing rather than a zero.",
            size=13,
            fill=MUTED,
        )
    )
    return svg(
        w,
        h,
        "\n".join(p),
        title="BTC across venues",
        desc="Open interest and 8-hour funding for BTC by venue from a real Atlas run.",
    )


# -------------------------------------------------------------------------- main


def render() -> dict[str, str]:
    venues = load_venues()
    return {
        "hero.svg": hero(venues),
        "architecture.svg": architecture(venues),
        "coverage.svg": coverage(venues),
        "btc-snapshot.svg": snapshot_chart(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if committed files differ")
    args = parser.parse_args()

    rendered = render()
    if args.check:
        stale = [
            name
            for name, content in rendered.items()
            if not (ASSETS / name).is_file()
            or (ASSETS / name).read_text(encoding="utf-8").replace("\r\n", "\n") != content
        ]
        if stale:
            print("README graphics are out of date: " + ", ".join(stale))
            print("Run: python scripts/render_readme_assets.py")
            return 1
        print("README graphics are current.")
        return 0

    ASSETS.mkdir(parents=True, exist_ok=True)
    for name, content in rendered.items():
        (ASSETS / name).write_text(content, encoding="utf-8", newline="\n")
        print(f"wrote docs/assets/{name} ({len(content) // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
