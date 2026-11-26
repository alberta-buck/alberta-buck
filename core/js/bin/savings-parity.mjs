// Run the same SavingsWorld as the browser worker against a tape resolved by Python.
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { parseEventLogs } from "viem";

import identity from "../src/identity.js";
import { loadAnyArtifact, nodeWebevmSession, repoRoot } from "../src/nodefs.js";
import { SAVINGS_NODE_OPTIONS, SavingsWorld } from "../src/scenarios/savings-world.js";

const output = process.argv[2];
if (!output) throw new Error("savings-parity requires an output path");
const { days, sets, prices, clock, opening_fields, bootstrap_deposits } = JSON.parse(readFileSync(0, "utf8"));
const urPath = join(repoRoot(), "alberta_buck/sim/artifacts/UniversalRouter.json");
const bootJournal = [];
const session = await nodeWebevmSession({ blockSeconds: clock.block_seconds,
  nodeOptions: { ...SAVINGS_NODE_OPTIONS, genesisTimestamp: BigInt(clock.genesis_timestamp) },
  journal: { record(entry) { bootJournal.push(entry); return entry; } } });
const world = new SavingsWorld({ session, identity, artifacts: loadAnyArtifact,
  urArtifact: JSON.parse(readFileSync(urPath, "utf8")), prices, sets, days,
  bootstrapDeposits: bootstrap_deposits.map(({ token, amount }) => ({ token, amount: BigInt(amount) })) });
await world.boot();
session.journal = null;

const opening = {};
for (const [name, fields] of Object.entries(opening_fields)) {
  const contract = world.world[name];
  opening[name] = {};
  for (const field of fields) opening[name][field] = String(await session.call(contract, field));
}

const bootActions = { deposits: [], wheel: [] };
const { basket, wheel, tokens } = world.world;
for (const entry of bootJournal) {
  if (!entry.tag.startsWith("eq:bootstrap-") || !["depositToken", "tick"].includes(entry.fn)) continue;
  const { blockNumber, logs } = await session.client.getTransactionReceipt({ hash: entry.tx });
  const timestamp = Number((await session.client.getBlock({ blockNumber })).timestamp);
  if (entry.fn === "depositToken") {
    for (const { args } of parseEventLogs({ abi: basket.abi, logs, eventName: "Deposited" })) {
      const token = tokens.findIndex((t) => t.erc20.address.toLowerCase() === args.token.toLowerCase());
      const [, basis] = await session.call(basket, "holdings", [args.receiptId]);
      bootActions.deposits.push({ token, amount: String(args.tokenAmount),
        basis: String(basis), credit_mark: String(args.buckMinted), timestamp });
    }
  } else {
    for (const { args } of parseEventLogs({ abi: wheel.abi, logs, eventName: "Ticked" })) {
      bootActions.wheel.push({ work: String(args.work), timestamp });
    }
  }
}

const rows = [];
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
}
if (session.mismatches.length) throw new Error("WebEVM transaction expectations failed during replay");
writeFileSync(output, JSON.stringify({ opening, boot_actions: bootActions, frames: rows }) + "\n");
