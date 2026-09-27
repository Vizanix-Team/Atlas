"""Publication: manifests, validation, and the Publisher protocol.

Nothing in this package computes analytics or touches the physical Parquet layout;
:mod:`vizanix_atlas.storage` does that. This package answers "is this generation safe
to publish" and "how does it get from local disk to a durable, discoverable location"
(see ``docs/STORAGE.md`` and ``docs/GITHUB_ARCHITECTURE.md``).
"""

from __future__ import annotations
