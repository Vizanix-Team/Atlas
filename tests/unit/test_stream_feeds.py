"""Feed parsers, pinned to frames recorded from the live endpoints on 2026-10-12."""

from __future__ import annotations

import json

import pytest

from vizanix_atlas.stream.feeds import (
    FEEDS,
    BinanceFeed,
    BitgetFeed,
    BybitLinearFeed,
    BybitSpotFeed,
    CoinbaseFeed,
    CryptoComFeed,
    DeribitFeed,
    GateFeed,
    OkxFeed,
)
from vizanix_atlas.stream.types import finite, positive

NOW = 1_791_803_760_000


def test_binance_array_frame_yields_one_tick_per_market():
    frame = json.dumps(
        [
            {
                "e": "24hrMiniTicker",
                "E": 1791803723015,
                "s": "BTCUSDT",
                "c": "83416.16000000",
                "o": "83012.30",
                "h": "84352.0",
                "l": "83008.06",
                "v": "10850.08585",
                "q": "908272305.6284",
            },
            {
                "e": "24hrMiniTicker",
                "E": 1791803723017,
                "s": "ETHUSDT",
                "c": "2551.70",
                "o": "2501.25",
                "h": "2579.88",
                "l": "2500.87",
                "v": "259808.0824",
                "q": "661308125.95",
            },
        ]
    )
    parsed = BinanceFeed().parse(frame, NOW)
    assert [t.symbol for t in parsed.ticks] == ["BTCUSDT", "ETHUSDT"]
    btc = parsed.ticks[0]
    assert btc.venue == "binance" and btc.instrument_class == "spot"
    assert btc.last == pytest.approx(83416.16)
    assert btc.quote_volume_24h == pytest.approx(908272305.6284)
    assert btc.event_ms == 1791803723015
    assert btc.bid is None and btc.ask is None


def test_binance_needs_no_subscription_and_ignores_non_arrays():
    feed = BinanceFeed()
    assert feed.subscribe_frames(["BTCUSDT"]) == []
    assert feed.parse('{"result":null,"id":1}', NOW).ticks == []


def test_bybit_spot_snapshot():
    frame = (
        '{"topic":"tickers.BTCUSDT","ts":1791803757227,"type":"snapshot","cs":115298716541,'
        '"data":{"symbol":"BTCUSDT","lastPrice":"83432.9","highPrice24h":"84355.8",'
        '"lowPrice24h":"83004.5","prevPrice24h":"83004.5","volume24h":"6707.034922",'
        '"turnover24h":"561610618.14684716","price24hPcnt":"0.0052","usdIndexPrice":"83347.2"}}'
    )
    (tick,) = BybitSpotFeed().parse(frame, NOW).ticks
    assert (tick.venue, tick.instrument_class, tick.symbol) == ("bybit", "spot", "BTCUSDT")
    assert tick.last == pytest.approx(83432.9)
    assert tick.quote_volume_24h == pytest.approx(561610618.14684716)
    assert tick.event_ms == 1791803757227


def test_bybit_subscribes_in_groups_of_ten():
    frames = BybitSpotFeed().subscribe_frames([f"S{i}USDT" for i in range(23)])
    sizes = [len(json.loads(f)["args"]) for f in frames]
    assert sizes == [10, 10, 3]
    assert json.loads(frames[0])["args"][0] == "tickers.S0USDT"


def test_bybit_ignores_acknowledgements_and_pongs():
    feed = BybitSpotFeed()
    assert feed.parse('{"success":true,"ret_msg":"subscribe","op":"subscribe"}', NOW).ticks == []
    assert feed.parse('{"success":true,"ret_msg":"pong","op":"ping"}', NOW).ticks == []


