"""Venue-specific semantics that would be easy to get silently wrong.

Each test here encodes a fact verified against the live venue at fixture-capture time.
They exist so that a refactor cannot quietly change the meaning of a published number.
"""

from __future__ import annotations

import pytest

from tests.conftest import RouteRecorder, load_fixture, make_client, venue_for
from tests.contract.test_all_adapters import build
from vizanix_atlas.models.enums import InstrumentType, OpenInterestUnit


async def test_deribit_untraded_options_stay_null_not_zero(routes: RouteRecorder) -> None:
    """An option with no trades reports null, and Atlas must not read it as zero.

    This is the single clearest case of the rule that unknown and zero are different
    values. Deribit returns ``null`` for last, high and low on an untraded strike.
    """
    adapter = build("deribit", routes)
    tickers = {t.symbol_native: t for t in await adapter.fetch_tickers()}

    untraded = tickers["BTC-28SEP26-88000-P"]
    assert untraded.last_price is None, "an untraded option's last price is absent, not zero"
    assert untraded.high_24h is None
    assert untraded.low_24h is None
    # The quote is still real, and volume of zero is a genuine measurement.
    assert untraded.bid_price is not None and untraded.bid_price > 0
    assert untraded.ask_price is not None and untraded.ask_price > 0
    assert untraded.base_volume_24h == 0.0


async def test_deribit_excludes_calendar_spreads(routes: RouteRecorder) -> None:
    """A future_combo is a spread, not an outright instrument.

    Including one would count the same exposure twice, since a spread is built from
    contracts that are themselves listed.
    """
    adapter = build("deribit", routes)
    instruments = await adapter.discover_instruments()
    symbols = {i.symbol_native for i in instruments}

    # The fixture deliberately contains a combo record.
    raw = load_fixture("deribit", "instruments_btc")["result"]
    combos = {r["instrument_name"] for r in raw if r["kind"].endswith("_combo")}
    assert combos, "the fixture must contain a combo for this test to mean anything"
    assert not (combos & symbols), f"calendar spreads must not be collected: {combos & symbols}"


async def test_deribit_perpetual_carries_no_expiry(routes: RouteRecorder) -> None:
    """Deribit gives perpetuals a sentinel expiry in the year 2999.

    Passing that through would make a perpetual look like a dated contract.
    """
    adapter = build("deribit", routes)
    instruments = {i.symbol_native: i for i in await adapter.discover_instruments()}

    perpetual = instruments["BTC-PERPETUAL"]
    assert perpetual.instrument_type == InstrumentType.PERPETUAL.value
    assert perpetual.expiry is None
    assert perpetual.contract_type == "inverse"
    # Inverse contracts report open interest as notional USD, not as a contract count.
    assert perpetual.open_interest_unit == OpenInterestUnit.USD.value
    # Funding is published as an 8-hour equivalent, so the interval is 8 hours.
    assert perpetual.funding_interval_hours == pytest.approx(8.0)


async def test_krakenfutures_distinguishes_perpetual_from_dated(routes: RouteRecorder) -> None:
    """Both Kraken Futures product families mix perpetual and dated contracts.

    The discriminator is which fields the venue populates: a perpetual carries
    ``fundingRateCoefficient``; a dated contract carries ``lastTradingTime``.
    """
    adapter = build("krakenfutures", routes)
    instruments = {i.symbol_native: i for i in await adapter.discover_instruments()}

    linear_perp = instruments["PF_XBTUSD"]
    assert linear_perp.instrument_type == InstrumentType.PERPETUAL.value
    assert linear_perp.contract_type == "linear"
    assert linear_perp.expiry is None
    # Funding is hourly, confirmed from consecutive hourly historical records.
    assert linear_perp.funding_interval_hours == pytest.approx(1.0)
    # The published number is an absolute rate, not a relative one.
    assert linear_perp.funding_semantics == "absolute_per_interval"

    inverse_perp = instruments["PI_XBTUSD"]
    assert inverse_perp.instrument_type == InstrumentType.PERPETUAL.value
    assert inverse_perp.contract_type == "inverse"
    assert inverse_perp.expiry is None
    # An inverse contract settles in the base asset.
    assert inverse_perp.settlement_symbol_native == "BTC"


async def test_krakenfutures_absolute_funding_is_preserved_unconverted(
    routes: RouteRecorder,
) -> None:
    """The adapter stores the venue's absolute rate; conversion happens downstream.

    Converting inside the adapter would lose the raw value and put the arithmetic
    somewhere the instrument's contract type is not available.
    """
    adapter = build("krakenfutures", routes)
    observations = {d.symbol_native: d for d in await adapter.fetch_derivatives()}
    raw = {t["symbol"]: t for t in load_fixture("krakenfutures", "tickers")["tickers"]}

    for symbol in ("PF_XBTUSD", "PI_XBTUSD"):
        assert observations[symbol].funding_rate_raw == raw[symbol]["fundingRate"]
        assert observations[symbol].funding_interval_hours == pytest.approx(1.0)


