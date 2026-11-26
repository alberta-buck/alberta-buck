import assert from "node:assert/strict";
import { test } from "node:test";
import { encodeAbiParameters, encodeEventTopics, parseAbi } from "viem";

import { savingsPriceTape } from "../src/scenarios/savings-prices.js";
import { rebalanceFills } from "../src/scenarios/savings-rebalances.js";
import { SavingsWorld } from "../src/scenarios/savings-world.js";

test("price regimes preserve the historical default and reproduce seeded noise", () => {
  const values = Array(367).fill(1_000_000);
  const prices = { tokens: [{ sym: "LABR" }, { sym: "FOOD" }], series: {
    history: [values, values], revert: [values, values], "three-phase": [values, values],
  } };
  assert.deepEqual(savingsPriceTape(prices, "history", {}, 1), prices.series.history);
  const settings = { "scenario.sine_period.LABR": "8", "scenario.sine_period.FOOD": "24", "scenario.usd_inflation_pct": "4" };
  const tape = savingsPriceTape(prices, "sine", settings, 1);
  assert.ok(tape[0][2] > tape[1][2], "each commodity follows its own period");
  assert.equal(tape[0][24], tape[1][24], "a common cycle boundary has the same inflation drift");
  assert.ok(tape[0][24] > 1_000_000);
  for (const mode of ["history", "revert", "three-phase", "sine"]) {
    const noisy = { ...settings, "scenario.price_noise_pct": "1.5" };
    const first = savingsPriceTape(prices, mode, noisy, 1);
    assert.deepEqual(first, savingsPriceTape(prices, mode, noisy, 1));
    assert.notDeepEqual(first, savingsPriceTape(prices, mode, noisy, 2));
    const quiet = savingsPriceTape(prices, mode, settings, 1);
    first.forEach((feed, i) => feed.forEach((price, day) => {
      assert.ok(Math.abs(price - quiet[i][day]) <= quiet[i][day] * 0.015 + 1);
    }));
  }
  assert.throws(() => savingsPriceTape(prices, "sine", { "scenario.sine_period.LABR": 0 }, 1), /LABR period/);
});

test("live price changes preserve the previous interpolation endpoint and replace only future returns", () => {
  const history = [1_000_000, 1_100_000, 1_200_000, 1_300_000, 1_400_000];
  const revert = [1_000_000, 1_000_000, 2_000_000, 3_000_000, 4_000_000];
  const w = new SavingsWorld({ prices: { start: "2020-01-01", tokens: [{ sym: "FOOD" }],
    series: { history: [history], revert: [revert], "three-phase": [revert.slice(0, 4)] } } });
  w.world = { feeds: [history.map(BigInt)] };
  w.day = 2;
  w.changePriceRegime({ "scenario.prices": "revert" });
  assert.deepEqual(w.priceTape, [[1_000_000, 1_100_000, 1_200_000, 1_800_000, 2_400_000]]);
  assert.deepEqual(w.world.feeds[0], w.priceTape[0].map(BigInt));
  assert.equal(w.priceChangeDay, 3);
  assert.equal(w.startDate, "2020-01-01");
  const revised = w.priceTape;
  assert.throws(() => w.changePriceRegime({ "scenario.price_noise_pct": 51 }), /Price noise/);
  assert.throws(() => w.changePriceRegime({ "scenario.prices": "three-phase" }), /remaining price horizon/);
  w.day = 4;
  assert.throws(() => w.changePriceRegime({}), /no remaining prices/);
  assert.equal(w.priceTape, revised);
});

test("rebalancing fills use confirmed swaps and preserve distinct response groups", () => {
  const addr = (n) => `0x${n.toString(16).padStart(40, "0")}`;
  const [basket, policy, buck, tokenA, tokenB, poolA, poolB] = [1, 2, 4, 3, 5, 6, 7].map(addr);
  const policyAbi = parseAbi(["event RebalanceGaps(int256[] gaps, uint256[] urgency)"]);
  const poolAbi = parseAbi(["event Swap(address indexed sender, address indexed recipient, int256 amount0, int256 amount1)"]);
  const upkeepAbi = parseAbi(["event Responded(uint8 indexed action, uint256 sell, uint256 buy, uint256 proceeds, uint256 buySpent)",
    "event WheelWork(uint8 indexed kind, uint256 indexed i, uint256 amount)",
    "event RebalancePlan(uint256[] sells, uint256[] buys, uint8[] sellLimits, uint8[] buyLimits)",
    "event RebalanceBuyBudget(uint256 indexed token, uint256 budget)"]);
  const logs = [];
  function emit(address, abi, eventName, args, values) {
    const item = abi.find((e) => e.name === eventName);
    logs.push({ address, topics: encodeEventTopics({ abi, eventName, args }), logIndex: logs.length,
      data: encodeAbiParameters(item.inputs.filter((field) => !field.indexed), values) });
  }
  const swap = (pool, amounts) => emit(pool, poolAbi, "Swap", { sender: basket, recipient: basket }, amounts);
  const decision = () => emit(policy, policyAbi, "RebalanceGaps", {}, [[10n, -11n], [60n, 70n]]);
  const sell = () => emit(basket, upkeepAbi, "Responded", { action: 1 }, [0n, 0n, 20n, 0n]);
  const buy = () => emit(basket, upkeepAbi, "Responded", { action: 2 }, [0n, 1n, 0n, 33n]);
  const end = () => emit(basket, upkeepAbi, "WheelWork", { kind: 2, i: 0n }, [42n]);
  emit(basket, upkeepAbi, "RebalancePlan", {}, [[20n, 0n], [0n, 44n], [2, 0], [0, 5]]);
  decision(); swap(poolA, [8n, -19n]); sell(); swap(poolB, [21n, -7n]);
  emit(basket, upkeepAbi, "RebalanceBuyBudget", { token: 1n }, [78n]);
  buy(); end();
  decision(); sell(); swap(poolB, [18n, -6n]); buy(); end();
  const world = { basket: { address: basket }, buck: { address: buck }, machine: { policy: { abi: policyAbi, address: policy } },
    net: { upkeep: { abi: upkeepAbi } }, tokens: [[tokenA, poolA], [tokenB, poolB]].map(([asset, pool]) =>
      ({ erc20: { address: asset }, poolBuck: { address: pool, abi: poolAbi } })),
    lastWheelReceipts: [{ status: "reverted", logs }, { status: "success", logs, transactionHash: "0x01" }] };
  const fills = rebalanceFills(world, 3, 1);
  assert.deepEqual(fills.map((f) => [f.side, f.token_amount, f.buck_amount, f.urgency, f.group]), [
    ["sell", "8", "19", "60", "0x01:7"], ["buy", "7", "21", "70", "0x01:7"], ["buy", "6", "18", "70", "0x01:12"],
  ]);
  assert.deepEqual(fills.map((f) => [f.planned_amount, f.limits, f.buy_budget]), [
    ["20", 2, null], ["44", 5, "78"], [null, null, null],
  ]);
});
