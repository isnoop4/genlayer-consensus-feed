# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""ConsensusFeed — a reusable multi-source price feed for GenLayer.

One contract hosts many feeds (BTC/USD, ETH/USD, ...). Each feed has 2-5
independent public HTTP sources, each with a dotted JSON path to the price.
Anyone may call update(feed_id); the result is a new round holding the
median of the sources that are live and agree with each other.

No LLM is involved anywhere. What GenLayer contributes is the consensus:

  leader     fetches every source, drops the ones that fail or sit further
             than max_deviation_bps from the median, requires min_sources
             survivors, and proposes the median of the survivors.
  validator  does the same fetch-and-aggregate independently, then accepts
             the leader's price only if it is within tolerance_bps of its
             own median. Strict equality would never pass (prices move
             between two fetches), so agreement is a band, not a value.

A failed quorum is itself agreed on: the leader reports ok=False, every
validator that also fails agrees, and update() then reverts with no new
round. A validator that cannot reach quorum never vouches for a price.

Prices are integers scaled by the feed's `decimals` (decimals=8 means
6065100000000 is 60651.00000000). No float touches a stored value.

State (all storage is TreeMap + JSON strings / u256):
  owner                 Address   may add and configure feeds
  feed_count            u256      feed ids are 1..feed_count
  feeds[id]             str       JSON feed config (symbol, sources, limits)
  symbol_index[symbol]  u256      symbol -> feed id
  round_counts[id]      u256      latest round id of a feed (0 = none yet)
  rounds[id*32 + slot]  str       JSON round; slot = (round_id-1) % 32, a
                                  ring that keeps the last 32 rounds per feed
"""
import datetime
import json

from genlayer import *


HISTORY_SIZE = 32          # rounds kept per feed (ring buffer)
BPS = 10000                # 100% in basis points
MIN_FEED_SOURCES = 2
MAX_FEED_SOURCES = 5
MAX_DECIMALS = 18
MAX_DEVIATION_LIMIT_BPS = 5000
MAX_TOLERANCE_BPS = 1000


# ----------------------------------------------------------------------
# Pure helpers. No storage, no network: each is unit-tested on its own.
# ----------------------------------------------------------------------
def parse_scaled(raw, decimals: int):
    """Turn a decimal string such as "60611.375" into an integer scaled by
    10**decimals, or None if it is not a plain positive decimal.

    Digits beyond `decimals` are truncated (never rounded up). Exponents,
    signs other than a leading "+", and anything non-numeric are rejected:
    a source that answers in an odd format is treated as down, not guessed.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("+"):
        text = text[1:]
    if len(text) == 0 or len(text) > 40:
        return None
    whole, dot, frac = text.partition(".")
    if not whole.isdigit() or not whole.isascii():
        return None
    if dot == "." and len(frac) > 0 and not (frac.isdigit() and frac.isascii()):
        return None
    frac = frac[:decimals].ljust(decimals, "0")
    value = int(whole + frac)
    if value <= 0:
        return None
    return value


def read_path(doc, path: str):
    """Walk a dotted path ("result.XXBTZUSD.c.0") through parsed JSON.
    Numeric segments index lists. Returns None when the path does not exist.
    """
    node = doc
    for segment in path.split("."):
        if isinstance(node, dict):
            if segment not in node:
                return None
            node = node[segment]
        elif isinstance(node, list):
            if not segment.isdigit() or int(segment) >= len(node):
                return None
            node = node[int(segment)]
        else:
            return None
    return node


def extract_price(body_text: str, path: str, decimals: int):
    """Parse one source's response body into a scaled integer, or None."""
    try:
        # Keep numbers as their original text so no float rounding can
        # creep in between the source and the stored integer.
        doc = json.loads(body_text, parse_float=str, parse_int=str)
    except ValueError:
        return None
    return parse_scaled(read_path(doc, path), decimals)


