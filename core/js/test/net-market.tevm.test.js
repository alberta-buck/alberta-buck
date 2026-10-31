// Real V3 pool and real identity approvals with the cash-only net world.
// This does not model the multi-asset basket or a five-year economy.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { devAccount, tevmSession } from "../src/backends.js";
import { buildBuckWorld, onboard, advanceTime, DAY } from "../src/buckworld.js";
import { buildMarket, openTrading, fiatIn, buyBuck, buyBuckExact } from "../src/market.js";

let identity;
try { identity = await import("../src/identity.js"); } catch { /* node kernel not built */ }
const bundle = new URL("../artifacts/sandbox.mjs", import.meta.url);
const skip = !identity ? "identity kernel not built (make nix-core-build-wasm)"
  : !existsSync(bundle) ? "sandbox artifacts not built (make nix-venv-net-demurrage-smoke)" : false;
const fields = (name) => ({ given_name: name, family_name: "Market", jurisdiction: "Alberta, Canada",
  id_type: "Alberta Identity", id_number: `NET-MARKET-${name}`, date_of_birth: "1990-01-01",
  issued_at: "2026-01-01T00:00:00Z", epoch: 42 });

test("aged carrying V3 output is reported as actual net BUCK", { skip }, async () => {
  const { artifact } = await import(bundle);
  let seed = 0x109ab42n;
  const rng = () => {
    seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 256n) - 1n);
    return seed % (identity.ORDER - 1n) + 1n;
  };
  const s = await tevmSession();
  const world = await buildBuckWorld(s, artifact, { identity, rng, accounting: "net" });
  const operator = await onboard(world, s.account, fields("Operator"), { rng });
  const market = await buildMarket(world, artifact, { operator, depth: 1_000_000_000n });
  assert.ok(market.providerCreditId !== null);
  const provider = await s.call(world.credit, "issuanceOf", [market.providerCreditId]);
  assert.ok(provider[0] > 1_000_000_000n);
  assert.equal(await s.call(world.reg, "isCarrying", [market.pool.address]), true);
  const buyer = await onboard(world, devAccount(1), fields("Buyer"), { rng });
  await fiatIn(world, market, buyer.account.address, 500_000_000n);
  await openTrading(world, market, buyer, { rng });
  await advanceTime(world, 365 * DAY + 6 * 3600);
  const poolNominal = await s.call(world.buck, "balanceOf", [market.pool.address]);
  const poolNet = await s.call(world.buck, "netBalanceOf", [market.pool.address]);
  assert.ok(poolNet < poolNominal);
  assert.equal(await s.call(world.buck, "balanceOf", [buyer.account.address]), 0n);
  const result = await buyBuck(world, market, buyer, 100_000_000n);
  assert.equal(result.paid, 100_000_000n);
  assert.ok(result.received > 0n && result.received < result.nominalReceived);
  assert.equal(await s.call(world.buck, "balanceOf", [buyer.account.address]), result.received);
  await assert.rejects(buyBuck(world, market, buyer, 1_000_000n, { minOut: 1n }), /net-value guard/);
  await assert.rejects(buyBuckExact(world, market, buyer, 1_000_000n, 2_000_000n), /net-value guard/);
  assert.deepEqual(s.mismatches, []);
});
