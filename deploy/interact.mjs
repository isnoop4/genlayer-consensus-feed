// Publish one round on a deployed ConsensusFeed and read it back.
// Usage:
//   CONTRACT=0x... PRIVATE_KEY=0x<testnet key> node deploy/interact.mjs
// Optional: FEED_ID=1 (default 1), READ_ONLY=1 (skip update, only read;
// PRIVATE_KEY is then not needed).
// CONTRACT and PRIVATE_KEY are read from the environment only. The key is
// never printed and never written anywhere by this script.
const RPC_URL = process.env.RPC_URL || "https://studio.genlayer.com/api";
const CONTRACT = (process.env.CONTRACT || "").trim();
const PRIVATE_KEY = (process.env.PRIVATE_KEY || "").trim();
const READ_ONLY = process.env.READ_ONLY === "1";
const FEED_ID = Number(process.env.FEED_ID || 1);
if (!/^0x[0-9a-fA-F]{40}$/.test(CONTRACT)) { console.error("CONTRACT env must be a 0x address"); process.exit(2); }
if (!READ_ONLY && !/^0x[0-9a-fA-F]{64}$/.test(PRIVATE_KEY)) {
  console.error("PRIVATE_KEY env must be 0x + 64 hex (a testnet-only key), or set READ_ONLY=1");
  process.exit(2);
}
if (!Number.isInteger(FEED_ID) || FEED_ID < 1) { console.error("FEED_ID must be a positive integer"); process.exit(2); }

const { createAccount, createClient } = await import("genlayer-js");
const { studionet } = await import("genlayer-js/chains");
const { TransactionStatus } = await import("genlayer-js/types");

const account = READ_ONLY ? undefined : createAccount(PRIVATE_KEY);
const client = createClient({ chain: studionet, endpoint: RPC_URL, ...(account ? { account } : {}) });
const read = (functionName, args = []) => client.readContract({ address: CONTRACT, functionName, args });

const feed = JSON.parse(await read("get_feed", [FEED_ID]));
console.log(`Feed #${FEED_ID} ${feed.symbol}: ${feed.sources.length} sources, quorum ${feed.min_sources}, ` +
  `outlier band ${feed.max_deviation_bps} bps, validator tolerance ${feed.tolerance_bps} bps`);

if (!READ_ONLY) {
  console.log("Account:", account.address);
  const hash = await client.writeContract({ address: CONTRACT, functionName: "update", args: [FEED_ID], value: 0n });
  console.log("update tx:", hash);
  try {
    await client.waitForTransactionReceipt({ hash, status: TransactionStatus.FINALIZED, retries: 120, interval: 5000 });
    console.log("update FINALIZED");
  } catch (e) {
    console.log("update wait timed out (tx may still finalize):", e.message);
  }
}

const roundId = Number(await read("latest_round_id", [FEED_ID]));
if (roundId === 0) {
  console.log("No round yet for this feed.");
} else {
  const latest = JSON.parse(await read("latest", [FEED_ID]));
  const scale = 10 ** latest.decimals;
  console.log(`Round ${latest.round_id}: ${feed.symbol} = ${latest.price / scale} ` +
    `(raw ${latest.price}, ${latest.decimals} decimals), ${latest.sources_used}/${latest.sources_total} sources, ` +
    `updated_at ${new Date(latest.updated_at * 1000).toISOString()}`);
  console.log("stale after 1h?", await read("is_stale", [FEED_ID, 3600]));
  const history = JSON.parse(await read("history", [FEED_ID, 5]));
  console.log("last rounds:", history.map((r) => `#${r.round_id}=${r.price}`).join("  "));
}
