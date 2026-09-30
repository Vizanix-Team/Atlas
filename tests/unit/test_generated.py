"""Committed generated files must match what the code generates today.

A failure means a metric or schema changed without regenerating ``docs/METRICS.md`` or
``schemas/`` (``python scripts/generate.py``). The regenerated diff is where a reviewer
sees the change and where a schema-version bump is decided.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_generated_files_are_current() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import generate
    finally:
        sys.path.pop(0)
    stale = [
        str(path.relative_to(ROOT))
        for path, content in generate.targets().items()
        if not path.is_file() or path.read_text(encoding="utf-8") != content
    ]
    assert not stale, f"stale generated files, run `python scripts/generate.py`: {stale}"
