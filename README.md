# ConsensusFeed — a multi-source price feed primitive for GenLayer

**One Intelligent Contract that turns several public price APIs into one
agreed on-chain number, with no LLM and no off-chain oracle operator.**

Other contracts and apps read `latest_price(feed_id)` and get an integer
that a GenLayer validator set agreed on. The owner registers feeds
(BTC/USD, ETH/USD, …), each with 2–5 independent public sources. Anyone can
call `update(feed_id)` to publish a new round.

This is a building block, not an app: no frontend, no token, no fees.

## Live deployment (studionet, chain 61999)

- **Contract:** `0xd13000D4e92FD60a47c7074c8cfd59f4d2bEA826`
- **Explorer:** https://explorer-studio.genlayer.com/address/0xd13000D4e92FD60a47c7074c8cfd59f4d2bEA826
- **Deployer / owner:** `0xfD7d408f5Cc0A4d507bDc41c48B4A00A8C9Fc399`
- **Deploy tx:** `0xe69313a4b68b40efca3afa7754e00a5b847cad09394591b8a95c6fd6374c7e27`
- **add_feed BTC/USD:** `0xcbeb206335825bf61a457c48be18145eea0b5a9e45debddc86d4eaa137caacd2`
- **add_feed ETH/USD:** `0x7e9be0f2f8f3a43c1a701d46123340ca98f65720f6ab6119f544c0a18a180805`
- **update(1), BTC/USD round 1:** `0x2d49072159733789fcfb8f21ddbcf7cc555dc0e113cc3bdf8abf87b0c2cf7246`
  → 60628.6125 (raw 6062861250000, 8 decimals), 4 of 4 sources
- **update(2), ETH/USD round 1:** `0x83ecf1f499d257a44b851aeead1c2d228b76a0c0251f1b34b38a22386e959032`
  → 1712.52 (raw 171252000000, 8 decimals), 4 of 4 sources

All FINALIZED on 2026-11-22 UTC; `is_stale(feed, 3600)` read back `false`
right after each round. Full record in `deployment.json`.

## Why this needs GenLayer consensus

A normal chain cannot call an HTTP API, and a normal oracle asks you to
trust whoever runs it. Here every validator fetches the sources itself.
The hard part is that they cannot all get the same answer: a price moves
between two fetches a second apart, and one validator may be rate-limited
by a source another one reaches. Strict equality would fail almost every
round. So the contract defines exactly what "the same answer" means.

### What the leader does

Inside the non-deterministic block the leader runs `observe()`:

1. Fetch every source with `gl.nondet.web.get`. A source counts as **down**
   if the request throws, the status is not 200, the body is not JSON, the
   JSON path is missing, or the value is not a plain positive decimal.
2. If fewer than `min_sources` are live, report **no quorum**.
3. Take the median of the live values. Drop every value further than
   `max_deviation_bps` from that median (outliers).
4. If fewer than `min_sources` are left, report **no quorum**. Otherwise
   propose the **median of what is left**, plus how many sources were used.

### What each validator does (the equivalence check)

A validator does not compare bytes with the leader. It runs the same
`observe()` independently, then `judge()` decides:

| Leader says | Validator found | Vote |
|---|---|---|
| a price | its own price, and the leader's is within `tolerance_bps` of the **validator's** median | agree |
| a price | its own price, further apart than `tolerance_bps` | disagree |
| a price | no quorum | disagree — it cannot vouch for a number it could not reproduce |
| no quorum | no quorum | agree — the failure itself is agreed on |
| no quorum | a price | disagree |
| anything malformed (wrong types, `used` below quorum, more sources than exist, price ≤ 0) | — | disagree |

Three choices here are deliberate:

- **The band is measured against the validator's own number**, so a leader
  cannot widen it by proposing a larger price.
- **Two separate thresholds.** `max_deviation_bps` is about sources
  disagreeing with each other at one moment (a broken or manipulated API).
  `tolerance_bps` is about the same honest sources read at two slightly
  different moments. They answer different questions and are set per feed.
- **A failed quorum is a consensus result, not an error path.** The leader
  returns `{"ok": false, …}`; validators that also fail agree; only then
  does `update()` revert. The feed never stores a number it could not get a
  quorum for, and it never silently reuses the previous one.

All arithmetic is on integers. Numbers are read from JSON as text
(`parse_float=str`) and scaled by `10**decimals` by string handling, so no
float rounding can differ between nodes. Extra digits are truncated.

After consensus the deterministic part re-checks the proposal's shape,
then stores the round with the transaction's own timestamp.

### What is and is not protected