def median_int(values) -> int:
    """Median of a non-empty list of ints. An even count takes the floor of
    the mean of the two middle values."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def within_bps(value: int, reference: int, bps: int) -> bool:
    """True when |value - reference| is at most `bps` basis points of
    `reference`. Integer arithmetic only."""
    if reference <= 0:
        return False
    return abs(value - reference) * BPS <= bps * reference


def aggregate(observations, min_sources: int, max_deviation_bps: int) -> dict:
    """Combine per-source observations (ints, or None for a failed source)
    into one price.

    1. Drop failed sources. Fewer than min_sources live -> no quorum.
    2. Take the median of the live values and drop every value further than
       max_deviation_bps from it (outliers).
    3. Fewer than min_sources left -> no quorum. Otherwise the price is the
       median of what is left.

    With exactly two live sources that disagree, both are equally far from
    their midpoint, so both are dropped and the round fails. Two sources
    can detect a disagreement but cannot say who is right; use three or
    more when a feed must survive one bad source.
    """
    live = [v for v in observations if isinstance(v, int) and not isinstance(v, bool) and v > 0]
    if len(live) < min_sources:
        return {"ok": False, "reason": "quorum: too few live sources", "live": len(live)}
    first_median = median_int(live)
    kept = [v for v in live if within_bps(v, first_median, max_deviation_bps)]
    if len(kept) < min_sources:
        return {"ok": False, "reason": "quorum: sources disagree", "live": len(live)}
    return {"ok": True, "price": median_int(kept), "used": len(kept), "live": len(live)}


def is_valid_proposal(proposal, min_sources: int, source_count: int) -> bool:
    """Shape and range check of a leader's successful proposal."""
    if not isinstance(proposal, dict) or proposal.get("ok") is not True:
        return False
    for key in ("price", "used", "live"):
        value = proposal.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            return False
    if proposal["price"] <= 0:
        return False
    if proposal["used"] < min_sources or proposal["used"] > proposal["live"]:
        return False
    return proposal["live"] <= source_count


def judge(leader, own, tolerance_bps: int, min_sources: int, source_count: int) -> bool:
    """The validator's decision: does my own independent result support the
    leader's?

    - Leader says "no quorum": agree only if I also found no quorum.
    - Leader proposes a price: I must have reached quorum myself, the
      proposal must be well-formed, and its price must be within
      tolerance_bps of MY median. The band is measured against the
      validator's own number, so a leader cannot widen it.
    """
    if not isinstance(leader, dict) or not isinstance(own, dict):
        return False
    if leader.get("ok") is False:
        return own.get("ok") is False
    if own.get("ok") is not True:
        return False
    if not is_valid_proposal(leader, min_sources, source_count):
        return False
    return within_bps(leader["price"], own["price"], tolerance_bps)


def validate_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if len(symbol) < 3 or len(symbol) > 20:
        raise ValueError("symbol must be 3-20 chars")
    for ch in symbol:
        if not (ch.isascii() and (ch.isalnum() or ch in "/-")):
            raise ValueError("symbol may only contain A-Z, 0-9, '/' and '-'")
    return symbol


def validate_sources(sources_json: str) -> list:
    """Parse and check a JSON array of {"name", "url", "path"} objects."""
    try:
        raw = json.loads(sources_json)
    except ValueError:
        raise ValueError("sources must be a JSON array")
    if not isinstance(raw, list):
        raise ValueError("sources must be a JSON array")
    if len(raw) < MIN_FEED_SOURCES or len(raw) > MAX_FEED_SOURCES:
        raise ValueError("a feed needs 2-5 sources")
    out = []
    names = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each source must be an object")
        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        path = str(item.get("path", "")).strip()
        if len(name) < 1 or len(name) > 32:
            raise ValueError("source name must be 1-32 chars")
        if name in names:
            raise ValueError("source names must be unique")
        if not url.startswith("https://") or len(url) > 300 or " " in url:
            raise ValueError("source url must be an https URL of at most 300 chars")
        if len(path) < 1 or len(path) > 120:
            raise ValueError("source path must be 1-120 chars")
        for segment in path.split("."):
            if len(segment) == 0:
                raise ValueError("source path has an empty segment")
            for ch in segment:
                if not (ch.isascii() and (ch.isalnum() or ch in "_-")):
                    raise ValueError("source path segments may only contain letters, digits, '_' and '-'")
        names.append(name)
        out.append({"name": name, "url": url, "path": path})
    return out


