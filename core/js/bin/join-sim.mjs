#!/usr/bin/env node
// Join a running anvil node -- typically one a Python sim just deployed
// into -- and run the walking-skeleton JS agents against its REAL pools:
// the PinWhale snaps TOKEN/USDC to a reference path and the
// RoundTripTrader round-trips through it, all journaled.
//
//   node bin/join-sim.mjs --config cfg.json
//
// cfg.json: { rpc, simlp, pool, token, usdc, tokenDec, targets: [usdc-
// micro-per-token, ...], days, tradeTokens, journal }.  Prints a one-line
// JSON result (journal summary + final spot) to stdout; exits nonzero on
// any unexpected outcome.

import { readFileSync } from "node:fs";

import { anvilSession } from "../src/backends.js";
import { fileJournalWriter, loadArtifact } from "../src/nodefs.js";
import { parseJournal, summarize } from "../src/journal.js";
import { spotFromSqrtPriceX96 } from "../src/v3.js";
import { runDays } from "../src/world.js";
import { PinWhale } from "../src/agents/whale.js";
import { RoundTripTrader } from "../src/agents/trader.js";

const cfg = JSON.parse(
  readFileSync(process.argv[process.argv.indexOf("--config") + 1], "utf8"));

const journal = fileJournalWriter(cfg.journal);
const session = anvilSession(cfg.rpc, { journal });

const erc20Abi = loadArtifact("MockERC20").abi;
const simlp = session.contractAt(loadArtifact("SimLP").abi, cfg.simlp);
const pool = session.contractAt(loadArtifact("UniswapV3Pool").abi, cfg.pool);
const token = session.contractAt(erc20Abi, cfg.token);
const usdc = session.contractAt(erc20Abi, cfg.usdc);
const targets = cfg.targets.map(BigInt);
const amount = BigInt(cfg.tradeTokens) * 10n ** BigInt(cfg.tokenDec);

// The sim's tokens are MockERC20 faucets: stake the trader directly.
await session.send(token, "mint",
  [session.account.address, amount * BigInt(cfg.days) * 2n],
  { tag: "join:faucet" });

const world = { session };
const whale = new PinWhale({
  simlp, pool, token: token.address, tokenDec: cfg.tokenDec,
  quote: usdc.address, refPrice: (d) => targets[d % targets.length],
});
const trader = new RoundTripTrader({ simlp, pool, token, quote: usdc, amount });
await runDays(world, [whale, trader], { days: cfg.days });

const slot0 = await session.call(pool, "slot0");
const spot = spotFromSqrtPriceX96(slot0[0], token.address, cfg.tokenDec,
                                  usdc.address);
const sum = summarize(parseJournal(readFileSync(cfg.journal, "utf8")));
console.log(JSON.stringify({ ...sum, spot: spot.toString(),
                             sessionMismatches: session.mismatches.length }));
process.exit(sum.mismatches === 0 ? 0 : 1);
