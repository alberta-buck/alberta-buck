// Run the same SavingsWorld as the browser worker against a tape resolved by Python.
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { parseEventLogs } from "viem";

import identity from "../src/identity.js";
import { loadAnyArtifact, nodeWebevmSession, repoRoot } from "../src/nodefs.js";
import { SAVINGS_NODE_OPTIONS, SavingsWorld } from "../src/scenarios/savings-world.js";

const output = process.argv[2];
if (!output) throw new Error("savings-parity requires an output path");
const { days, sets, prices, clock, opening_fields, bootstrap_deposits, deployment, record } = JSON.parse(readFileSync(0, "utf8"));
const urPath = join(repoRoot(), "alberta_buck/sim/artifacts/UniversalRouter.json");
const bootJournal = [];
const session = await nodeWebevmSession({ blockSeconds: clock.block_seconds,
  nodeOptions: { ...SAVINGS_NODE_OPTIONS, genesisTimestamp: BigInt(clock.genesis_timestamp) },
  journal: { record(entry) { bootJournal.push(entry); return entry; } } });
const world = new SavingsWorld({ session, identity, artifacts: loadAnyArtifact,
  urArtifact: JSON.parse(readFileSync(urPath, "utf8")), prices, sets, days,
  bootstrapDeposits: bootstrap_deposits.map(({ token, amount }) => ({ token, amount: BigInt(amount) })) });
let firstDepositMark;
await world.boot({
  firstTickTimestamp: BigInt(clock.first_tick_timestamp),
  deployment: {
    targetBuck: BigInt(deployment.targetBuck),
    targetBuckLp: BigInt(deployment.targetBuckLp),
    controllerHorizon: deployment.controllerHorizon === null ? undefined : BigInt(deployment.controllerHorizon),
    params: Object.fromEntries(Object.entries(deployment.params).map(([key, value]) => [key, BigInt(value)])),
  },
  async beforeFirstDeposit({ basket, buck, token, amount, market }) {
    const { poolBuck, dec } = market;
    const [slot, token0, window, balances, marks] = await Promise.all([
      session.call(poolBuck, "slot0"), session.call(poolBuck, "token0"),
      session.call(basket, "twapWindow"),
      session.call(buck, "accountBalancesScaled", [poolBuck.address]),
      session.call(basket, "marks", [BigInt(token), 0n]),
    ]);
    const [cumulative] = await session.call(poolBuck, "observe", [[window, 0]]);
    const elapsed = BigInt(window), delta = cumulative[1] - cumulative[0];
    const twapTick = delta / elapsed - (delta < 0n && delta % elapsed !== 0n ? 1n : 0n);
    firstDepositMark = { token, amount: String(amount), buck_is_token0: token0.toLowerCase() === buck.address.toLowerCase(),
      sqrt_price_x96: String(slot[0]), tick: slot[1], twap_tick: Number(twapTick),
      carry_gross: String(balances[0]), carry_net: String(balances[1]),
      price_low: String(marks.pLow), price_twap: String(marks.pTwap), price_high: String(marks.pHigh),
      basis_at_sample: String(amount * marks.pLow / 10n ** BigInt(dec)) };
  },
});
if ((await session.client.getBlock()).timestamp > world.world.householdEpoch) {
  throw new Error("WebEVM bootstrap ran past the replay's first tick");
}
session.journal = null;

const opening = {};
for (const [name, fields] of Object.entries(opening_fields)) {
  const contract = world.world[name];
  opening[name] = {};
  for (const field of fields) opening[name][field] = String(await session.call(contract, field));
}

const bootActions = { deposits: [], wheel: [], samples: [] };
const { basket, wheel, tokens, kctrl, machine } = world.world;
const { parity } = machine;
const source = (await session.call(wheel, "sampleSource")).toLowerCase();
bootActions.sample_source = source === parity.address.toLowerCase() ? "price book"
  : machine.desk && source === machine.sample.address.toLowerCase() ? "desk policy" : "unknown";
const eventKeys = new Set(["at", "raw", "newBuckK", "kind", "i", "job", "work", "route", "direction", "amount"]);
const bootEvent = (event) => ({ name: event.eventName,
  args: Object.fromEntries(Object.entries(event.args).filter(([key]) => eventKeys.has(key))
    .map(([key, value]) => [key, String(value)])) });
