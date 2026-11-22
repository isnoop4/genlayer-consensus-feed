// Deploy ConsensusFeed to GenLayer studionet (chain 61999) and register the
// feeds listed in data/feeds.json.
// Usage:
//   PRIVATE_KEY=0x<testnet key> node deploy/deploy.mjs
// The key is read from the environment only. This script never prints it
// and never writes it anywhere. Use a testnet-only key.
// Set SKIP_FEEDS=1 to deploy without registering any feed.
// Output: deployment.json in the repo root (addresses and tx hashes only).
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const RPC_URL = process.env.RPC_URL || "https://studio.genlayer.com/api";
const PRIVATE_KEY = (process.env.PRIVATE_KEY || "").trim();
if (!/^0x[0-9a-fA-F]{64}$/.test(PRIVATE_KEY)) {
  console.error("PRIVATE_KEY env must be 0x + 64 hex (a testnet-only key).");
  process.exit(2);
}

const { createAccount, createClient } = await import("genlayer-js");
const { studionet } = await import("genlayer-js/chains");
const { TransactionStatus } = await import("genlayer-js/types");

const account = createAccount(PRIVATE_KEY);
console.log("Deployer:", account.address);
const client = createClient({ chain: studionet, endpoint: RPC_URL, account });
const out = { network: "studionet", chainId: 61999, rpc: RPC_URL, deployer: account.address, feeds: [] };

async function wait(hash, label) {
  console.log(`  waiting for ${label}: ${hash}`);
  try {
    await client.waitForTransactionReceipt({
      hash, status: TransactionStatus.FINALIZED, retries: 120, interval: 5000,
    });
    console.log(`  ${label} FINALIZED`);
    return true;
  } catch (e) {
    console.log(`  ${label} wait timed out (tx may still finalize): ${e.message}`);
    return false;
  }
}

const code = await readFile(path.join(ROOT, "contracts", "consensus_feed.py"), "utf-8");
console.log("Deploying consensus_feed.py …");
const deployTx = await client.deployContract({ code, args: [] });
console.log("deploy tx:", deployTx);
await wait(deployTx, "deploy");
// genlayer-js returns the deploy tx hash, and its receipt carries no
// contractAddress on studionet. The contract address is the tx's `to`,
// so read it from the raw receipt.
const raw = await fetch(RPC_URL, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ jsonrpc: "2.0", method: "eth_getTransactionReceipt", params: [deployTx], id: 1 }),
}).then((r) => r.json());
const contract = raw?.result?.to;
if (!/^0x[0-9a-fA-F]{40}$/.test(contract || "")) {
  throw new Error("could not resolve the contract address from the deploy receipt");
}
out.deployTx = deployTx;
out.contractAddress = contract;
out.explorer = `https://explorer-studio.genlayer.com/address/${contract}`;
console.log("Contract:", contract, out.explorer);

if (process.env.SKIP_FEEDS !== "1") {
  const feeds = JSON.parse(await readFile(path.join(ROOT, "data", "feeds.json"), "utf-8"));
  for (const f of feeds) {
    const hash = await client.writeContract({
      address: contract, functionName: "add_feed",
      args: [f.symbol, f.decimals, JSON.stringify(f.sources), f.min_sources, f.max_deviation_bps, f.tolerance_bps],
      value: 0n,
    });
    const finalized = await wait(hash, `add_feed ${f.symbol}`);
    out.feeds.push({ symbol: f.symbol, tx: hash, finalized });
  }
}

await writeFile(path.join(ROOT, "deployment.json"), JSON.stringify(out, null, 2));
console.log("Wrote deployment.json");
console.log(JSON.stringify(out, null, 2));
