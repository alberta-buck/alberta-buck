// Runtime integration of the existing world and wheel, rather than a second
// economy fixture. Detailed route/fee/aged-inventory cases live in Foundry.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { encodeFunctionData, parseEventLogs } from "viem";
import { nodeWebevmSession } from "../src/nodefs.js";
import { buildEquilibriumWorld } from "../src/scenarios/eqworld.js";
import { artifact } from "../artifacts/bundle.mjs";
import * as identity from "../src/identity.js";

const RAW = 1_000_000n;

for (const arbFlash of [false, true]) test(`canonical JS wheel repairs a USDC withdrawal with ${arbFlash
  ? "flash-borrowed" : "funded"} Carrying-basket arb`, async () => {
  const s = await nodeWebevmSession();
  const world = await buildEquilibriumWorld(s, artifact, { identity, arbFlash,
    targetBuck: 1_000_000n * RAW,
    targetBuckLp: 1_200_000n * RAW,
    urArtifact: JSON.parse(readFileSync(new URL("../../../alberta_buck/sim/artifacts/UniversalRouter.json", import.meta.url))),
  });
  assert.equal(await s.call(world.wheel, "arbFlash"), arbFlash);
  const { buck, basket, wheel, simlp, poolUB, tokens } = world;
  assert.equal(await s.call(world.usdc, "balanceOf", [poolUB.address]), 1_200_000n * RAW);
  assert.equal(await s.call(world.reg, "isCarrying", [basket.address]), true);
  assert.equal(await s.call(buck, "approvedCarryingIssuer", [basket.address]), true);
  assert.equal(await s.call(wheel, "triangleCount"), BigInt(tokens.length));
  assert.equal(await s.call(wheel, "arbRouteCount"), 3n);

  const execute = (contract, functionName, args) => s.send(simlp, "exec", [contract.address,
    encodeFunctionData({ abi: contract.abi, functionName, args })]);
  const capital = 20_000n * RAW;
  await execute(buck, "approve", [basket.address, capital]);
  await execute(basket, "deposit", [buck.address, capital, 1n]);
  const b0 = (await s.call(poolUB, "token0")).toLowerCase() === buck.address.toLowerCase();
  await s.send(simlp, "swap", [poolUB.address, simlp.address, b0, 10_000n * RAW,
    b0 ? 4295128740n : 1461446703485210103287273052203988822378723970341n,
    await s.call(poolUB, "token0"), await s.call(poolUB, "token1")]);
  const before = (await s.call(poolUB, "slot0"))[0];
  const supply0 = await s.call(buck, "totalIssued");
  const receipts = [];
  for (let i = 0; i < 2; i++) {
    const last = await world.turnWheel({ tag: `arb:shock:${i}` });
    assert.equal(last, world.lastWheelReceipts.at(-1), "public API returns the actual final receipt");
    assert.ok(world.lastWheelReceipts.length > 1, "active routes use bounded transactions");
    receipts.push(...world.lastWheelReceipts);
  }
  const logs = receipts.flatMap((r) => r.logs);
  const arb = parseEventLogs({ abi: basket.abi, logs, eventName: "BasketArbitrage" });
  assert.ok(arb.length > 0, "the ordinary keeper must execute actual arb");
  for (const { address, args } of arb) {
    assert.equal(address.toLowerCase(), basket.address.toLowerCase());
    assert.ok(args.totalReturn > 0n);
    assert.equal(args.totalReturn, args.cashReturn + args.lpReturn, "LP fees count once");
  }
  const flow = parseEventLogs({ abi: basket.abi, logs, eventName: "ArbBuckFlow" });
  assert.ok(flow.length >= arb.length * 2, "actual BUCK transport is observable at both ends");
  assert.ok(flow.every(({ args }) => args.nominal > 0n && args.netScaled > 0n));
  const after = (await s.call(poolUB, "slot0"))[0];
  assert.ok(b0 ? after > before : after < before, "arb repairs the withdrawal price shock");
  const flashes = parseEventLogs({ abi: buck.abi, logs, eventName: "FlashIssued" });
  assert.equal(flashes.length > 0, arbFlash, "with arbFlash every cycle borrows its leg; without, none does");
  if (arbFlash) {
    assert.equal(flashes.length, arb.length, "one flash a cycle");
    assert.ok(flashes.every(({ args }) => args.borrower.toLowerCase() === basket.address.toLowerCase()));
  }
  const buckLogs = logs.filter(({ address }) => address.toLowerCase() === buck.address.toLowerCase());
  const principal = (eventName) => parseEventLogs({ abi: buck.abi, logs: buckLogs, eventName })
    .reduce((total, { args }) => total + args.principal, 0n);
  assert.equal(await s.call(buck, "totalIssued"), supply0 + principal("FacilityIssued") - principal("CashRetired"),
    "only permanent draws and retirements change the standing supply during the rotations");
  for (const receipt of receipts) {
    assert.ok(receipt.gasUsed < 12_000_000n);
    const ticks = parseEventLogs({ abi: wheel.abi, logs: receipt.logs, eventName: "Ticked" });
    assert.equal(ticks.length, 1);
    assert.ok(ticks[0].args.work <= 1n);
  }
});
