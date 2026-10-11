"""Disabled adapters.

Atlas records why a venue is not collected rather than leaving it absent. Each class
here is a real adapter class that the registry knows about and that the runtime will
refuse to run, raising an error naming the blocker. That keeps three things true:

- ``atlas exchanges`` can list the venue and explain its absence.
- The disabled reason lives in ``config/exchanges.yaml`` where it is reviewable, and
  the schema requires it (a venue with ``enabled: false`` and no ``disabled_reason``
  fails configuration validation).
- Nothing here is a mock that could be mistaken for a working implementation. A
  disabled adapter raises; it never returns plausible-looking empty data.

Today the only entry is BitMEX, which is not blocked by a network or a missing fixture:
the exchange itself has closed.
"""

from __future__ import annotations

from typing import ClassVar

from vizanix_atlas.adapters.base import ExchangeAdapter
from vizanix_atlas.core.errors import UnsupportedCapability
from vizanix_atlas.models.observations import RawInstrument, RawTicker


class DisabledAdapter(ExchangeAdapter):
    """Base for an adapter that cannot be implemented against a live venue.

    Every operation raises :class:`UnsupportedCapability` carrying the blocker, so a
    caller that reaches one gets an explanation rather than silence.
    """

    #: Why this adapter is disabled. Stated in full in ``config/exchanges.yaml``.
    blocker: ClassVar[str]

    def _refuse(self) -> UnsupportedCapability:
        """Build the error every operation raises."""
        return UnsupportedCapability(
            f"the {self.slug} adapter is disabled",
            venue=self.slug,
            blocker=self.blocker,
        )

    async def discover_instruments(self) -> list[RawInstrument]:
        """Raise, because there is no live catalogue to read."""
        raise self._refuse()

    async def fetch_tickers(self) -> list[RawTicker]:
        """Raise, because there are no live markets to quote."""
        raise self._refuse()


class BitmexAdapter(DisabledAdapter):
    """BitMEX. The exchange ceased operations in September 2026."""

    slug = "bitmex"
    base_url = "https://www.bitmex.com"
    blocker = (
        "BitMEX has closed. Its own announcements (bitmex.com/blog/bitmex-closure, "
        "bitmex.com/blog/bitmex-closure-live) state that the exchange ceased all trading "
        "in September 2026. Observed 2026-10-11 from a network that is not restricted: "
        "/api/v1/instrument/active lists only 12 unlisted placeholder spot pairs dated "
        "2200, every other instrument (XBTUSD included) is in state Settled, the order "
        "book is empty and the last XBTUSD trade is the 2026-09-16 settlement. There is "
        "no live market to collect."
    )
