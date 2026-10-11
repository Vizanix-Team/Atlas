"""Working state that carries from one run to the next.

Besides the generations themselves, two small things must survive between runs: the
order-book plan (which markets to snapshot next) and the history store (what the windowed
metrics read). Both live as auxiliary assets of the ``data-latest`` release, outside any
generation's manifest, because they are inputs to the next run rather than published data.

Failure policy, which is the reason this is its own module:

- Neither is allowed to fail a run. The generation is already canonical by the time they
  are written, so a failure here costs the *next* run some context (no books, or a thin
  history), never the data that was just published.
- The history is never overwritten from a read that failed. If the previous state could not
  be read, the run continues without history **and does not save**, because saving a history
  rebuilt from nothing would destroy weeks of accumulated series.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from vizanix_atlas.core.errors import AtlasError, SchemaMismatch
from vizanix_atlas.core.logging import get_logger
from vizanix_atlas.discovery.books_plan import PLAN_FILENAME, BooksPlan
from vizanix_atlas.storage.history import STORE_SLOTS, HistoryStore

if TYPE_CHECKING:
    from vizanix_atlas.core.config import HistoryStoreConfig
    from vizanix_atlas.discovery.pipeline import Generation

_log = get_logger(__name__)

_PARQUET: Final = "application/vnd.apache.parquet"


class AuxiliaryStore(Protocol):
    """The two operations a publisher offers for auxiliary ``data-latest`` assets."""

    async def read_data_latest_asset(self, filename: str) -> bytes | None:
        """Return the asset's bytes, or ``None`` if it does not exist."""
        ...

    async def publish_data_latest_asset(
        self, filename: str, content: bytes, *, content_type: str = ...
    ) -> None:
        """Create or replace the asset."""
        ...


@dataclass(frozen=True, slots=True)
class LoadedHistory:
    """The history read at the start of a run, and whether it may be written back."""

    store: HistoryStore
    #: The slot the state came from, or ``None`` when there was none. The next write goes
    #: to the *other* slot, so the one just read stays intact until the new one is complete.
    source_slot: str | None
    #: ``False`` when a read failed. Such a run must not save.
    trustworthy: bool


async def load_history(publisher: object) -> LoadedHistory:
    """Read the newest valid history, or start empty.

    Both slots are read and the one with the newest snapshot wins, so a half-finished write
    to the other slot (which can only hold older or corrupt data) is harmless.
    """
    if not hasattr(publisher, "read_data_latest_asset"):
        return LoadedHistory(HistoryStore.empty(), None, trustworthy=False)
    store: AuxiliaryStore = publisher  # type: ignore[assignment]

    best: tuple[HistoryStore, str] | None = None
    for slot in STORE_SLOTS:
        try:
            content = await store.read_data_latest_asset(slot)
        except AtlasError as error:
            _log.warning(
                "history could not be read",
                extra={"slot": slot, "error_type": error.error_type},
            )
            return LoadedHistory(HistoryStore.empty(), None, trustworthy=False)
        if content is None:
            continue
        try:
            candidate = HistoryStore.from_bytes(content)
        except SchemaMismatch:
            _log.warning("history slot is corrupt and is ignored", extra={"slot": slot})
            continue
        newest = candidate.last_observed_at or 0
        if best is None or newest > (best[0].last_observed_at or 0):
            best = (candidate, slot)

    if best is None:
        return LoadedHistory(HistoryStore.empty(), None, trustworthy=True)
    return LoadedHistory(best[0], best[1], trustworthy=True)


async def save_history(
    publisher: object,
    loaded: LoadedHistory,
    generation: Generation,
    *,
    config: HistoryStoreConfig,
) -> bool:
    """Extend the history with a published generation and store it. Returns whether it saved."""
    if not loaded.trustworthy or not hasattr(publisher, "publish_data_latest_asset"):
        return False
    store: AuxiliaryStore = publisher  # type: ignore[assignment]
    extended = loaded.store.extended_with(generation, config=config)
    target = STORE_SLOTS[1] if loaded.source_slot == STORE_SLOTS[0] else STORE_SLOTS[0]
    try:
        await store.publish_data_latest_asset(target, extended.to_bytes(), content_type=_PARQUET)
    except AtlasError as error:
        _log.warning(
            "history could not be saved",
            extra={"slot": target, "error_type": error.error_type},
        )
        return False
    _log.info(
        "history saved",
        extra={"slot": target, "assets": extended.asset_count, "rows": extended.row_count},
    )
    return True


async def publish_books_plan(publisher: object, plan: BooksPlan) -> bool:
    """Publish the order-book plan for the next run. Returns whether it was stored."""
    if not hasattr(publisher, "publish_data_latest_asset"):
        return False
    store: AuxiliaryStore = publisher  # type: ignore[assignment]
    try:
        await store.publish_data_latest_asset(
            PLAN_FILENAME,
            plan.model_dump_json(indent=1).encode("utf-8"),
            content_type="application/json",
        )
    except AtlasError as error:
        _log.warning(
            "order-book plan could not be published", extra={"error_type": error.error_type}
        )
        return False
    return True
