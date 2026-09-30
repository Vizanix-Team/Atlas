"""A small, real :class:`Generation` built from hand-constructed observations.

Used by the test suite, the schema generator and the website fixture script, so all three
describe the same dataset shape. It runs the genuine identity, normalisation and analytics
pipeline; only the venue observations are fabricated, which is why nothing built from it
may ever be presented as market data.
"""

from __future__ import annotations

from vizanix_atlas.core.atlas_time import to_epoch_ms, utc_now
from vizanix_atlas.core.config import load_collection_config
from vizanix_atlas.discovery.pipeline import Generation, build_generation
from vizanix_atlas.models.enums import PriceSource
from vizanix_atlas.models.observations import (
    AdapterHealth,
    CollectionResult,
    ObservationTiming,
    RawFxObservation,
    RawInstrument,
    RawTicker,
)

T0 = to_epoch_ms(utc_now())


def _timing() -> ObservationTiming:
    return ObservationTiming(collector_receive_time=T0, exchange_event_time=T0)


def _health(venue: str, *, tickers: int, instruments: int) -> AdapterHealth:
    return AdapterHealth(
        venue_slug=venue,
        status="success",
        duration_ms=100,
        requests_attempted=2,
        requests_successful=2,
        instrument_count=instruments,
        ticker_count=tickers,
    )


def build_sample_generation(
    *,
    price_multiplier: float = 1.0,
    extra_asset: bool = False,
    effective_time: int | None = None,
) -> Generation:
    """Build a small, real Generation from hand-constructed collection results.

    Runs the actual identity, normalisation and analytics pipeline end to end, just
    against fabricated rather than live-fetched observations, so publishing tests
    exercise the real data shape without any network dependency.
    """
    config = load_collection_config()
    instruments = [
        RawInstrument(
            venue_slug="okx",
            symbol_native="BTC-USDT",
            instrument_class="spot",
            instrument_type="spot",
            base_symbol_native="BTC",
            quote_symbol_native="USDT",
        ),
        RawInstrument(
            venue_slug="coinbase",
            symbol_native="BTC-USD",
            instrument_class="spot",
            instrument_type="spot",
            base_symbol_native="BTC",
            quote_symbol_native="USD",
        ),
    ]
    price = 84_400.0 * price_multiplier
    okx_tickers = [
        RawTicker(
            venue_slug="okx",
            symbol_native="BTC-USDT",
            timing=_timing(),
            last_price=price,
            bid_price=price - 0.5,
            ask_price=price + 0.5,
            base_volume_24h=1_000.0,
            quote_volume_24h=8.44e7,
        )
    ]
    coinbase_tickers = [
        RawTicker(
            venue_slug="coinbase",
            symbol_native="BTC-USD",
            timing=_timing(),
            last_price=price + 20.0,
            base_volume_24h=500.0,
        )
    ]
    fx = [
        RawFxObservation(
            venue_slug="coinbase",
            symbol_native="USDT-USD",
            timing=_timing(),
            from_symbol_native="USDT",
            to_symbol_native="USD",
            rate=0.9997,
            source_price=PriceSource.MID,
        )
    ]
    if extra_asset:
        instruments.append(
            RawInstrument(
                venue_slug="okx",
                symbol_native="ETH-USDT",
                instrument_class="spot",
                instrument_type="spot",
                base_symbol_native="ETH",
                quote_symbol_native="USDT",
            )
        )
        okx_tickers.append(
            RawTicker(
                venue_slug="okx",
                symbol_native="ETH-USDT",
                timing=_timing(),
                last_price=2_700.0,
                base_volume_24h=5_000.0,
                quote_volume_24h=1.35e7,
            )
        )

    results = [
        CollectionResult(
            venue_slug="okx",
            run_id="test",
            collection_started_at=T0,
            collection_finished_at=T0,
            health=_health(
                "okx",
                tickers=len(okx_tickers),
                instruments=len([i for i in instruments if i.venue_slug == "okx"]),
            ),
            instruments=tuple(i for i in instruments if i.venue_slug == "okx"),
            tickers=tuple(okx_tickers),
        ),
        CollectionResult(
            venue_slug="coinbase",
            run_id="test",
            collection_started_at=T0,
            collection_finished_at=T0,
            health=_health("coinbase", tickers=len(coinbase_tickers), instruments=1),
            instruments=tuple(i for i in instruments if i.venue_slug == "coinbase"),
            tickers=tuple(coinbase_tickers),
            fx_observations=tuple(fx),
        ),
    ]
    return build_generation(results, config=config, snapshot_effective_time=effective_time)