- One dead or wrong source out of three or more: the round still succeeds.
- Two sources that disagree: both are equally far from their midpoint, so
  the round fails. Two sources detect a disagreement but cannot say who is
  right. Use three or more sources with `min_sources` ≥ 2 if a feed must
  survive one bad source.
- `sources_used` / `sources_live` in a round are the leader's counts. They
  are range-checked by validators, not matched one-to-one, because honest
  validators can legitimately see a different live set.
- All sources lying together is outside what this contract can detect.
  Pick sources that do not share an upstream.

## State layout

All storage is `TreeMap` plus `u256` and JSON strings.

| Field | Type | Meaning |
|---|---|---|
| `owner` | `Address` | may add and configure feeds |
| `feed_count` | `u256` | feed ids are `1..feed_count` |
| `feeds[id]` | `str` (JSON) | `id, symbol, decimals, sources[], min_sources, max_deviation_bps, tolerance_bps, paused` |
| `symbol_index[symbol]` | `u256` | symbol → feed id |
| `round_counts[id]` | `u256` | latest round id of a feed, 0 = never updated |
| `rounds[id*32 + slot]` | `str` (JSON) | one round; `slot = (round_id-1) % 32` |

`rounds` is a ring: each feed keeps its last **32** rounds and overwrites
the oldest, so storage per feed is bounded no matter how often it updates.

A round is:

```json
{"feed_id": 1, "round_id": 7, "price": 6061078750000, "decimals": 8,
 "updated_at": 1795376202, "sources_used": 4, "sources_live": 4, "sources_total": 4}
```

`price / 10**decimals` is the human number (here 60610.7875).
`updated_at` is unix seconds, UTC, taken from the transaction time.
`decimals` is fixed when a feed is created so history never changes scale.

## Contract API (`contracts/consensus_feed.py`)

Constructor: `ConsensusFeed()` — no arguments; the deployer becomes owner.

| Method | Type | Who | What it does |
|---|---|---|---|
| `add_feed(symbol, decimals, sources_json, min_sources, max_deviation_bps, tolerance_bps) -> u256` | write | owner | Register a feed, returns its id. |
| `set_sources(feed_id, sources_json)` | write | owner | Replace the sources. History and decimals stay. |
| `set_limits(feed_id, min_sources, max_deviation_bps, tolerance_bps)` | write | owner | Change quorum, outlier band, validator tolerance. |
| `set_paused(feed_id, paused)` | write | owner | Stop or resume updates. Reads keep working. |
| `transfer_ownership(new_owner)` | write | owner | Hand the feed set to another address. |
| `update(feed_id) -> u256` | write | anyone | Fetch, reach consensus, store a round. Reverts on no quorum. |
| `latest_price(feed_id) -> u256` | view | anyone | Latest scaled price. Reverts if the feed has no round. |
| `read_fresh(feed_id, max_age_seconds) -> u256` | view | anyone | Latest price, reverts if older than `max_age_seconds`. |
| `is_stale(feed_id, max_age_seconds) -> bool` | view | anyone | True if never updated or older than `max_age_seconds`. |
| `latest(feed_id) -> str` | view | anyone | Latest round as JSON. |
| `latest_round_id(feed_id) -> u256` | view | anyone | 0 means never updated. |
| `get_round(feed_id, round_id) -> str` | view | anyone | One of the last 32 rounds as JSON. |
| `history(feed_id, limit) -> str` | view | anyone | Up to 32 most recent rounds, newest first. |
| `get_feed(feed_id) -> str` / `list_feeds() -> str` | view | anyone | Feed config(s) as JSON. |
| `feed_id_of(symbol) -> u256` | view | anyone | 0 if the symbol is not registered. |
| `get_feed_count() -> u256` / `get_owner() -> str` | view | anyone | |

`sources_json` is a JSON array of 2–5 objects:

```json
[{"name": "coinbase", "url": "https://api.coinbase.com/v2/prices/BTC-USD/spot", "path": "data.amount"}]
```

`path` is a dotted path into the response; a numeric segment indexes a
list (`result.XXBTZUSD.c.0`). Limits: `decimals` 0–18, `min_sources` from
2 to the number of sources, `max_deviation_bps` 1–5000, `tolerance_bps`
1–1000, URLs https only.

## Shipped feeds (`data/feeds.json`)

Every URL below was fetched with `curl` on 2026-11-23 and returned the
field its path reads. None needs an API key.

