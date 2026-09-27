"""Storage: turning a generation into tabular files, and reading them back.

This package is the only place that knows Atlas's physical layout - Parquet schemas,
sharding, and file naming. Analytics and identity code never import it, and it never
imports anything about GitHub: publication is a separate concern in
:mod:`vizanix_atlas.publishing` (see ``docs/STORAGE.md``).
"""

from __future__ import annotations
