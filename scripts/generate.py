"""Regenerate (or with --check, verify) docs/METRICS.md and the schemas/ directory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vizanix_atlas.util.generate import (  # noqa: E402
    render_arrow_schemas,
    render_json_schemas,
    render_metrics_markdown,
)


def targets() -> dict[Path, str]:
    """Every generated file and its expected content."""
    files = {ROOT / "docs" / "METRICS.md": render_metrics_markdown()}
    files.update({ROOT / "schemas" / "json" / n: c for n, c in render_json_schemas().items()})
    files.update({ROOT / "schemas" / "arrow" / n: c for n, c in render_arrow_schemas().items()})
    return files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if generated files are stale")
    args = parser.parse_args()
    stale = []
    for path, content in targets().items():
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                stale.append(path.relative_to(ROOT))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if stale:
        print("stale generated files (run `python scripts/generate.py`):")
        for path in stale:
            print(f"  {path}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
