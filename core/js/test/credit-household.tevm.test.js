// Exercise the portable household's real credit, lender cash and savings custody on both baskets.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { tevmSession } from "../src/backends.js";
import { loadArtifact, artifactsAvailable, repoRoot } from "../src/nodefs.js";
import { CreditHouseholdAgent, CreditSaverAgent } from "../src/agents/credit-household.js";
import { issuance } from "../src/agents/household-policy.js";
import { buildEquilibriumWorld } from "../src/scenarios/eqworld.js";
import { advanceTime, DAY } from "../src/buckworld.js";

let identity;
try { identity = (await import("../src/identity.js")).default; } catch { /* kernel unavailable */ }
const urPath = join(repoRoot(), "alberta_buck/sim/artifacts/UniversalRouter.json");
const skip = !identity || !artifactsAvailable() || !existsSync(urPath) ? "build kernels and contract artifacts first" : false;
const E6 = 1_000_000n;
const settings = { home_k: 1000, ltv: .2, apr: .055, premium_bp: 50, theta: 3, cash_k: 100,
  buffer_k: 10, switch_k: 0, risk: 0, decision_days: 1, income_frac: .1, living_frac: .5 };

for (const basket of ["prorata", "machine"]) test(`households: live ${basket} accounting and reusable credit`, { skip }, async () => {
  const session = await tevmSession();
  const w = await buildEquilibriumWorld(session, loadArtifact, { identity, basket,
    urArtifact: JSON.parse(readFileSync(urPath, "utf8")) });
  const make = async (Class, member, overrides = {}) => {
    const a = new Class({ account: privateKeyToAccount(generatePrivateKey()), member, settings: { ...settings, ...overrides } });
    await a.setup(w); return a;
  };
  const a = await make(CreditHouseholdAgent, "test");
  await a.act(w, 0, 0);
  assert.equal(a.lastError, "");
  assert.ok(a.mortgage > 0n && a.mortgage < a.hypoMortgage, "partial mortgage retirement");
  assert.equal(a.externalPaid, a.hypoMortgage - a.mortgage, "real lender payment recorded");
  assert.ok(a.premiumPaid > 0n && a.joined);
  const tids = w._credits.get(a.account.address);
  const coverage = () => Promise.all(tids.map((tid) => session.call(w.credit, "creditInfo", [tid])));
  const original = await coverage(), before = await w.households.read(a.account);
  // Policy boundaries are tested with shared fixtures. Supply explicit cheap/dear quotes here
  // to exercise both contract paths without conflating them with controller calibration.
  await a.surplus(w, { ...await a.state(w, 0), basket_usd: before.ru / before.rb * 1.2 });
  assert.ok(a.unwound > 0n);
  assert.ok((await w.households.read(a.account)).drawn < before.drawn);
  assert.deepEqual(await coverage(), original, "repayment never burns coverage");
  const small = { ...a.p, max_impact_bp: 1 };
  const headroom = await a.state(w, 0);
  const [refi, next] = issuance(headroom, small);
  assert.ok(next.go);
  await a.issue(w, headroom, refi, next);
  assert.deepEqual(await coverage(), original, "repaid headroom is reusable without activation");
  const repaid = await w.households.read(a.account);
  await a.surplus(w, { ...await a.state(w, 0), basket_usd: repaid.ru / repaid.rb * .9 });
  assert.ok(a.receipts.length > 0 && a.investedUsd > 0n, a.lastError);
  assert.equal((await w.households.read(a.account)).drawn, repaid.drawn, "savings do not repay borrower by accident");
  assert.equal(await session.call(w.buck, "creditLimit", [a.savings.address]), 0n);

  const send = session.send.bind(session);
  session.send = async (contract, fn, args, opts) => opts?.tag === "household:deposit"
    ? { status: "reverted", logs: [] } : send(contract, fn, args, opts);
  await w.fiatIn(a.account, 10000n * E6);
  await a.invest(w, 10000n * E6);
  const spent = a.investedUsd, count = a.receipts.length;
  assert.ok((await w.households.mark(a)).pending > 0n, "rejected deposit keeps its TOKEN");
  session.send = send;
  await a.invest(w, 0n);
  assert.equal(a.investedUsd, spent, "retry spends no new cash");
  assert.equal(a.receipts.length, count + 1);
  assert.equal((await w.households.mark(a)).pending, 0n);

  const saver = await make(CreditSaverAgent, "saver");
  saver.nextDecision = 1000;
  await saver.act(w, 0, 0);
  const st = await saver.state(w, 0);
  st.basket_usd = st.ru / st.rb * .9;
  const [kind, v] = issuance(st, saver.p);
  assert.ok(v.go);
  await saver.issue(w, st, kind, v);
  assert.equal(saver.lastError, "");
  assert.ok(saver.issuedInvest > 0n && saver.receipts.length > 0);
  assert.equal(saver.mortgage, 0n);

  const untouched = await make(CreditHouseholdAgent, "counterfactual", { cash_k: 0, income_frac: 0 });
  untouched.nextDecision = 1000;
  await untouched.act(w, 0, 0);
  await untouched.act(w, 60, 0);
  assert.equal(untouched.mortgage, untouched.hypoMortgage);
  assert.equal(untouched.externalInterest, untouched.hypoInterest);
  assert.equal(untouched.premiumArrears, untouched.hypoPremiumArrears);
  assert.ok(untouched.premiumArrears > 0n, "unpaid insurance remains payable");
  const last = untouched.ledger.at(-1);
  assert.equal(last.nw, last.hypo);
  assert.ok(saver.investedUsd > E6);
  await advanceTime(w, 365 * DAY);
  const aged = await w.households.mark(a);
  const raw = await session.call(w.buck, "rawBalanceOf", [w.poolAcct]);
  const fee = await session.call(w.buck, "feeOwing", [w.poolAcct]);
  assert.equal(aged.insuranceAge, fee * aged.deposit / raw);
  assert.ok(aged.insuranceAge > 0n, "insurance asset carries its share of pool demurrage");
  assert.equal(session.mismatches.length, 0);
});
