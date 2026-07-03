// The walking skeleton, standalone: a real Uniswap V3 TOKEN/USDC pool on
// the in-process Tevm backend, with the PinWhale snapping spot along a
// reference path and the RoundTripTrader swapping through it -- the same
// agents that join a Python-deployed anvil sim (bin/join-sim.mjs).
// Skips when the Foundry artifacts (out/) are not built.

import { test } from "node:test";
import assert from "node:assert/strict";

import { JournalWriter, parseJournal, mismatches } from "../src/journal.js";
import { tevmSession } from "../src/backends.js";
import { artifactsAvailable, loadArtifact } from "../src/nodefs.js";
import { buildOnePool } from "../src/scenarios/onepool.js";
import { spotFromSqrtPriceX96 } from "../src/v3.js";
import { runDays } from "../src/world.js";
import { PinWhale } from "../src/agents/whale.js";
import { RoundTripTrader } from "../src/agents/trader.js";

const HAVE = artifactsAvailable();
const PATH = [2_500_000n, 3_000_000n, 2_000_000n];   // $2.50, $3.00, $2.00

test("whale pins + trader round-trips on tevm", { skip: !HAVE }, async () => {
  const lines = [];
  const session = await tevmSession({
    journal: new JournalWriter((l) => lines.push(l)),
  });
  const world = await buildOnePool(session, loadArtifact,
                                   { price: PATH[0] });

  const whale = new PinWhale({
    simlp: world.simlp, pool: world.pool, token: world.token.address,
    tokenDec: 18, quote: world.usdc.address,
    refPrice: (d) => PATH[d % PATH.length],
  });
  const trader = new RoundTripTrader({
    simlp: world.simlp, pool: world.pool, token: world.token,
    quote: world.usdc, amount: 100n * 10n ** 18n,
  });
  await session.send(world.token, "mint",
    [session.account.address, 10_000n * 10n ** 18n], { tag: "faucet" });

  await runDays(world, [whale, trader], { days: 3 });

  // The last whale snap targeted $2.00; the trader's round-trip nudges
  // spot but fees make it near-neutral -- accept 2%.
  const slot0 = await session.call(world.pool, "slot0");
  const spot = spotFromSqrtPriceX96(slot0[0], world.token.address, 18,
                                    world.usdc.address);
  const target = PATH[2];
  const dev = spot > target ? spot - target : target - spot;
  assert.ok(dev * 100n < target * 2n,
            `spot ${spot} not within 2% of target ${target}`);

  // Journal: every op matched its expectation (the SPL demo is DECLARED).
  const entries = parseJournal(lines.join(""));
  assert.equal(mismatches(entries).length, 0);
  assert.equal(session.mismatches.length, 0);
  const spl = entries.find((e) => e.tag === "trader:spl-demo:d0");
  assert.equal(spl.outcome, "revert");
  assert.match(spl.err, /SPL/);
  // Day 0 the pool sits exactly on the initialize target (identical sqrt
  // math) so the whale correctly no-ops; days 1 and 2 snap.
  assert.equal(entries.filter((e) => e.tag.startsWith("whale:snap")).length, 2);
  assert.equal(entries.filter((e) => e.tag.startsWith("trader:sell")).length, 3);
});