def test_bybit_linear_snapshot_and_delta():
    snapshot = (
        '{"topic":"tickers.BTCUSDT","type":"snapshot","data":{"symbol":"BTCUSDT",'
        '"lastPrice":"83412.00","markPrice":"83402.80","indexPrice":"83431.05",'
        '"openInterest":"56573.116","openInterestValue":"4718356279.12","turnover24h":"3232500526.0874",'
        '"volume24h":"38612.0670","fundingIntervalHour":"8","fundingRate":"0.00003653",'
        '"bid1Price":"83412.00","bid1Size":"4.731","ask1Price":"83412.10","ask1Size":"1.613"},'
        '"cs":1,"ts":1791803758000}'
    )
    delta = (
        '{"topic":"tickers.BTCUSDT","type":"delta","data":{"symbol":"BTCUSDT","lastPrice":"83412.10",'
        '"indexPrice":"83438.24","bid1Price":"83412.00","ask1Price":"83412.10"},"cs":2,"ts":1791803758483}'
    )
    feed = BybitLinearFeed()
    (first,) = feed.parse(snapshot, NOW).ticks
    assert first.instrument_class == "linear-perp"
    assert first.mark == pytest.approx(83402.8)
    assert first.funding_rate == pytest.approx(0.00003653)
    assert first.funding_interval_hours == 8.0
    assert first.open_interest_usd == pytest.approx(4718356279.12)
    (second,) = feed.parse(delta, NOW).ticks
    # A delta states only what changed; everything else stays None for the book to keep.
    assert second.last == pytest.approx(83412.10)
    assert second.mark is None and second.funding_rate is None and second.open_interest_usd is None


def test_okx_ticker_frame_and_pong():
    frame = (
        '{"arg":{"channel":"tickers","instId":"BTC-USDT"},"data":[{"instType":"SPOT",'
        '"instId":"BTC-USDT","last":"83454","lastSz":"0.0000215","askPx":"83454.5","askSz":"0.77",'
        '"bidPx":"83454.4","bidSz":"2.4","open24h":"83004.9","high24h":"84361.6","low24h":"83004.9",'
        '"volCcy24h":"358201581.935151008","vol24h":"4276.33198301","ts":"1791803759765"}]}'
    )
    feed = OkxFeed()
    (tick,) = feed.parse(frame, NOW).ticks
    assert tick.symbol == "BTC-USDT"
    assert (tick.bid, tick.ask) == (83454.4, 83454.5)
    assert tick.quote_volume_24h == pytest.approx(358201581.935151008)
    assert tick.base_volume_24h == pytest.approx(4276.33198301)
    assert feed.parse("pong", NOW).ticks == []
    assert (
        feed.parse(
            '{"event":"subscribe","arg":{"channel":"tickers","instId":"BTC-USDT"}}', NOW
        ).ticks
        == []
    )


def test_okx_subscribes_in_groups_of_fifty():
    frames = OkxFeed().subscribe_frames([f"A{i}-USDT" for i in range(120)])
    assert [len(json.loads(f)["args"]) for f in frames] == [50, 50, 20]


def test_coinbase_ticker_states_base_volume_only():
    frame = (
        '{"type":"ticker","sequence":137579916210,"product_id":"BTC-USD","price":"83366.08",'
        '"open_24h":"83074.47","volume_24h":"3253.69729420","low_24h":"82929.08","high_24h":"84261.99",'
        '"volume_30d":"175325.8","best_bid":"83366.08","best_bid_size":"0.0049","best_ask":"83366.09",'
        '"best_ask_size":"0.0269","side":"sell","time":"2026-10-12T11:16:00.266586Z","trade_id":1,'
        '"last_size":"0.00000009"}'
    )
    (tick,) = CoinbaseFeed().parse(frame, NOW).ticks
    assert tick.symbol == "BTC-USD"
    assert tick.base_volume_24h == pytest.approx(3253.6972942)
    assert tick.quote_volume_24h is None
    assert tick.event_ms == 1791803760266
    assert CoinbaseFeed().parse('{"type":"subscriptions","channels":[]}', NOW).ticks == []


def test_gate_update_frame():
    frame = (
        '{"time":1791803771,"time_ms":1791803771145,"channel":"spot.tickers","event":"update",'
        '"result":{"currency_pair":"BTC_USDT","last":"83442.1","lowest_ask":"83442.1",'
        '"highest_bid":"83442","change_percentage":"0.5267","base_volume":"5891.630193",'
        '"quote_volume":"493470346.9686727","high_24h":"84352.2","low_24h":"83003.1"}}'
    )
    feed = GateFeed()
    (tick,) = feed.parse(frame, NOW).ticks
    assert (tick.symbol, tick.bid, tick.ask) == ("BTC_USDT", 83442.0, 83442.1)
    assert tick.quote_volume_24h == pytest.approx(493470346.9686727)
    ack = '{"time":1,"channel":"spot.tickers","event":"subscribe","result":{"status":"success"}}'
    assert feed.parse(ack, NOW).ticks == []
    assert json.loads(feed.ping() or "")["channel"] == "spot.ping"


