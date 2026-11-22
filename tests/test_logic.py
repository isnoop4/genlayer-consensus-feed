"""Logic tests for ConsensusFeed. Standard library only.

Run either way:
    python3 tests/test_logic.py
    python -m pytest tests/test_logic.py -v

These run the real contract source against tests/_stub_sdk.py, a small
stand-in for the GenLayer SDK. They check the feed's own logic. GenVM
behaviour is covered by tests/test_consensus_feed.py (gltest direct mode).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _stub_sdk  # noqa: E402

SOURCES = [
    {"name": "coingecko", "url": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", "path": "bitcoin.usd"},
    {"name": "coinbase", "url": "https://api.coinbase.com/v2/prices/BTC-USD/spot", "path": "data.amount"},
    {"name": "kraken", "url": "https://api.kraken.com/0/public/Ticker?pair=XBTUSD", "path": "result.XXBTZUSD.c.0"},
]
OWNER = _stub_sdk.Address(b"\x01" * 20)
STRANGER = _stub_sdk.Address(b"\x02" * 20)


def fresh():
    world = _stub_sdk.World()
    mod = _stub_sdk.load(world)
    world.sender = OWNER
    return world, mod, mod.ConsensusFeed()


def add_btc(feed, min_sources=2, max_deviation_bps=100, tolerance_bps=50, decimals=2):
    return feed.add_feed("btc/usd", decimals, json.dumps(SOURCES),
                         min_sources, max_deviation_bps, tolerance_bps)


def quote(world, coingecko=None, coinbase=None, kraken=None):
    """Set what each source answers. None = HTTP 500."""
    def put(needle, body):
        if body is None:
            world.mock(needle, 500, "oops")
        else:
            world.mock(needle, 200, body)
    put("coingecko", None if coingecko is None else '{"bitcoin":{"usd":%s}}' % coingecko)
    put("coinbase", None if coinbase is None else '{"data":{"amount":"%s","base":"BTC","currency":"USD"}}' % coinbase)
    put("kraken", None if kraken is None else '{"error":[],"result":{"XXBTZUSD":{"c":["%s","0.0001"]}}}' % kraken)


def raises(fn, fragment=""):
    try:
        fn()
    except Exception as err:  # the stub's UserError, or ValueError for pure helpers
        assert fragment in str(err), "expected %r in %r" % (fragment, str(err))
        return True
    raise AssertionError("expected an error containing %r" % fragment)


# ---------------------------------------------------------------- pure helpers
def test_parse_scaled():
    _, mod, _ = fresh()
    p = mod.parse_scaled
    assert p("60611.375", 2) == 6061137          # truncates, never rounds up
    assert p("60611.375", 8) == 6061137500000
    assert p("60651", 2) == 6065100
    assert p("1712.09", 2) == 171209
    assert p("0.5", 2) == 50
    assert p("+12.", 2) == 1200
    assert p(" 7 ", 0) == 7
    for bad in ("", "-5", "1e5", "abc", "1,000", "1.2.3", ".5", "0", "0.00", "NaN"):
        assert p(bad, 2) is None, bad
    for not_text in (None, 5, 5.0, True, ["5"], {"a": 1}):
        assert p(not_text, 2) is None


def test_read_path_and_extract():
    _, mod, _ = fresh()
    doc = {"result": {"XXBTZUSD": {"c": ["60610.20000", "0.0001"]}}, "n": 3}
    assert mod.read_path(doc, "result.XXBTZUSD.c.0") == "60610.20000"
    assert mod.read_path(doc, "result.XXBTZUSD.c.9") is None
    assert mod.read_path(doc, "result.missing") is None
    assert mod.read_path(doc, "n.deeper") is None
    body = '{"bitcoin":{"usd":60651.129}}'
    assert mod.extract_price(body, "bitcoin.usd", 2) == 6065112   # no float rounding
    assert mod.extract_price(body, "bitcoin.eur", 2) is None
    assert mod.extract_price("<html>rate limited</html>", "bitcoin.usd", 2) is None
    assert mod.extract_price('{"bitcoin":{"usd":null}}', "bitcoin.usd", 2) is None


def test_median_int():
    _, mod, _ = fresh()
    assert mod.median_int([5]) == 5
    assert mod.median_int([3, 1, 2]) == 2
    assert mod.median_int([4, 1, 3, 2]) == 2          # floor((2+3)/2)
    assert mod.median_int([10, 10, 30, 20]) == 15


def test_within_bps():
    _, mod, _ = fresh()
    assert mod.within_bps(10050, 10000, 50) is True       # exactly 0.50%
    assert mod.within_bps(10051, 10000, 50) is False
    assert mod.within_bps(9950, 10000, 50) is True
    assert mod.within_bps(9949, 10000, 50) is False
    assert mod.within_bps(1, 0, 50) is False              # no reference, no agreement


def test_aggregate_median_and_outliers():
    _, mod, _ = fresh()
    agg = mod.aggregate
    assert agg([100000, 100100, 100200], 2, 100) == {"ok": True, "price": 100100, "used": 3, "live": 3}
    # one wild source is dropped, the median of the rest stands
    assert agg([100000, 100100, 250000], 2, 100) == {"ok": True, "price": 100050, "used": 2, "live": 3}
    # a failed source (None) is not an outlier, it is just absent
    assert agg([100000, None, 100100], 2, 100) == {"ok": True, "price": 100050, "used": 2, "live": 2}
    # junk values never count as live
    assert agg([100000, True, -5, 0, "100000", 100100], 2, 100)["live"] == 2


def test_aggregate_quorum_failures():
    _, mod, _ = fresh()
    agg = mod.aggregate
    assert agg([100000, None, None], 2, 100) == {"ok": False, "reason": "quorum: too few live sources", "live": 1}
    assert agg([], 2, 100)["ok"] is False
    # two sources that disagree: both are off their midpoint, neither wins
    assert agg([100000, 110000, None], 2, 100) == {"ok": False, "reason": "quorum: sources disagree", "live": 2}
    # three live, but quorum of three and one is an outlier
    assert agg([100000, 100100, 250000], 3, 100)["reason"] == "quorum: sources disagree"


def test_judge_tolerance():
    _, mod, _ = fresh()
    j = mod.judge
    own = {"ok": True, "price": 100000, "used": 3, "live": 3}
    lead = lambda price, used=3, live=3: {"ok": True, "price": price, "used": used, "live": live}
    assert j(lead(100000), own, 50, 2, 3) is True
    assert j(lead(100500), own, 50, 2, 3) is True         # exactly on the band
    assert j(lead(100501), own, 50, 2, 3) is False
    assert j(lead(99499), own, 50, 2, 3) is False
    # the validator may have seen a different number of live sources
    assert j(lead(100010, used=2, live=2), own, 50, 2, 3) is True
    # malformed or impossible proposals
    assert j(lead(100000, used=1, live=3), own, 50, 2, 3) is False   # below quorum
    assert j(lead(100000, used=3, live=2), own, 50, 2, 3) is False   # used > live
    assert j(lead(100000, used=4, live=4), own, 50, 2, 3) is False   # more than exist
    assert j(lead(0), own, 50, 2, 3) is False
    assert j(lead("100000"), own, 50, 2, 3) is False
    assert j(lead(True), own, 50, 2, 3) is False
    assert j("not a dict", own, 50, 2, 3) is False
    assert j({"price": 100000, "used": 3, "live": 3}, own, 50, 2, 3) is False  # no ok flag


def test_judge_failed_quorum():
    _, mod, _ = fresh()
    j = mod.judge
    failed = {"ok": False, "reason": "quorum: too few live sources", "live": 1}
    good = {"ok": True, "price": 100000, "used": 3, "live": 3}
    assert j(failed, failed, 50, 2, 3) is True       # both saw no quorum: agreed
    assert j(failed, good, 50, 2, 3) is False        # leader gave up, I did not
    assert j(good, failed, 50, 2, 3) is False        # I cannot vouch for a price


def test_validate_sources_and_limits():
    _, mod, _ = fresh()
    ok = mod.validate_sources(json.dumps(SOURCES))
    assert [s["name"] for s in ok] == ["coingecko", "coinbase", "kraken"]
    one = json.dumps(SOURCES[:1])
    six = json.dumps([dict(SOURCES[0], name="s%d" % i) for i in range(6)])
    dup = json.dumps([SOURCES[0], SOURCES[0]])
    http = json.dumps([SOURCES[0], dict(SOURCES[1], url="http://insecure.example/x")])
    path = json.dumps([SOURCES[0], dict(SOURCES[1], path="data..amount")])
    odd = json.dumps([SOURCES[0], dict(SOURCES[1], path="data.am ount")])
    raises(lambda: mod.validate_sources("not json"), "JSON array")
    raises(lambda: mod.validate_sources('{"a":1}'), "JSON array")
    raises(lambda: mod.validate_sources(one), "2-5 sources")
    raises(lambda: mod.validate_sources(six), "2-5 sources")
    raises(lambda: mod.validate_sources(dup), "unique")
    raises(lambda: mod.validate_sources(http), "https")
    raises(lambda: mod.validate_sources(path), "empty segment")
    raises(lambda: mod.validate_sources(odd), "segments")
    mod.validate_limits(3, 2, 100, 50)
    raises(lambda: mod.validate_limits(3, 1, 100, 50), "min_sources")
    raises(lambda: mod.validate_limits(3, 4, 100, 50), "min_sources")
    raises(lambda: mod.validate_limits(3, 2, 0, 50), "max_deviation_bps")
    raises(lambda: mod.validate_limits(3, 2, 100, 1001), "tolerance_bps")
    assert mod.validate_symbol(" btc/usd ") == "BTC/USD"
    raises(lambda: mod.validate_symbol("B"), "3-20")
    raises(lambda: mod.validate_symbol("BTC USD"), "A-Z")


def test_real_response_shapes():
    """Bodies exactly as the live APIs returned them on 2026-11-23 (curl),
    read through the same paths data/feeds.json ships."""
    _, mod, _ = fresh()
    x = mod.extract_price
    assert x('{"bitcoin":{"usd":60651}}', "bitcoin.usd", 8) == 6065100000000
    assert x('{"data":{"amount":"60611.375","base":"BTC","currency":"USD"}}', "data.amount", 8) == 6061137500000
    kraken = ('{"error":[],"result":{"XXBTZUSD":{"a":["60610.30000","1","1.000"],"b":["60610.20000","1","1.000"],'
              '"c":["60610.20000","0.00015481"],"v":["2439.05515600","2900.41147750"],"o":"60089.90000"}}}')
    assert x(kraken, "result.XXBTZUSD.c.0", 8) == 6061020000000
    bitstamp = ('{"timestamp":"1795376202","open":"60078.34","high":"61565.21","low":"58640.00","last":"60606.10",'
                '"volume":"1524.39136411","vwap":"60130.06","bid":"60606.09","ask":"60606.10","side":"0"}')
    assert x(bitstamp, "last", 8) == 6060610000000
    assert x('{"ethereum":{"usd":1712.09}}', "ethereum.usd", 8) == 171209000000
    assert x('{"data":{"amount":"1711.62","base":"ETH","currency":"USD"}}', "data.amount", 8) == 171162000000
    assert x('{"error":[],"result":{"XETHZUSD":{"c":["1710.92000","0.02006082"]}}}', "result.XETHZUSD.c.0", 8) == 171092000000
    # those four BTC quotes, aggregated the way update() would
    quotes = [6065100000000, 6061137500000, 6061020000000, 6060610000000]
    assert mod.aggregate(quotes, 3, 100) == {"ok": True, "price": 6061078750000, "used": 4, "live": 4}


# ---------------------------------------------------------------- contract flow
def test_add_feed_and_views():
    world, mod, feed = fresh()
    assert add_btc(feed) == 1
    assert feed.get_feed_count() == 1
    assert feed.feed_id_of("BTC/USD") == 1
    assert feed.feed_id_of("eth/usd") == 0
    cfg = json.loads(feed.get_feed(1))
    assert cfg["symbol"] == "BTC/USD" and cfg["decimals"] == 2 and cfg["paused"] is False
    assert len(json.loads(feed.list_feeds())) == 1
    assert feed.latest_round_id(1) == 0
    assert feed.is_stale(1, 3600) is True              # never updated
    raises(lambda: feed.latest(1), "no round")
    raises(lambda: feed.latest_price(1), "no round")
    raises(lambda: feed.get_feed(9), "Feed not found")
    raises(lambda: add_btc(feed), "already registered")
    raises(lambda: feed.add_feed("ETH/USD", 19, json.dumps(SOURCES), 2, 100, 50), "decimals")
    assert feed.get_owner() == "0x" + "01" * 20


def test_update_stores_median():
    world, mod, feed = fresh()
    add_btc(feed)
    mod.now_ts = lambda: 1000
    quote(world, coingecko="60651", coinbase="60611.375", kraken="60610.20000")
    world.sender = STRANGER                              # anyone may update
    assert feed.update(1) == 6061137                     # median of 6065100, 6061137, 6061020
    latest = json.loads(feed.latest(1))
    assert latest == {"feed_id": 1, "round_id": 1, "price": 6061137, "decimals": 2,
                      "updated_at": 1000, "sources_used": 3, "sources_live": 3, "sources_total": 3}
    assert feed.latest_price(1) == 6061137
    assert feed.latest_round_id(1) == 1
    assert world.run_validator() is True                 # same data, same answer


def test_update_rejects_outlier_source():
    world, mod, feed = fresh()
    add_btc(feed)
    quote(world, coingecko="60600", coinbase="60620", kraken="91000")   # kraken is wild
    assert feed.update(1) == 6061000
    latest = json.loads(feed.latest(1))
    assert latest["sources_used"] == 2 and latest["sources_live"] == 3


def test_update_survives_one_dead_source():
    world, mod, feed = fresh()
    add_btc(feed)
    quote(world, coingecko=None, coinbase="60620", kraken="60600")
    assert feed.update(1) == 6061000
    world.fail("coinbase")                               # network error, not an HTTP status
    world.mock("coingecko", 200, '{"bitcoin":{"usd":60640}}')
    assert feed.update(1) == 6062000                     # coingecko + kraken
    assert feed.latest_round_id(1) == 2


def test_update_quorum_failure_reverts_without_round():
    world, mod, feed = fresh()
    add_btc(feed)
    quote(world, coingecko=None, coinbase=None, kraken="60600")
    raises(lambda: feed.update(1), "too few live sources")
    assert feed.latest_round_id(1) == 0
    assert world.run_validator() is True                 # validators agree it failed
    quote(world, coingecko="60600", coinbase="70000", kraken=None)
    raises(lambda: feed.update(1), "sources disagree")
    assert feed.latest_round_id(1) == 0
    # garbage bodies count as dead sources
    world.mock("coingecko", 200, "<html>Too Many Requests</html>")
    world.mock("coinbase", 200, '{"data":{"amount":"-1"}}')
    world.mock("kraken", 200, '{"error":["EGeneral:Too many requests"]}')
    raises(lambda: feed.update(1), "too few live sources")


def test_validator_tolerance_between_fetches():
    world, mod, feed = fresh()
    add_btc(feed, tolerance_bps=50)
    quote(world, coingecko="60000", coinbase="60000", kraken="60000")
    feed.update(1)                                       # leader proposes 60000.00
    quote(world, coingecko="60200", coinbase="60200", kraken="60200")
    assert world.run_validator() is True                 # +0.33% moved, inside 0.50%
    quote(world, coingecko="60400", coinbase="60400", kraken="60400")
    assert world.run_validator() is False                # +0.67% moved, outside
    quote(world, coingecko="59600", coinbase="59600", kraken="59600")
    assert world.run_validator() is False
    # a validator that cannot reach quorum itself never vouches
    quote(world, coingecko=None, coinbase=None, kraken="60000")
    assert world.run_validator() is False
    # and a validator with healthy sources rejects a leader that gave up
    quote(world, coingecko="60000", coinbase="60000", kraken="60000")
    assert world.run_validator({"ok": False, "reason": "quorum: too few live sources", "live": 0}) is False
    # a lying leader is rejected even if its shape is fine
    assert world.run_validator({"ok": True, "price": 9999999, "used": 3, "live": 3}) is False


def test_history_ring_keeps_last_32():
    world, mod, feed = fresh()
    add_btc(feed)
    for i in range(1, 41):
        mod.now_ts = lambda i=i: 1000 + i
        price = str(60000 + i)
        quote(world, coingecko=price, coinbase=price, kraken=price)
        feed.update(1)
    assert feed.latest_round_id(1) == 40
    hist = json.loads(feed.history(1, 100))
    assert len(hist) == 32
    assert [r["round_id"] for r in hist] == list(range(40, 8, -1))
    assert hist[0]["price"] == 6004000 and hist[-1]["price"] == 6000900
    assert [r["round_id"] for r in json.loads(feed.history(1, 3))] == [40, 39, 38]
    assert json.loads(feed.history(1, 0)) == []
    assert json.loads(feed.get_round(1, 9))["updated_at"] == 1009
    raises(lambda: feed.get_round(1, 8), "no longer kept")
    raises(lambda: feed.get_round(1, 41), "Round not found")
    raises(lambda: feed.get_round(1, 0), "Round not found")


def test_two_feeds_do_not_share_rounds():
    world, mod, feed = fresh()
    add_btc(feed)
    eth = [dict(s, url=s["url"].replace("bitcoin", "ethereum").replace("BTC", "ETH").replace("XBT", "ETH")) for s in SOURCES]
    eth[0]["path"] = "ethereum.usd"
    eth[2]["path"] = "result.XETHZUSD.c.0"
    assert feed.add_feed("ETH/USD", 2, json.dumps(eth), 2, 100, 50) == 2
    quote(world, coingecko="60000", coinbase="60000", kraken="60000")
    feed.update(1)
    world.web.clear()
    world.mock("ids=ethereum", 200, '{"ethereum":{"usd":1712.09}}')
    world.mock("ETH-USD", 200, '{"data":{"amount":"1711.62"}}')
    world.mock("pair=ETHUSD", 200, '{"result":{"XETHZUSD":{"c":["1710.92000","0.02"]}}}')
    assert feed.update(2) == 171162
    assert feed.latest_price(1) == 6000000
    assert feed.latest_price(2) == 171162
    assert json.loads(feed.history(2, 10))[0]["feed_id"] == 2


def test_staleness():
    world, mod, feed = fresh()
    add_btc(feed)
    quote(world, coingecko="60000", coinbase="60000", kraken="60000")
    mod.now_ts = lambda: 5000
    feed.update(1)
    mod.now_ts = lambda: 5060
    assert feed.is_stale(1, 60) is False                 # exactly max age is still fresh
    assert feed.read_fresh(1, 60) == 6000000
    mod.now_ts = lambda: 5061
    assert feed.is_stale(1, 60) is True
    raises(lambda: feed.read_fresh(1, 60), "stale")
    assert feed.latest_price(1) == 6000000               # plain read still works


def test_owner_controls():
    world, mod, feed = fresh()
    add_btc(feed)
    quote(world, coingecko="60000", coinbase="60000", kraken="60000")
    world.sender = STRANGER
    raises(lambda: add_btc(feed), "Only owner")
    raises(lambda: feed.set_paused(1, True), "Only owner")
    raises(lambda: feed.set_limits(1, 3, 100, 50), "Only owner")
    raises(lambda: feed.set_sources(1, json.dumps(SOURCES[:2])), "Only owner")
    raises(lambda: feed.transfer_ownership(STRANGER), "Only owner")
    world.sender = OWNER
    feed.set_paused(1, True)
    raises(lambda: feed.update(1), "paused")
    feed.set_paused(1, False)
    feed.update(1)
    feed.set_limits(1, 3, 200, 25)
    cfg = json.loads(feed.get_feed(1))
    assert (cfg["min_sources"], cfg["max_deviation_bps"], cfg["tolerance_bps"]) == (3, 200, 25)
    # cannot shrink the source list below the current quorum
    raises(lambda: feed.set_sources(1, json.dumps(SOURCES[:2])), "min_sources")
    feed.set_limits(1, 2, 200, 25)
    feed.set_sources(1, json.dumps(SOURCES[:2]))
    assert len(json.loads(feed.get_feed(1))["sources"]) == 2
    assert feed.latest_round_id(1) == 1                  # history untouched by config changes
    feed.transfer_ownership(STRANGER)
    assert feed.get_owner() == "0x" + "02" * 20
    raises(lambda: feed.set_paused(1, True), "Only owner")
    world.sender = STRANGER
    feed.set_paused(1, True)


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Exception as err:
            failed += 1
            print("FAIL", name, "-", type(err).__name__, err)
    print("%d passed, %d failed" % (len(tests) - failed, failed))
    sys.exit(1 if failed else 0)
