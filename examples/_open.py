"""Shared helper: open a local dataset given on the command line, else the latest release."""

from __future__ import annotations

import sys

from vizanix_atlas import Atlas


def open_atlas() -> Atlas:
    """Open ``sys.argv[1]`` as a local dataset, or fall back to the latest release."""
    if len(sys.argv) > 1:
        return Atlas.from_local(sys.argv[1])
    return Atlas.latest()