def validate_limits(source_count: int, min_sources: int, max_deviation_bps: int, tolerance_bps: int) -> None:
    if min_sources < MIN_FEED_SOURCES or min_sources > source_count:
        raise ValueError("min_sources must be between 2 and the number of sources")
    if max_deviation_bps < 1 or max_deviation_bps > MAX_DEVIATION_LIMIT_BPS:
        raise ValueError("max_deviation_bps must be 1-5000")
    if tolerance_bps < 1 or tolerance_bps > MAX_TOLERANCE_BPS:
        raise ValueError("tolerance_bps must be 1-1000")


def now_ts() -> int:
    """Transaction time as unix seconds (UTC). GenVM serves datetime.now()
    from the transaction's own datetime, so this is the same on every
    node; it is read only outside the non-deterministic block."""
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp())


# ----------------------------------------------------------------------
# Non-deterministic part: runs inside the leader/validator sub-VM only.
# ----------------------------------------------------------------------
def fetch_price(url: str, path: str, decimals: int):
    """Fetch one source. Any failure (network, status, format) is None."""
    try:
        response = gl.nondet.web.get(url)
        if response.status != 200:
            return None
        body = response.body
        if isinstance(body, bytes):
            text = body.decode("utf-8", "ignore")
        else:
            text = str(body)
        return extract_price(text, path, decimals)
    except Exception:
        return None


def observe(sources: list, decimals: int, min_sources: int, max_deviation_bps: int) -> dict:
    """Fetch every source of a feed and aggregate them."""
    observations = [fetch_price(s["url"], s["path"], decimals) for s in sources]
    return aggregate(observations, min_sources, max_deviation_bps)


