"""The live layer: venue WebSocket feeds merged into a continuously updated reference price.

Separate from the dataset pipeline on purpose. A dataset generation is versioned,
reproducible and replayable; the live layer is a moving view with no history, which borrows
its identity mapping from the latest generation and never contradicts it. See
``docs/STREAMING.md``.
"""
