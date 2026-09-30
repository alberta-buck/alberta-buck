// The Savings tab's arithmetic (sandbox/src/savings/model.js): where the
// world is, what a frame says, what a receipt is worth -- and the tab's
// hand-written contract surface (savings/abi.js) against the compiled
// contracts, when this checkout has built them (out/).

import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { BASKET, DIRECTOR, ERC20, POOL, RECEIPT } from "../sandbox/src/savings/abi.js";
import { channels, chooseIndex, dateOf, deviationBp, equityWorth, hintIndex, newSid, prices, receiptWorth, row,
         serverBase, tokenFor } from "../sandbox/src/savings/model.js";

const loc = (href) => new URL(href);

test("the world's address: ?sim=, then this browser's choice, then the page's configuration", () => {
  assert.equal(serverBase({ param: "wss://sim.example/", saved: "ws://x:1", location: loc("https://a.example/") }),
    "wss://sim.example");
  assert.equal(serverBase({ param: "same-origin", location: loc("https://a.example/x/") }), "wss://a.example");
  assert.equal(serverBase({ configured: "same-origin", location: loc("http://h:8797/") }), "ws://h:8797");
  assert.equal(serverBase({ configured: "wss://pub.example", location: loc("https://h/") }), "wss://pub.example");
  assert.equal(serverBase({ saved: "ws://x:1/", configured: "same-origin", location: loc("http://h/") }), "ws://x:1");
  assert.equal(serverBase({ location: loc("http://h/") }), "ws://127.0.0.1:8797");
});

test("a world's channels carry the build-time settings, lite and replayed", () => {
  const c = channels("ws://h:1", "ab12", { "scenario.prices": "revert", "scenario.seed": "" });
  assert.equal(c.frames, "ws://h:1/s/ab12/frames?lite=1&replay=1&set=scenario.prices%3Drevert");
  assert.equal(c.control, "ws://h:1/s/ab12/control");
  assert.equal(c.rpc, "ws://h:1/s/ab12/rpc");
  assert.match(newSid(), /^[0-9a-f]{24}$/);
});

test("a frame cut down to what the tab draws", () => {
  const r = row({ day: 3, refUsd: [2_000_000], spotUsdc: [2_010_000], spotBuck: [1_900_000],
                  buck_usd: 1_050_000, basketVal: "1002000000000000000", buckK: "750000000000000000",
                  supply: 5_000_000_000_000, sv: { O: 10, S: 1, P: 9, B: 12, T: 0, D: 1.5 },
                  wh_sol_credited_usd: 42.5, ut_issued_open: 3_000_000 });
  assert.equal(r.bu, 1.05);
  assert.equal(r.bv, 1.002);
  assert.equal(r.k, 0.75);
  assert.equal(r.supply, 5_000_000);
  assert.equal(r.D, 1.5);
  assert.equal(r.credited, 42.5);
  assert.equal(r.utIssued, 3);
  const p = prices(r, 0);
  assert.equal(p.ref, 2);
  assert.equal(p.usdc, 2.01);
  assert.ok(Math.abs(p.viaBuck - 1.9 * 1.05) < 1e-12);
});

test("a receipt pays the TOKEN side of its claim (BuckBasketProRata._redeem)", () => {
  // At the start (B = O, no bonus) a receipt is paid what it put in.
  assert.deepEqual(receiptWorth(100n, { O: 1000, S: 0, B: 1000 }), { burn: 100n, claim: 200n, paid: 100n });
  // TOKENs up against BUCK (B > O): the half of the claim; the BUCK surplus is the treasury's.
  assert.equal(receiptWorth(100n, { O: 1000, S: 0, B: 1200 }).paid, 120n);
  // TOKENs down (B < O): the claim less the burn (TOKEN converted to cover it).
  assert.equal(receiptWorth(100n, { O: 1000, S: 0, B: 900 }).paid, 80n);
  // The wheel's credits (the bonus S, with its partner BUCK in B): a larger burn, a larger claim.
  const w = receiptWorth(100n, { O: 1100, S: 100, B: 1100 });
  assert.equal(w.burn, 110n);
  assert.equal(w.paid, 110n);
  assert.equal(receiptWorth(100n, { O: 1000, S: 0, B: null }), null);
});