class ConsensusFeed(gl.Contract):
    owner: Address
    feed_count: u256
    feeds: TreeMap[u256, str]
    symbol_index: TreeMap[str, u256]
    round_counts: TreeMap[u256, u256]
    rounds: TreeMap[u256, str]

    def __init__(self):
        self.owner = gl.message.sender_address
        self.feed_count = u256(0)

    # ------------------------------------------------------------------
    # Internal helpers (deterministic)
    # ------------------------------------------------------------------
    def _addr_key(self, addr) -> str:
        # The same address can arrive as an SDK Address, raw bytes or a
        # "0x..." string; compare on the lowercase hex of the 20 raw bytes.
        if isinstance(addr, str):
            return Address(addr).as_bytes.hex()
        raw = addr.as_bytes if hasattr(addr, "as_bytes") else bytes(addr)
        return raw.hex()

    def _only_owner(self) -> None:
        if self._addr_key(gl.message.sender_address) != self._addr_key(self.owner):
            raise gl.vm.UserError("Only owner")

    def _feed(self, feed_id: u256) -> dict:
        raw = self.feeds.get(feed_id, "")
        if raw == "":
            raise gl.vm.UserError("Feed not found")
        return json.loads(raw)

    def _round_key(self, feed_id: u256, round_id: int) -> u256:
        return u256(int(feed_id) * HISTORY_SIZE + (round_id - 1) % HISTORY_SIZE)

    def _latest_round(self, feed_id: u256) -> dict:
        self._feed(feed_id)
        count = int(self.round_counts.get(feed_id, u256(0)))
        if count == 0:
            raise gl.vm.UserError("Feed has no round yet")
        return json.loads(self.rounds[self._round_key(feed_id, count)])

    # ------------------------------------------------------------------
    # Owner: feed management
    # ------------------------------------------------------------------
    @gl.public.write
    def add_feed(self, symbol: str, decimals: u256, sources_json: str,
                 min_sources: u256, max_deviation_bps: u256, tolerance_bps: u256) -> u256:
        """Register a feed. `sources_json` is a JSON array of
        {"name", "url", "path"}. Returns the new feed id."""
        self._only_owner()
        try:
            symbol = validate_symbol(symbol)
            sources = validate_sources(sources_json)
            validate_limits(len(sources), int(min_sources), int(max_deviation_bps), int(tolerance_bps))
        except ValueError as err:
            raise gl.vm.UserError(str(err))
        if int(decimals) > MAX_DECIMALS:
            raise gl.vm.UserError("decimals must be 0-18")
        if int(self.symbol_index.get(symbol, u256(0))) != 0:
            raise gl.vm.UserError("Symbol already registered")
        feed_id = self.feed_count + u256(1)
        self.feed_count = feed_id
        config = {
            "id": int(feed_id),
            "symbol": symbol,
            "decimals": int(decimals),
            "sources": sources,
            "min_sources": int(min_sources),
            "max_deviation_bps": int(max_deviation_bps),
            "tolerance_bps": int(tolerance_bps),
            "paused": False,
        }
        self.feeds[feed_id] = json.dumps(config)
        self.symbol_index[symbol] = feed_id
        self.round_counts[feed_id] = u256(0)
        return feed_id

    @gl.public.write
    def set_sources(self, feed_id: u256, sources_json: str) -> None:
        """Replace a feed's sources. Decimals and history are untouched."""
        self._only_owner()
        config = self._feed(feed_id)
        try:
            sources = validate_sources(sources_json)
            validate_limits(len(sources), config["min_sources"],
                            config["max_deviation_bps"], config["tolerance_bps"])
        except ValueError as err:
            raise gl.vm.UserError(str(err))
        config["sources"] = sources
        self.feeds[feed_id] = json.dumps(config)

    @gl.public.write
    def set_limits(self, feed_id: u256, min_sources: u256,
                   max_deviation_bps: u256, tolerance_bps: u256) -> None:
        """Change a feed's quorum, outlier band and validator tolerance."""
        self._only_owner()
        config = self._feed(feed_id)
        try:
            validate_limits(len(config["sources"]), int(min_sources),
                            int(max_deviation_bps), int(tolerance_bps))
        except ValueError as err:
            raise gl.vm.UserError(str(err))
        config["min_sources"] = int(min_sources)
        config["max_deviation_bps"] = int(max_deviation_bps)
        config["tolerance_bps"] = int(tolerance_bps)
        self.feeds[feed_id] = json.dumps(config)

    @gl.public.write
    def set_paused(self, feed_id: u256, paused: bool) -> None:
        """Pause or resume updates. Reads keep working while paused."""
        self._only_owner()
        config = self._feed(feed_id)
        config["paused"] = bool(paused)
        self.feeds[feed_id] = json.dumps(config)

    @gl.public.write
    def transfer_ownership(self, new_owner: Address) -> None:
        self._only_owner()
        self.owner = Address("0x" + self._addr_key(new_owner))

    # ------------------------------------------------------------------
    # Anyone: publish a new round
    # ------------------------------------------------------------------
    @gl.public.write
    def update(self, feed_id: u256) -> u256:
        """Fetch all sources, reach consensus, store a new round.
        Returns the new price. Reverts (no new round) on a failed quorum."""
        config = self._feed(feed_id)
        if config["paused"]:
            raise gl.vm.UserError("Feed is paused")
        sources = config["sources"]
        decimals = int(config["decimals"])
        min_sources = int(config["min_sources"])
        max_deviation_bps = int(config["max_deviation_bps"])
        tolerance_bps = int(config["tolerance_bps"])
        source_count = len(sources)

        def leader() -> dict:
            return observe(sources, decimals, min_sources, max_deviation_bps)

        def validator(leader_result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                return False
            try:
                own = observe(sources, decimals, min_sources, max_deviation_bps)
            except Exception:
                return False
            return judge(leader_result.calldata, own, tolerance_bps, min_sources, source_count)

        proposal = gl.vm.run_nondet_unsafe(leader, validator)

        # From here on everything is deterministic and identical on all nodes.
        if not isinstance(proposal, dict) or proposal.get("ok") is not True:
            reason = "no quorum"
            if isinstance(proposal, dict):
                reason = str(proposal.get("reason", reason))
            raise gl.vm.UserError("Update failed, " + reason)
        if not is_valid_proposal(proposal, min_sources, source_count):
            raise gl.vm.UserError("Update failed, malformed proposal")

        round_id = int(self.round_counts.get(feed_id, u256(0))) + 1
        record = {
            "feed_id": int(feed_id),
            "round_id": round_id,
            "price": int(proposal["price"]),
            "decimals": decimals,
            "updated_at": now_ts(),
            "sources_used": int(proposal["used"]),
            "sources_live": int(proposal["live"]),
            "sources_total": source_count,
        }
        self.rounds[self._round_key(feed_id, round_id)] = json.dumps(record)
        self.round_counts[feed_id] = u256(round_id)
        return u256(record["price"])

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------
    @gl.public.view
    def latest(self, feed_id: u256) -> str:
        """Latest round as JSON: feed_id, round_id, price, decimals,
        updated_at, sources_used, sources_live, sources_total."""
        return json.dumps(self._latest_round(feed_id))

    @gl.public.view
    def latest_price(self, feed_id: u256) -> u256:
        """Latest price as a scaled integer. Reverts if there is none."""
        return u256(self._latest_round(feed_id)["price"])

    @gl.public.view
    def latest_round_id(self, feed_id: u256) -> u256:
        """Latest round id; 0 means the feed has never been updated."""
        self._feed(feed_id)
        return self.round_counts.get(feed_id, u256(0))

    @gl.public.view
    def is_stale(self, feed_id: u256, max_age_seconds: u256) -> bool:
        """True when the feed has no round, or its latest round is older
        than max_age_seconds at the time of this call."""
        self._feed(feed_id)
        if int(self.round_counts.get(feed_id, u256(0))) == 0:
            return True
        age = now_ts() - int(self._latest_round(feed_id)["updated_at"])
        return age > int(max_age_seconds)

    @gl.public.view
    def read_fresh(self, feed_id: u256, max_age_seconds: u256) -> u256:
        """Latest price, but reverts if it is older than max_age_seconds.
        The one-call guard for consumers that must not act on old data."""
        latest = self._latest_round(feed_id)
        if now_ts() - int(latest["updated_at"]) > int(max_age_seconds):
            raise gl.vm.UserError("Price is stale")
        return u256(latest["price"])

    @gl.public.view
    def get_round(self, feed_id: u256, round_id: u256) -> str:
        """One round as JSON. Only the last 32 rounds of a feed are kept."""
        self._feed(feed_id)
        count = int(self.round_counts.get(feed_id, u256(0)))
        wanted = int(round_id)
        if wanted < 1 or wanted > count:
            raise gl.vm.UserError("Round not found")
        if wanted <= count - HISTORY_SIZE:
            raise gl.vm.UserError("Round no longer kept (history holds the last 32)")
        return self.rounds[self._round_key(feed_id, wanted)]

    @gl.public.view
    def history(self, feed_id: u256, limit: u256) -> str:
        """Up to `limit` most recent rounds (max 32), newest first, as JSON."""
        self._feed(feed_id)
        count = int(self.round_counts.get(feed_id, u256(0)))
        wanted = min(int(limit), HISTORY_SIZE, count)
        out = []
        for round_id in range(count, count - wanted, -1):
            out.append(json.loads(self.rounds[self._round_key(feed_id, round_id)]))
        return json.dumps(out)

    @gl.public.view
    def get_feed(self, feed_id: u256) -> str:
        """Feed config as JSON, including its sources and limits."""
        return json.dumps(self._feed(feed_id))

    @gl.public.view
    def feed_id_of(self, symbol: str) -> u256:
        """Feed id for a symbol such as "BTC/USD"; 0 if not registered."""
        return self.symbol_index.get(symbol.strip().upper(), u256(0))

    @gl.public.view
    def list_feeds(self) -> str:
        """All feed configs as a JSON array."""
        out = []
        for feed_id in range(1, int(self.feed_count) + 1):
            out.append(json.loads(self.feeds[u256(feed_id)]))
        return json.dumps(out)

    @gl.public.view
    def get_feed_count(self) -> u256:
        return self.feed_count

    @gl.public.view
    def get_owner(self) -> str:
        return "0x" + self._addr_key(self.owner)
