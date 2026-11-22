"""Direct-mode tests for ConsensusFeed (gltest, no server, no network).

Run:  python -m pytest tests/test_consensus_feed.py -v
Needs the `genlayer-test` package. Web calls are mocked by gltest's
VMContext; the contract runs on the real GenLayer Python SDK.

gltest quirk (same as in the Gen402 tests): VMContext.activate() evicts
the SDK modules when the block exits, so deploy AND exercise the contract
inside ONE `with vm.activate():` block per test.
"""
import json
from pathlib import Path

import pytest
from gltest.direct import VMContext, deploy_contract, create_test_addresses

CONTRACT = Path(__file__).resolve().parent.parent / "contracts" / "consensus_feed.py"
SOURCES = [
    {"name": "coingecko", "url": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", "path": "bitcoin.usd"},
    {"name": "coinbase", "url": "https://api.coinbase.com/v2/prices/BTC-USD/spot", "path": "data.amount"},
    {"name": "kraken", "url": "https://api.kraken.com/0/public/Ticker?pair=XBTUSD", "path": "result.XXBTZUSD.c.0"},
]


@pytest.fixture()
def env():
    vm = VMContext()
    owner, stranger = create_test_addresses(2)
    return vm, owner, stranger


def _deploy(vm, owner):
    vm.sender = owner
    contract = deploy_contract(CONTRACT, vm)
    contract.add_feed("BTC/USD", 2, json.dumps(SOURCES), 2, 100, 50)
    return contract


def _quote(vm, coingecko, coinbase, kraken):
    """Replace every web mock. None = HTTP 500 for that source."""
    vm.clear_mocks()

    def put(pattern, body):
        if body is None:
            vm.mock_web(pattern, {"status": 500, "body": "oops"})
        else:
            vm.mock_web(pattern, {"status": 200, "body": body})

    put(r".*coingecko.*", None if coingecko is None else '{"bitcoin":{"usd":%s}}' % coingecko)
    put(r".*coinbase.*", None if coinbase is None else '{"data":{"amount":"%s"}}' % coinbase)
    put(r".*kraken.*", None if kraken is None else '{"error":[],"result":{"XXBTZUSD":{"c":["%s","0.0001"]}}}' % kraken)


def test_add_feed_and_config(env):
    vm, owner, _ = env
    with vm.activate():
        contract = _deploy(vm, owner)
        assert contract.get_feed_count() == 1
        assert contract.feed_id_of("btc/usd") == 1
        cfg = json.loads(contract.get_feed(1))
        assert cfg["symbol"] == "BTC/USD"
        assert len(cfg["sources"]) == 3
        assert contract.latest_round_id(1) == 0
        assert contract.is_stale(1, 3600) is True
        with pytest.raises(Exception):
            contract.latest(1)


def test_update_stores_median_and_validator_agrees(env):
    vm, owner, stranger = env
    with vm.activate():
        contract = _deploy(vm, owner)
        _quote(vm, "60651", "60611.375", "60610.20000")
        vm.sender = stranger                      # updates are permissionless
        assert contract.update(1) == 6061137      # median of the three, 2 decimals
        latest = json.loads(contract.latest(1))
        assert latest["round_id"] == 1
        assert latest["price"] == 6061137
        assert latest["sources_used"] == 3
        assert contract.latest_price(1) == 6061137
        # the captured validator re-fetches with the same mocks and agrees
        assert vm.run_validator() is True


def test_outlier_source_is_dropped(env):
    vm, owner, _ = env
    with vm.activate():
        contract = _deploy(vm, owner)
        _quote(vm, "60600", "60620", "91000")
        assert contract.update(1) == 6061000
        latest = json.loads(contract.latest(1))
        assert latest["sources_used"] == 2
        assert latest["sources_live"] == 3


def test_quorum_failure_reverts(env):
    vm, owner, _ = env
    with vm.activate():
        contract = _deploy(vm, owner)
        _quote(vm, None, None, "60600")
        with pytest.raises(Exception):
            contract.update(1)
        assert contract.latest_round_id(1) == 0
        # validators that also see no quorum agree with the failure
        assert vm.run_validator() is True


def test_validator_tolerance(env):
    vm, owner, _ = env
    with vm.activate():
        contract = _deploy(vm, owner)
        _quote(vm, "60000", "60000", "60000")
        contract.update(1)
        _quote(vm, "60200", "60200", "60200")     # +0.33%, inside the 0.50% band
        assert vm.run_validator() is True
        _quote(vm, "60400", "60400", "60400")     # +0.67%, outside
        assert vm.run_validator() is False
        _quote(vm, None, None, "60000")           # validator has no quorum: no vouching
        assert vm.run_validator() is False
        _quote(vm, "60000", "60000", "60000")
        assert vm.run_validator(leader_result={"ok": True, "price": 9999999, "used": 3, "live": 3}) is False


def test_history_and_staleness(env):
    vm, owner, _ = env
    with vm.activate():
        contract = _deploy(vm, owner)
        vm.warp("2026-11-23T00:00:00Z")
        _quote(vm, "60000", "60000", "60000")
        contract.update(1)
        vm.warp("2026-11-23T00:01:00Z")
        _quote(vm, "60100", "60100", "60100")
        contract.update(1)
        hist = json.loads(contract.history(1, 10))
        assert [r["round_id"] for r in hist] == [2, 1]
        assert hist[0]["updated_at"] - hist[1]["updated_at"] == 60
        assert json.loads(contract.get_round(1, 1))["price"] == 6000000
        assert contract.is_stale(1, 30) is False
        assert contract.read_fresh(1, 30) == 6010000
        vm.warp("2026-11-23T00:02:00Z")
        assert contract.is_stale(1, 30) is True
        with pytest.raises(Exception):
            contract.read_fresh(1, 30)


def test_owner_controls(env):
    vm, owner, stranger = env
    with vm.activate():
        contract = _deploy(vm, owner)
        _quote(vm, "60000", "60000", "60000")
        vm.sender = stranger
        with pytest.raises(Exception):
            contract.set_paused(1, True)
        with pytest.raises(Exception):
            contract.add_feed("ETH/USD", 2, json.dumps(SOURCES), 2, 100, 50)
        vm.sender = owner
        contract.set_paused(1, True)
        with pytest.raises(Exception):
            contract.update(1)
        contract.set_paused(1, False)
        contract.set_limits(1, 3, 200, 25)
        assert json.loads(contract.get_feed(1))["min_sources"] == 3
        with pytest.raises(Exception):
            contract.add_feed("BAD", 2, json.dumps(SOURCES[:1]), 2, 100, 50)
