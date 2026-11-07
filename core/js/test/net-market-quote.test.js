import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { netPurchaseCost, netPurchaseExpected, netSaleExpected } from "../src/net-market-quote.js";

const cases = JSON.parse(readFileSync(new URL("../../../test/fixtures/net-market-quote.json", import.meta.url))).cases;
for (const c of cases) test(`net market estimate: ${c.name}`, () => {
  const q = Object.fromEntries(Object.entries(c.quote).map(([k, v]) => [k, BigInt(v)]));
  assert.equal(netSaleExpected(q, BigInt(c.amount)), BigInt(c.sale));
  assert.equal(netPurchaseExpected(q, BigInt(c.amount)), BigInt(c.purchase));
  assert.equal(netPurchaseCost(q, BigInt(c.target)), c.cost === null ? null : BigInt(c.cost));
  assert.equal(netPurchaseCost(q, 0n), 0n); assert.equal(netPurchaseExpected(q, 0n), 0n); assert.equal(netSaleExpected(q, 0n), 0n);
});
test("invalid estimates and amounts never become zero money", () => {
  const q = Object.fromEntries(Object.entries(cases[0].quote).map(([k, v]) => [k, BigInt(v)]));
  for (const bad of [{ net_scaled: q.gross_scaled + 1n }, { gross_scaled: 0n }, { fee_ppm: 1_000_000n },
    { reserve_buck: 0n }, { reserve_usdc: 1n << 256n }]) {
    assert.throws(() => netPurchaseCost({ ...q, ...bad }, 1n), /quote/);
  }
  for (const amount of [-1n, 1n << 256n, true, 1.5]) for (const fn of [netPurchaseCost, netPurchaseExpected, netSaleExpected]) {
    assert.throws(() => fn(q, amount), /amount/);
  }
});