test("an equity receipt pays its shares' value, less the treasury's cut and the exit charge", () => {
  const E18 = 10n ** 18n;
  const sv = { sp: (E18 * 11n) / 10n, lam: 2500, chg: 0n };     // share price 1.1, a quarter of the gain
  // 100 shares bought for 100: worth 110, the treasury takes 2.5 of the 10 gained.
  assert.deepEqual(equityWorth(100n * E18, 100n * E18, sv),
                   { claim: 110n * E18, cut: (25n * E18) / 10n, paid: (1075n * E18) / 10n });
  // At a loss there is no cut.
  assert.equal(equityWorth(100n * E18, 120n * E18, sv).cut, 0n);
  // The exit charge (1e18 scale) comes off what is left.
  assert.equal(equityWorth(100n * E18, 110n * E18, { ...sv, chg: E18 / 100n }).paid, (1089n * E18) / 10n);
  assert.equal(equityWorth(100n * E18, 100n * E18, { sp: null }), null);
  assert.equal(equityWorth(0n, 0n, sv), null);
});

test("dollars into TOKEN units at the pool's price; where a deposit helps most", () => {
  assert.equal(tokenFor(10_000, 2.5, 18), 4_000n * 10n ** 18n);
  assert.equal(tokenFor(100_000, 100_000, 8), 100_000_000n);
  assert.throws(() => tokenFor(1, 0, 18));
  assert.equal(hintIndex(2n, [[0.5, 0.5], [0.3, 0.3], [0.2, 0.2]]), 2);
  assert.equal(hintIndex((1n << 256n) - 1n, [[0.5, 0.4], [0.2, 0.4], [0.3, 0.2]]), 1);
  assert.equal(dateOf("2025-09-01", 30), "2025-10-01");
  // The deposit guard: 200 ticks is about 2% either way; the choice skips a pool past the guard.
  assert.ok(Math.abs(deviationBp(1200, 1000) - 202) < 1);
  assert.ok(Math.abs(deviationBp(1000, 1200) - 202) < 1);
  assert.equal(deviationBp(5, null), 0);
  const w = [[0.2, 0.4], [0.3, 0.35], [0.5, 0.25]];
  assert.equal(chooseIndex(undefined, w, [0, 0, 0], 200), 0);
  assert.equal(chooseIndex(undefined, w, [350, 0, 0], 200), 1);
  assert.equal(chooseIndex(2n, w, [350, 0, 0], 200), 2);
  assert.equal(chooseIndex(0n, w, [350, 0, 0], 200), 1);
  assert.equal(chooseIndex(undefined, w, [900, 500, 300], 200), 2);
  assert.equal(dateOf(null, 3), "");
});

// The tab's ABI against the compiled contracts: every function, event and
// error the tab uses exists with these types (a drifted signature would
// encode calls the basket rejects, or decode garbage).
const OUT = fileURLToPath(new URL("../../../out/", import.meta.url));
const ARTIFACTS = {
  ERC20: ["MockERC20.sol/MockERC20.json"],
  BASKET: ["BuckBasketOps.sol/BuckBasketOps.json", "BuckBasketProRata.sol/BuckBasketProRata.json",
           "BuckBasketUniswapV3.sol/BuckBasketUniswapV3.json",
           "BuckBasketEquity.sol/BuckBasketEquity.json"],
  POOL: ["UniswapV3Pool.sol/UniswapV3Pool.json"],
  RECEIPT: ["BuckBasketReceipt.sol/BuckBasketReceipt.json"],
  DIRECTOR: ["PairsRebalanceDirector.sol/PairsRebalanceDirector.json"],
};
const have = Object.values(ARTIFACTS).flat().every((f) => existsSync(OUT + f));
const canon = (p) => (p.type.startsWith("tuple")
  ? `(${p.components.map(canon).join(",")})${p.type.slice(5)}` : p.type);
const sig = (it) => `${it.type} ${it.name}(${(it.inputs ?? []).map(canon).join(",")})`
  + (it.type === "function" ? ` -> (${(it.outputs ?? []).map(canon).join(",")})` : "")
  + (it.type === "event" ? ` [${it.inputs.map((p) => (p.indexed ? 1 : 0)).join("")}]` : "");

test("the tab's contract surface matches the compiled contracts", { skip: !have && "out/ not built" }, () => {
  for (const [name, abi] of Object.entries({ ERC20, BASKET, POOL, RECEIPT, DIRECTOR })) {
    const known = new Set(ARTIFACTS[name].flatMap((f) => JSON.parse(readFileSync(OUT + f, "utf8")).abi.map(sig)));
    for (const it of abi) assert.ok(known.has(sig(it)), `${name}: ${sig(it)} is not in the compiled contracts`);
  }
});
