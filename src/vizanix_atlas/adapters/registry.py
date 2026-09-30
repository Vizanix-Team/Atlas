"""Adapter registry.

Maps a venue slug to its adapter class, and constructs adapters with their configured
HTTP client. This is the only place that knows the full set of adapters, so adding a
venue means adding one entry here plus one entry in ``config/exchanges.yaml``.

Registry and configuration are cross-checked at import time: a venue declared in
configuration with no adapter, or an adapter with no declaration, fails fast rather
than producing a generation that silently omits a venue.
"""

from __future__ import annotations

from typing import Final

from vizanix_atlas.adapters.base import ExchangeAdapter
from vizanix_atlas.adapters.binance import BinanceSpotAdapter
from vizanix_atlas.adapters.bitfinex import BitfinexAdapter
from vizanix_atlas.adapters.bitget import BitgetAdapter
from vizanix_atlas.adapters.bitstamp import BitstampAdapter
from vizanix_atlas.adapters.coinbase import CoinbaseAdapter
from vizanix_atlas.adapters.cryptocom import CryptocomAdapter
from vizanix_atlas.adapters.deribit import DeribitAdapter
from vizanix_atlas.adapters.disabled import (
    BinanceDerivativesAdapter,
    BitmexAdapter,
    BybitAdapter,
)
from vizanix_atlas.adapters.dydx import DydxAdapter
from vizanix_atlas.adapters.gateio import GateioAdapter
from vizanix_atlas.adapters.htx import HtxAdapter
from vizanix_atlas.adapters.hyperliquid import HyperliquidAdapter
from vizanix_atlas.adapters.kraken import KrakenAdapter
from vizanix_atlas.adapters.krakenfutures import KrakenFuturesAdapter
from vizanix_atlas.adapters.kucoin import KucoinAdapter
from vizanix_atlas.adapters.mexc import MexcAdapter
from vizanix_atlas.adapters.okx import OkxAdapter
from vizanix_atlas.core.config import load_exchange_registry
from vizanix_atlas.core.errors import ConfigurationError
from vizanix_atlas.core.http import ExchangeHttpClient
from vizanix_atlas.models.venue import Venue

#: Every adapter Atlas knows about, enabled or not.
ADAPTERS: Final[dict[str, type[ExchangeAdapter]]] = {
    adapter.slug: adapter
    for adapter in (
        BinanceSpotAdapter,
        BinanceDerivativesAdapter,
        BitfinexAdapter,
        BitgetAdapter,
        BitmexAdapter,
        BitstampAdapter,
        BybitAdapter,
        CoinbaseAdapter,
        CryptocomAdapter,
        DeribitAdapter,
        DydxAdapter,
        GateioAdapter,
        HtxAdapter,
        HyperliquidAdapter,
        KrakenAdapter,
        KrakenFuturesAdapter,
        KucoinAdapter,
        MexcAdapter,
        OkxAdapter,
    )
}


def adapter_class(slug: str) -> type[ExchangeAdapter]:
    """Return the adapter class registered for ``slug``.

    Raises:
        ConfigurationError: If no adapter is registered, listing the known slugs.

    """
    try:
        return ADAPTERS[slug]
    except KeyError:
        raise ConfigurationError(
            "no adapter is registered for this venue",
            slug=slug,
            known=sorted(ADAPTERS),
        ) from None


def build_adapter(
    venue: Venue, *, base_url: str | None = None, transport: object | None = None
) -> ExchangeAdapter:
    """Construct an adapter and its HTTP client from a venue's configuration.

    Args:
        venue: The declared venue.
        base_url: Overrides the adapter's default host, for a regional endpoint.
        transport: Injected httpx transport, used by tests.

    """
    cls = adapter_class(venue.slug)
    client = ExchangeHttpClient(
        venue_slug=venue.slug,
        base_url=base_url or cls.base_url,
        policy=venue.rate_limit,
        transport=transport,  # type: ignore[arg-type]
    )
    return cls(venue, client)


def verify_registry_matches_configuration() -> None:
    """Check that the registry and ``config/exchanges.yaml`` describe the same venues.

    Raises:
        ConfigurationError: If either side has a venue the other does not. Called at
            import time so that a half-finished adapter addition fails immediately
            rather than producing a generation missing a venue.

    """
    declared = set(load_exchange_registry().slugs())
    implemented = set(ADAPTERS)
    missing_adapter = sorted(declared - implemented)
    missing_declaration = sorted(implemented - declared)
    if missing_adapter or missing_declaration:
        raise ConfigurationError(
            "the adapter registry and exchange configuration disagree",
            declared_without_adapter=missing_adapter,
            implemented_without_declaration=missing_declaration,
        )


verify_registry_matches_configuration()
