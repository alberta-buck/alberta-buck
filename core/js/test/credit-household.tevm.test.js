// Exercise the portable household's real credit, lender cash and savings custody on both baskets.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { tevmSession } from "../src/backends.js";
import { loadArtifact, artifactsAvailable, repoRoot } from "../src/nodefs.js";
import { CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent, HouseholdPopulationAgent }
  from "../src/agents/credit-household.js";
import { issuance } from "../src/agents/household-policy.js";
import { buildEquilibriumWorld } from "../src/scenarios/eqworld.js";
import { advanceTime, DAY } from "../src/buckworld.js";

let identity;
try { identity = (await import("../src/identity.js")).default; } catch { /* kernel unavailable */ }
const urPath = join(repoRoot(), "alberta_buck/sim/artifacts/UniversalRouter.json");
const skip = !identity || !artifactsAvailable() || !existsSync(urPath) ? "build kernels and contract artifacts first" : false;
const E6 = 1_000_000n;
const settings = { home_k: 1000, ltv: .2, apr: .055, premium_bp: 50, theta: 3, cash_k: 100,
  buffer_k: 10, switch_k: 0, risk: 0, decision_days: 1, income_frac: .1, living_frac: .5, divest_rate: 0 };

for (const basket of ["prorata", "machine"]) test(`households: live ${basket} accounting and reusable credit`, { skip }, async () => {
  const session = await tevmSession();
  const w = await buildEquilibriumWorld(session, loadArtifact, { identity, basket,
    urArtifact: JSON.parse(readFileSync(urPath, "utf8")) });
  const make = async (Class, member, overrides = {}) => {
    const a = new Class({ account: privateKeyToAccount(generatePrivateKey()), member, settings: { ...settings, ...overrides } });
    await a.setup(w); return a;
  };
  const funded = await make(CreditHouseholdAgent, "funding", {
    cash_k: 10, switch_k: 5, surplus_policy: "basket", max_impact_bp: 400,
  });
  const waits = new Set(), held = [];
  for (let day = 0; day <= 360; day += 30) {
    await funded.act(w, day, 0);
    assert.equal(funded.lastError, "");
    if (funded.joined) break;
    waits.add(funded.lastIssue);
    assert.ok(funded.fundingPending);
    assert.equal(funded.lastSurplus, "fund-issue");
    assert.equal(funded.investedUsd, 0n);
    assert.equal(funded.mortgage, funded.hypoMortgage);
    for (const tid of w._credits.get(funded.account.address)) {
      assert.equal((await session.call(w.credit, "creditInfo", [tid]))[1], 0n);
    }
    const st = await w.households.read(funded.account);
    assert.ok(st.cash >= funded.buffer);
    if (funded.lastIssue === "gate-wait") {
      held.push(st.signed);
      assert.ok(st.cash >= funded.buffer + 5000n * E6);
    }
  }
  assert.deepEqual(waits, new Set(["cash-wait", "gate-wait"]));
  assert.ok(held.length >= 2 && held[0] > 0n && held.at(-1) > held[0], "real gate BUCKs accumulate across paydays");
  assert.ok(funded.joined && funded.mortgage < funded.hypoMortgage);
  assert.equal(funded.fundingPending, false);
  assert.equal(funded.lastSurplus, "save");
  assert.ok(funded.investedUsd > 0n);
  assert.equal(funded.switchPaid, 5000n * E6);
  assert.ok((await w.households.read(funded.account)).signed < 0n);

  const changed = await make(CreditHouseholdAgent, "changed", { cash_k: 12, switch_k: 5, surplus_policy: "basket" });
  await changed.act(w, 0, 0);
  assert.ok(changed.fundingPending && changed.investedUsd === 0n);
  changed.p.theta = 0;
  await changed.act(w, 1, 0);
  assert.equal(changed.lastIssue, "declined");
  assert.equal(changed.fundingPending, false);
  assert.ok(changed.investedUsd > 0n && !changed.joined);

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
  if (basket === "machine") {
    // Cheap: the saver redeems a receipt and repays its lien with the BUCKs that come back.
    const owed = (await w.households.read(saver.account)).drawn, n = saver.receipts.length;
    const cheap = await saver.state(w, 0);
    await saver.surplus(w, { ...cheap, basket_usd: cheap.ru / cheap.rb * 1.2 });
    assert.equal(saver.receipts.length, n - 1);
    assert.ok((await w.households.read(saver.account)).drawn < owed, "the redeemed BUCKs repay the lien");
  }

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

for (const basket of ["prorata", "machine"]) test(`households: ${basket} investors and a live population`, { skip }, async () => {
  const session = await tevmSession();
  const w = await buildEquilibriumWorld(session, loadArtifact, { identity, basket,
    urArtifact: JSON.parse(readFileSync(urPath, "utf8")) });
  const make = async (member, overrides = {}) => {
    const a = new HouseholdInvestorAgent({ account: privateKeyToAccount(generatePrivateKey()), member, settings: {
      ...settings, invest_cash_k: 50, invest_income_k: 12, max_impact_bp: 2000, hold_years: 10, ...overrides } });
    await a.setup(w); return a;
  };
  const inv = await make("investor");
  assert.equal(inv.face, 0n);
  assert.equal(await session.call(w.buck, "creditLimit", [w.holderAddress(inv.account)]), 0n);
  await inv.act(w, 0, 0);
  assert.equal(inv.lastError, "");
  assert.equal(inv.lastAction, "buy");
  assert.ok(inv.investedUsd > 0n && inv.receipts.length === 0);
  const held = (await w.households.read(inv.account)).signed;
  assert.ok(held > 40_000n * E6, "the BUCKs bought are held");
  const row = inv.ledger.at(-1);
  assert.equal(row.hypo, 50_000n * E6, "the counterfactual keeps its dollars");
  assert.ok(row.nw > row.hypo * 95n / 100n && row.nw < row.hypo * 101n / 100n);

  // Dear: it buys no more and converts what it holds into the basket. The guard may refuse
  // the whole and take a half; what it refused waits, and is counted.
  const state = inv.state.bind(inv), spent = inv.investedUsd;
  inv.state = async (world, day) => ({ ...await state(world, day), basket_usd: .9 });
  await inv.act(w, 1, 0);
  inv.state = state;
  assert.equal(inv.lastAction, "convert");
  assert.equal(inv.investedUsd, spent);
  assert.ok(inv.receipts.length > 0);
  assert.ok((await w.households.read(inv.account)).signed < held);

  // The horizon: redeem, sell and leave; afterwards, nothing.
  inv.exitDay = 2;
  for (let day = 2; day < 40 && !inv.exited; day++) {
    await inv.act(w, day, 0);
    assert.equal(inv.lastError, "");
  }
  assert.ok(inv.exited && inv.receipts.length === 0 && inv.withdrawnUsd > 0n);
  assert.ok((await w.households.read(inv.account)).signed <= E6);
  const cash = await w.households.cash(inv.account);
  await inv.act(w, 90, 0);
  assert.equal(await w.households.cash(inv.account), cash);

  // A debtor sells its home: the sale pays the mortgage, the lien is bought back, coverage released.
  const seller = new CreditHouseholdAgent({ account: privateKeyToAccount(generatePrivateKey()), member: "seller",
    settings: { ...settings, max_impact_bp: 2000, surplus_policy: "basket" } });
  await seller.setup(w);
  await seller.act(w, 0, 0);
  assert.ok((await w.households.read(seller.account)).drawn > E6, "it has a lien to clear");
  seller.divestDay = 1;
  for (let day = 1; day < 60 && !seller.exited; day++) {
    await seller.act(w, day, 0);
    assert.equal(seller.lastError, "", `day ${day}`);
  }
  const sold = await w.households.read(seller.account);
  assert.ok(seller.exited && seller.mortgage === 0n && seller.hypoMortgage === 0n);
  assert.ok(sold.signed >= 0n && sold.limit <= E6, "lien cleared and coverage released");

  // A population adds its opening members and its arrivals while the world runs.
  const pop = new HouseholdPopulationAgent({ makeAccount: () => privateKeyToAccount(generatePrivateKey()),
    seed: 0xA1BC, days: 365, initial_debtors: 1, initial_savers: 1, initial_investors: 1,
    debtor_rate: 0, saver_rate: 0, investor_rate: 200, settings: { decision_days: 1 } });
  await pop.setup(w);
  for (let day = 0; day < 6; day++) await pop.act(w, day, 0);
  const roles = pop.members.map((a) => a.p.role);
  assert.deepEqual(roles.slice(0, 3), ["debtor", "saver", "investor"]);
  assert.ok(roles.length > 3 && roles.slice(3).every((r) => r === "investor"));
  assert.equal(pop.members[3].member, "0/investor/1");
  assert.equal(pop.summary.investor.arrived, roles.length - 2);
  for (const a of pop.members) assert.equal(a.lastError, "", a.member);
  assert.equal(session.mismatches.length, 0);
});
