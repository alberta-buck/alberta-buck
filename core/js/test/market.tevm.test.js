// Phase 4 Stage 3: the background market composed into the citizen world.
//
// The demo controller opens a real TOKEN/USDC V3 pool beside the BUCK
// stack; a PinWhale snaps it to a seeded synthetic walk and a
// RoundTripTrader arbs through it, day by simulated day, while a
// citizen's demurrage keeps ticking on the same clock.  Deterministic:
// the walk and every nonce come from seeded streams.
//
// Skips cleanly when the wasm kernels or forge artifacts are not built.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

import { tevmSession } from "../src/backends.js";
import { loadArtifact, artifactsAvailable } from "../src/nodefs.js";

const require = createRequire(import.meta.url);
let id = null;
let math = null;
try {
  id = await import("../src/identity.js");
  math = require("../wasm/buck_math.js");
} catch {
  // kernels not built
}
const skip = !id || !math
  ? "kernels not built (make nix-core-build-wasm)"
  : !artifactsAvailable()
    ? "forge artifacts not built (make nix-build)"
    : false;

test("market: whale pins the walk, trader arbs, citizens' clocks tick", { skip }, async () => {
  const { BuckWorldApp, SAMPLE_CITIZENS } = await import("../demo/src/app.js");
  const { sqrtPriceX96 } = await import("../src/v3.js");
  const { JournalWriter } = await import("../src/journal.js");

  let seed = 0x3a9e3n;
  const rng = () => {
    seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 256n) - 1n);
    const v = seed % id.ORDER;
    return v === 0n ? 1n : v;
  };

  const lines = [];
  const app = new BuckWorldApp({
    session: await tevmSession({ journal: new JournalWriter((l) => lines.push(l)) }),
    identity: id.default,
    artifacts: loadArtifact,
    makeAccount: () => privateKeyToAccount(generatePrivateKey()),
    rng,
    math,
  });
  await app.boot();

  // Citizens first: a payment in flight while the market churns.
  const a = await app.addCitizen(SAMPLE_CITIZENS[0]);
  const b = await app.addCitizen(SAMPLE_CITIZENS[1]);
  await app.credit(a, 1_000_000000n);
  await app.approvePair(a, b);
  await app.pay(a, b, 250_000000n);
  const t0 = (await app.session.client.getBlock()).timestamp;

  // Open the market and run five simulated days.  Within a day the whale
  // pins the pool to the walk and the trader's round trip then dirties it
  // a little (fees + impact) -- so track within a band, and assert the
  // EXACT pin separately below with the whale acting alone.
  await app.openMarket({ seed: 0x90071, walkDays: 32 });
  const DAYS = 5;
  for (let d = 0; d < DAYS; d++) {
    await app.marketTick();
    const { spot } = await app.marketSnapshot();
    const ref = app.walk[d];
    const errBp = ((spot > ref ? spot - ref : ref - spot) * 10_000n) / ref;
    assert.ok(errBp <= 50n, `day ${d}: spot ${spot} off ref ${ref} by ${errBp} bp`);
  }

  // The walk moved (seeded but non-trivial), and the panel agrees.
  assert.notEqual(app.walk[DAYS - 1], app.walk[0]);
  const snap = await app.marketSnapshot();
  assert.equal(snap.day, DAYS);

  // Whale alone: the pin lands EXACTLY on the reference sqrt price.
  await app.agents[0].act({ session: app.session }, DAYS, 0);
  const slot0 = await app.session.call(app.market.pool, "slot0");
  assert.equal(slot0[0], sqrtPriceX96(
    app.market.token.address, 10n ** 18n,
    app.market.usdc.address, app.walk[DAYS]));

  // The journal shows the market's story: whale snaps and the trader's
  // daily round trips (the day-0 snap is legitimately absent -- the pool
  // initializes exactly on walk[0]).
  const tags = lines.map((l) => JSON.parse(l).tag);
  for (let d = 1; d <= DAYS; d++) {
    assert.ok(tags.includes(`whale:snap:d${d === DAYS ? DAYS : d}`) ||
              tags.includes(`whale:snap:d${d}`), `whale snap day ${d}`);
  }
  for (let d = 0; d < DAYS; d++) {
    assert.ok(tags.includes(`trader:sell:d${d}`), `trader sell day ${d}`);
    assert.ok(tags.includes(`trader:buyback:d${d}`), `trader buyback day ${d}`);
  }

  // Citizens' demurrage ticked on the SAME clock (each mined market tx
  // nudges it a further second) -- and stays bit-exact at the ACTUAL
  // elapsed time.
  const t1 = (await app.session.client.getBlock()).timestamp;
  const sB = (await app.snapshot()).citizens.find((c) => c.name === b.name);
  assert.ok(t1 - t0 >= BigInt(DAYS) * BigInt(BuckWorldApp.DAY));
  assert.equal(sB.feeOwing, app.predictFee(250_000000n, t1 - t0));
  assert.ok(sB.feeOwing > 0n);

  // Whole run consistent: the only declared-vs-outcome mismatch is the
  // refused... none here -- every expectation matched (the trader's SPL
  // demo is DECLARED expect:revert and so matches).
  assert.equal(app.session.mismatches.length, 0);
});