def test_bitget_snapshot_and_pong():
    frame = (
        '{"action":"snapshot","arg":{"instType":"SPOT","channel":"ticker","instId":"BTCUSDT"},'
        '"data":[{"instId":"BTCUSDT","lastPr":"83444.93","open24h":"83907.92","high24h":"84360.75",'
        '"low24h":"83019","change24h":"0.00503","bidPr":"83444.92","askPr":"83444.93","bidSz":"0.2",'
        '"askSz":"0.5","baseVolume":"1999.275339","quoteVolume":"167377148.081696",'
        '"openUtc":"83773.90","changeUtc24h":"-0.00393","ts":"1791803791641"}],"ts":1791803791645}'
    )
    feed = BitgetFeed()
    (tick,) = feed.parse(frame, NOW).ticks
    assert (tick.symbol, tick.last) == ("BTCUSDT", 83444.93)
    assert tick.quote_volume_24h == pytest.approx(167377148.081696)
    assert feed.parse("pong", NOW).ticks == []


def test_cryptocom_ticker_and_heartbeat_reply():
    frame = (
        '{"id":-1,"method":"subscribe","code":0,"result":{"instrument_name":"BTC_USDT",'
        '"subscription":"ticker.BTC_USDT","channel":"ticker","data":[{"h":"84364.09","l":"83011.76",'
        '"a":"83441.06","c":"0.0052","b":"83441.65","bs":"0.08981","k":"83441.66","ks":"0.09229",'
        '"i":"BTC_USDT","v":"802.9386","vv":"67144170.28","oi":"0","t":1791803785593}]}}'
    )
    feed = CryptoComFeed()
    (tick,) = feed.parse(frame, NOW).ticks
    assert (tick.symbol, tick.bid, tick.ask, tick.last) == (
        "BTC_USDT",
        83441.65,
        83441.66,
        83441.06,
    )
    heartbeat = feed.parse('{"id":1234,"method":"public/heartbeat","code":0}', NOW)
    assert heartbeat.ticks == []
    assert json.loads(heartbeat.replies[0]) == {"id": 1234, "method": "public/respond-heartbeat"}


def test_deribit_perpetual_ticker():
    frame = (
        '{"jsonrpc":"2.0","method":"subscription","params":{"channel":"ticker.BTC-PERPETUAL.100ms",'
        '"data":{"timestamp":1791803781443,"state":"open","stats":{"volume_usd":811070120.0},'
        '"index_price":83357.19,"instrument_name":"BTC-PERPETUAL","last_price":83395.5,'
        '"open_interest":833353150,"mark_price":83391.19,"current_funding":0.00015788,'
        '"funding_8h":0.00009628,"best_ask_price":83395.0,"best_bid_price":83394.5}}}'
    )
    (tick,) = DeribitFeed().parse(frame, NOW).ticks
    assert (tick.symbol, tick.instrument_class) == ("BTC-PERPETUAL", "inverse-perp")
    assert tick.mark == pytest.approx(83391.19)
    assert tick.funding_rate == pytest.approx(0.00009628) and tick.funding_interval_hours == 8.0
    assert tick.open_interest_usd == 833353150
    assert (
        DeribitFeed().parse('{"jsonrpc":"2.0","id":1,"result":["ticker.X.100ms"]}', NOW).ticks == []
    )


@pytest.mark.parametrize("name", sorted(FEEDS))
def test_every_feed_rejects_garbage_by_raising_not_inventing(name):
    feed = FEEDS[name]()
    # A frame that is not JSON must raise (the runner counts it), never yield ticks.
    with pytest.raises(ValueError):
        feed.parse("<html>502 bad gateway</html>", NOW)


def test_feed_registry_covers_the_configured_names():
    from vizanix_atlas.stream.config import load_stream_config

    assert set(load_stream_config().feeds) <= set(FEEDS)


def test_coercion_helpers():
    assert positive("1.5") == 1.5
    assert positive("0") is None and positive("") is None and positive(None) is None
    assert positive("nan") is None and positive("-3") is None and positive("abc") is None
    assert finite("-0.0001") == -0.0001 and finite("inf") is None and finite("") is None
