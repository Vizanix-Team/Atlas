"""Version identities.

Atlas keeps four version concepts apart, because overloading one number makes it
impossible to answer "did this number change because the code changed or because
the formula changed?" (see ``docs/OPERATIONS.md``).

``SOFTWARE_VERSION``
    The Python package. Semantic versioning. Changes on any release.
``SCHEMA_VERSION``
    The published table and manifest shape. Semantic versioning. A field removal
    or type change is a major bump and is enforced by a golden-schema test.
``METHODOLOGY_VERSION``
    The derivation formulas. Changing how the global reference price is computed
    bumps this even if no schema field changes, so that two generations are never
    silently compared across a formula change.
``DATASET_FORMAT_VERSION``
    The physical layout: file naming, sharding scheme, compression, manifest
    location. Independent of the logical schema.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Final

SOFTWARE_VERSION: Final = "0.1.0"
SCHEMA_VERSION: Final = "1.0.0"
METHODOLOGY_VERSION: Final = "1.0.0"
DATASET_FORMAT_VERSION: Final = "1.0.0"

#: Wire format version for the MQL semantic plan, reported by ``atlas query --explain``.
MQL_VERSION: Final = "1.0.0"


@lru_cache(maxsize=1)
def commit_sha() -> str | None:
    """Return the git commit the running code came from, or ``None``.

    Recorded in every generation manifest so that a published dataset can be tied
    back to the exact source that produced it (see ``docs/SPECIFICATION.md``).

    Prefers ``GITHUB_SHA``, which is authoritative inside Actions and correct even
    for a detached-HEAD checkout. Falls back to ``git rev-parse`` for local runs
    and returns ``None`` outside a repository rather than inventing a value.
    """
    from_env = os.environ.get("GITHUB_SHA")
    if from_env:
        return from_env.strip()
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git resolved from PATH by design
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parent,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def python_version() -> str:
    """Return the interpreter version that produced a generation."""
    import sys

    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def version_summary() -> dict[str, str | None]:
    """Return every version identity, for embedding in manifests and ``atlas status``."""
    return {
        "software_version": SOFTWARE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "methodology_version": METHODOLOGY_VERSION,
        "dataset_format_version": DATASET_FORMAT_VERSION,
        "mql_version": MQL_VERSION,
        "commit_sha": commit_sha(),
        "python_version": python_version(),
    }