for (const entry of bootJournal) {
  if (!entry.tag.startsWith("eq:bootstrap-") || !["depositToken", "tick", "observe"].includes(entry.fn)) continue;
  const { blockNumber, logs } = await session.client.getTransactionReceipt({ hash: entry.tx });
  const timestamp = Number((await session.client.getBlock({ blockNumber })).timestamp);
  if (entry.fn === "depositToken") {
    for (const { args } of parseEventLogs({ abi: basket.abi, logs, eventName: "Deposited" })) {
      const token = tokens.findIndex((t) => t.erc20.address.toLowerCase() === args.token.toLowerCase());
      const [, basis] = await session.call(basket, "holdings", [args.receiptId]);
      bootActions.deposits.push({ token, amount: String(args.tokenAmount),
        basis: String(basis), credit_mark: String(args.buckMinted), timestamp });
    }
  } else if (entry.fn === "tick") {
    const events = [
      ...parseEventLogs({ abi: parity.abi, logs, eventName: "Observed" }),
      ...parseEventLogs({ abi: kctrl.abi, logs, eventName: "BuckKUpdated" }),
      ...parseEventLogs({ abi: basket.abi, logs, eventName: "WheelWork" }),
      ...parseEventLogs({ abi: basket.abi, logs, eventName: "BasketArbitrage" }),
      ...parseEventLogs({ abi: wheel.abi, logs, eventName: "JubileeAttempted" }),
    ].sort((a, b) => a.logIndex - b.logIndex).map(bootEvent);
    for (const { args } of parseEventLogs({ abi: wheel.abi, logs, eventName: "Ticked" })) {
      bootActions.wheel.push({ work: String(args.work), timestamp, events });
    }
  } else {
    bootActions.samples.push(...parseEventLogs({ abi: parity.abi, logs, eventName: "Observed" }).map(bootEvent));
  }
}

const rows = [], recordFrames = [], cursors = new Map();
const declared = record ? await Promise.all(tokens.map(async (_, i) =>
  Number((await session.call(basket, "constituents", [BigInt(i)]))[9]))) : null;
for (let i = 0; i < days; i++) {
  const f = await world.step();
  if (!f.sv) throw new Error(`day ${f.day}: missing basket share and principal marks`);
  const households = Object.fromEntries(world.pop.members.map((member) => {
    const ledger = member.ledger.at(-1);
    return [member.member, {
      role: member.p.role, arrival_day: member.arrive_day,
      principal: ledger ? String(ledger.drawn) : null, basis: ledger ? String(ledger.basket) : null,
    }];
  }));
  rows.push({ day: f.day, reference: f.refUsd, households,
    money: { k: f.buckK, basket_value: f.basketVal, supply: f.supply,
      share_price: f.sv.sp, basket_lien: f.sv.L } });
  if (record) {
    const agents = [], events = [];
    for (const member of world.pop.members) {
      const ledger = member.ledger.at(-1);
      if (ledger?.day === f.day) agents.push({ member: member.member, role: member.p.role,
        drawn: ledger.drawn, limit: ledger.limit, basket: ledger.basket, basket_mark: ledger.basketMark,
        cash: ledger.banked, mortgage: ledger.mortgageOwing, nw: ledger.nw, nw_buck: ledger.nwBuck,
        hypo: ledger.hypo, held_buck: ledger.held, action: ledger.action, error: member.lastError,
        exited: member.exited, active: !member.exited, relief_unavailable: ledger.reliefUnavailable });
      const cursor = cursors.get(member.member) ?? 0;
      const pending = member.householdEventsSince(cursor);
      events.push(...pending);
      cursors.set(member.member, cursor + pending.length);
    }
    events.sort((a, b) => a.day - b.day || (a.block ?? 0) - (b.block ?? 0));
    recordFrames.push({ ...f, block: Number((await session.client.getBlock()).number),
      eq_decl: declared, agents, events });
  }
}
if (session.mismatches.length) throw new Error("WebEVM transaction expectations failed during replay");
const recordMeta = record ? {
  cast: world.cast.reduce((counts, agent) => {
    const name = agent.constructor.name;
    counts[name] = (counts[name] ?? 0) + 1;
    return counts;
  }, {}),
  contracts: Object.fromEntries(Object.entries({ buck: world.world.buck, basket, wheel, kctrl,
    credit: world.world.credit, jubilee: world.world.net.fund, desk: machine.desk })
    .filter(([, contract]) => contract).map(([name, contract]) => [name, { address: contract.address }])),
} : undefined;
writeFileSync(output, JSON.stringify({ opening, boot_actions: bootActions, frames: rows,
  first_deposit_mark: firstDepositMark, record_frames: record ? recordFrames : undefined, record_meta: recordMeta },
(_key, value) => typeof value === "bigint" ? String(value) : value) + "\n");