async def test_coinbase_publishes_no_quote_volume_and_no_timestamp(
    routes: RouteRecorder,
) -> None:
    """Coinbase's bulk stats endpoint has neither, and Atlas must not invent either."""
    adapter = build("coinbase", routes)
    tickers = {t.symbol_native: t for t in await adapter.fetch_tickers()}

    btc = tickers["BTC-USD"]
    assert btc.base_volume_24h is not None and btc.base_volume_24h > 0
    # Multiplying base volume by last price would fabricate a figure Coinbase never
    # reported and would differ from how every other venue computes it.
    assert btc.quote_volume_24h is None
    # No venue timestamp exists on this endpoint.
    assert btc.timing.exchange_event_time is None
    assert btc.timing.exchange_server_time is None
    assert btc.timing.collector_receive_time > 0
    # There is no bid or ask either, so no spread is available at tier A.
    assert btc.bid_price is None and btc.ask_price is None
    assert btc.spread_bps is None
    # The declared capability must match that behaviour.
    assert venue_for("coinbase").capabilities.top_of_book_in_bulk is False
    assert venue_for("coinbase").capabilities.quote_volume_in_bulk is False


async def test_kraken_resolves_legacy_asset_codes(routes: RouteRecorder) -> None:
    """XXBTZUSD must resolve to XBT and USD, not to XXBT and ZUSD."""
    adapter = build("kraken", routes)
    instruments = {i.symbol_native: i for i in await adapter.discover_instruments()}

    btc_usd = instruments["XXBTZUSD"]
    assert btc_usd.base_symbol_native == "XBT"
    assert btc_usd.quote_symbol_native == "USD"
    # Kraken publishes a 24h VWAP but no quote volume; the VWAP is not multiplied out.
    tickers = {t.symbol_native: t for t in await adapter.fetch_tickers()}
    assert tickers["XXBTZUSD"].base_volume_24h is not None
    assert tickers["XXBTZUSD"].quote_volume_24h is None


async def test_htx_volume_field_names_are_not_inverted(routes: RouteRecorder) -> None:
    """HTX names its volume fields the opposite way round to most venues.

    ``amount`` is base volume and ``vol`` is quote volume. Reading them the usual way
    would inflate reported volume by the asset's price.
    """
    adapter = build("htx", routes)
    tickers = {t.symbol_native: t for t in await adapter.fetch_tickers()}
    raw = {r["symbol"]: r for r in load_fixture("htx", "tickers")["data"]}

    btc = tickers["btcusdt"]
    assert btc.base_volume_24h == raw["btcusdt"]["amount"]
    assert btc.quote_volume_24h == raw["btcusdt"]["vol"]
    # Sanity: for a market priced in the tens of thousands, quote volume must dominate.
    assert btc.quote_volume_24h > btc.base_volume_24h


async def test_bitfinex_resolves_contractions_from_venue_published_maps(
    routes: RouteRecorder,
) -> None:
    """Bitfinex's UST is Tether, and the venue's own map is what says so.

    Resolving this from a hand-written guess is how an identity layer merges TerraUSD
    into Tether. Atlas reads the venue's published currency maps instead.
    """
    adapter = build("bitfinex", routes)
    instruments = {i.symbol_native: i for i in await adapter.discover_instruments()}

    # ALG is Algorand per pub:map:currency:sym.
    algo = next(i for i in instruments.values() if i.symbol_native == "tALGUSD")
    assert algo.base_symbol_native == "ALGO"
    assert algo.base_name == "Algorand"

    # UST carries the venue's own label, which is the evidence that it is Tether.
    ust = next(i for i in instruments.values() if i.symbol_native == "tUSTUSD")
    assert ust.base_name == "Tether USDt"
    assert ust.base_symbol_native != "UST", (
        "the venue's symbol map must be applied so UST is not left ambiguous"
    )


async def test_mexc_carries_contract_addresses_as_identity_evidence(
    routes: RouteRecorder,
) -> None:
    """MEXC publishes contract addresses, which is the strongest evidence available."""
    adapter = build("mexc", routes)
    instruments = await adapter.discover_instruments()

    with_address = [i for i in instruments if i.base_contract_address]
    assert with_address, "MEXC discovery must carry contract addresses through"
    with_name = [i for i in instruments if i.base_name]
    assert with_name, "MEXC discovery must carry asset names through"


async def test_okx_option_family_rejection_does_not_fail_the_venue(
    routes: RouteRecorder,
) -> None:
    """OKX lists option families that its own instruments endpoint then rejects.

    Losing one option family must not cost the other few thousand instruments.
    """
    from tests.contract.wiring import WIRING
    from vizanix_atlas.adapters.okx import OkxAdapter

    WIRING["okx"](routes)
    # Two extra families, both of which the instruments endpoint will reject with the
    # venue's own 51000 error, exactly as observed live for SOL-USD and XAU-USD.
    routes.routes.insert(
        0,
        (
            lambda r: r.url.path == "/api/v5/public/underlying",
            __import__("httpx").Response(
                200,
                json={"code": "0", "msg": "", "data": [["BTC-USD", "SOL-USD", "XAU-USD"]]},
            ),
        ),
    )
    routes.routes.insert(
        0,
        (
            lambda r: (
                r.url.path == "/api/v5/public/instruments"
                and r.url.params.get("instFamily") in ("SOL-USD", "XAU-USD")
            ),
            __import__("httpx").Response(
                200, json={"code": "51000", "data": [], "msg": "Parameter instFamily error"}
            ),
        ),
    )
    client = make_client(routes, venue_slug="okx", base_url=OkxAdapter.base_url)
    adapter = OkxAdapter(venue_for("okx"), client)

    instruments = await adapter.discover_instruments()
    assert instruments, "a rejected option family must not empty the whole catalogue"
    # The rejection is recorded rather than swallowed silently.
    assert any(
        "instFamily.rejected" in key for key in adapter.client.metrics.unknown_enum_values
    ), adapter.client.metrics.unknown_enum_values
