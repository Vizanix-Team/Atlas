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

None of these are blocked by anything Atlas could route around, and Atlas does not
try (see ``docs/DATA_POLICY.md``). Each is waiting on a fixture captured from a
network where the venue answers normally.
"""

from __future__ import annotations

from typing import ClassVar

from vizanix_atlas.adapters.base import ExchangeAdapter
from vizanix_atlas.core.errors import UnsupportedCapability
from vizanix_atlas.models.observations import RawInstrument, RawTicker


class DisabledAdapter(ExchangeAdapter):
    """Base for an adapter that cannot be implemented against a verified schema.

    Every operation raises :class:`UnsupportedCapability` carrying the blocker, so a
    caller that reaches one gets an explanation rather than silence.
    """

    #: Why this adapter is not implemented. Stated in full in ``config/exchanges.yaml``.
    blocker: ClassVar[str]

    def _refuse(self) -> UnsupportedCapability:
        """Build the error every operation raises."""
        return UnsupportedCapability(
            f"the {self.slug} adapter is disabled and has no verified implementation",
            venue=self.slug,
            blocker=self.blocker,
        )

    async def discover_instruments(self) -> list[RawInstrument]:
        """Raise, because no verified catalogue schema exists for this venue."""
        raise self._refuse()

    async def fetch_tickers(self) -> list[RawTicker]:
        """Raise, because no verified ticker schema exists for this venue."""
        raise self._refuse()


class BybitAdapter(DisabledAdapter):
    """Bybit. Blocked at the venue's edge from the network used to build this release."""

    slug = "bybit"
    base_url = "https://api.bybit.com"
    blocker = (
        "All Bybit API hosts returned HTTP 403 from a CloudFront edge with the body "
        "'The Amazon CloudFront distribution is configured to block access from your "
        "country'. No live payload could be captured, so no parser could be written "
        "against a verified schema."
    )


class BitmexAdapter(DisabledAdapter):
    """BitMEX. Reachable, but its catalogue was not representative of the venue."""

    slug = "bitmex"
    base_url = "https://www.bitmex.com"
    blocker = (
        "The instrument catalogue returned from the network used to build this release "
        "was not representative: /instrument/active returned 12 unlisted placeholder "
        "pairs listed for the year 2200, /instrument/activeIntervals returned empty "
        "arrays, and XBTUSD was reported with state 'Settled'. An adapter cannot be "
        "validated against a catalogue that does not reflect the venue's real state."
    )


class BinanceDerivativesAdapter(DisabledAdapter):
    """Binance USD-M and COIN-M futures. Blocked at the venue's edge."""

    slug = "binance_derivatives"
    base_url = "https://fapi.binance.com"
    blocker = (
        "fapi.binance.com and dapi.binance.com both returned HTTP 451 with the venue's "
        "jurisdiction-restriction message. Unlike Binance spot, these hosts have no "
        "market-data-only mirror, so no fixture could be captured. This materially "
        "reduces Atlas's funding and open-interest coverage; see docs/LIMITATIONS.md."
    )