| Feed | Source | URL | Path |
|---|---|---|---|
| BTC/USD | CoinGecko | `https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd` | `bitcoin.usd` |
| BTC/USD | Coinbase | `https://api.coinbase.com/v2/prices/BTC-USD/spot` | `data.amount` |
| BTC/USD | Kraken | `https://api.kraken.com/0/public/Ticker?pair=XBTUSD` | `result.XXBTZUSD.c.0` |
| BTC/USD | Bitstamp | `https://www.bitstamp.net/api/v2/ticker/btcusd/` | `last` |
| ETH/USD | CoinGecko | `https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd` | `ethereum.usd` |
| ETH/USD | Coinbase | `https://api.coinbase.com/v2/prices/ETH-USD/spot` | `data.amount` |
| ETH/USD | Kraken | `https://api.kraken.com/0/public/Ticker?pair=ETHUSD` | `result.XETHZUSD.c.0` |
| ETH/USD | Bitstamp | `https://www.bitstamp.net/api/v2/ticker/ethusd/` | `last` |

Both feeds use 8 decimals, quorum 3 of 4, outlier band 100 bps (1%),
validator tolerance 50 bps (0.5%). Four sources with a quorum of three
means one source can be down or rate-limited and the round still lands.
Binance's public ticker was tried and left out: it answers HTTP 451 from
restricted regions, so validators in different places would disagree on
whether it is alive.

## Using it from another contract

```python
from genlayer import *

class Vault(gl.Contract):
    feed: Address

    def __init__(self, feed: Address):
        self.feed = feed

    @gl.public.view
    def btc_value(self, sats: u256) -> u256:
        oracle = gl.get_contract_at(self.feed)
        # reverts if BTC/USD (feed 1) is older than 10 minutes
        price = oracle.view().read_fresh(u256(1), u256(600))   # 8 decimals
        return u256(int(sats) * int(price) // 10**8)
```

`read_fresh` is the call to prefer: it gives a price or reverts, so a
consumer cannot act on old data by forgetting a check. Use `latest_price`
when any age is acceptable, and `latest` when you want the round metadata.

## Using it from an app (genlayer-js)

```js
const price = await client.readContract({ address, functionName: "latest_price", args: [1] });
const round = JSON.parse(await client.readContract({ address, functionName: "latest", args: [1] }));
console.log(Number(round.price) / 10 ** round.decimals, new Date(round.updated_at * 1000));
```

Anyone can keep a feed fresh by calling `update(feed_id)` on a timer; see
`deploy/interact.mjs`.

## Tests

Two suites, on purpose:

```bash
# 1. Logic tests: standard library only, no install, no network.
python3 tests/test_logic.py                    # 20 passed

# 2. GenVM direct-mode tests: the contract on the real GenLayer SDK.
python3 -m venv .venv && .venv/bin/pip install genlayer-test genvm-linter
.venv/bin/genvm-lint check contracts/consensus_feed.py
.venv/bin/python -m pytest tests/ -v            # 27 passed, lint: validation passed
```

`tests/test_logic.py` runs the real contract source against
`tests/_stub_sdk.py`, a ~150-line stand-in for the SDK. It covers decimal
parsing, JSON paths, the live response shapes of all shipped sources,
median, outlier rejection, both quorum failures, the validator tolerance
band on its exact edges, malformed leader proposals, the 32-round ring,
two feeds side by side, staleness and owner controls.

`tests/test_consensus_feed.py` repeats the main flows through gltest's
`VMContext` (mocked web, `vm.run_validator()` with different data than the
leader saw, `vm.warp()` for time).

## Deploy and try it

```bash
npm install genlayer-js
PRIVATE_KEY=0x<testnet key> node deploy/deploy.mjs            # deploys + registers data/feeds.json
CONTRACT=0x... PRIVATE_KEY=0x<testnet key> node deploy/interact.mjs   # update feed 1, read it back
CONTRACT=0x... READ_ONLY=1 node deploy/interact.mjs           # read only, no key
```

Keys are read from the environment only and never written to disk.
`deploy.mjs` writes `deployment.json` with addresses and tx hashes.

## Files

```
contracts/consensus_feed.py    the contract
data/feeds.json                BTC/USD and ETH/USD feed definitions
deploy/deploy.mjs              deploy + register feeds
deploy/interact.mjs            publish a round and read it back
tests/test_logic.py            logic tests, standard library only
tests/_stub_sdk.py             SDK stand-in used by test_logic.py
tests/test_consensus_feed.py   gltest direct-mode tests
```

## Notes and limits

- Time comes from `datetime.now()`, which GenVM serves from the
  transaction's datetime, so `updated_at` is identical on every node.
- `update()` is permissionless and has no rate limit; every call costs the
  caller a transaction and adds one round to the ring.
- The owner can change sources and limits at any time. Consumers that need
  that fixed should check `get_feed` or use a feed whose owner is a
  contract with its own rules.
