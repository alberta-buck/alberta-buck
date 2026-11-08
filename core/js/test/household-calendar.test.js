import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { MortgageBook } from "../src/agents/household-calendar.js";

const vectors = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-calendar.json", import.meta.url)));
for (const c of vectors.cases) test(`dated mortgage: ${c.name}`, () => {
  const book = new MortgageBook(BigInt(c.principal), BigInt(c.apr_ppb), c.start_day);
  for (const step of c.steps) {
    if (Object.hasOwn(step, "advance")) book.advance(step.advance);
    if (step.pay) assert.equal(book.recordPayment(step.pay.key, step.pay.day, BigInt(step.pay.amount)), step.changed ?? true);
    const before = book.state;
    const state = Object.hasOwn(step, "at") ? book.at(step.at) : book.state;
    assert.deepEqual(book.state, before);
    assert.deepEqual(state, Object.fromEntries(Object.entries(step.expected).map(([k, v]) => [k, k === "day" ? v : BigInt(v)])));
    assert.equal(state.balance, book.principal + state.interest - state.paid);
  }
});
test("a delayed receipt corrects debt from its effective day", () => {
  const [timely, late, shadow] = Array.from({ length: 3 }, () => new MortgageBook(400_000_000_000n, 60_000_000n));
  timely.advance(10); timely.recordPayment("bank", 10, 100_000_000_000n);
  for (const b of [timely, late, shadow]) b.advance(90);
  const overstated = late.state.interest;
  late.recordPayment("bank", 10, 100_000_000_000n);
  assert.deepEqual(late.state, timely.state);
  assert.ok(late.state.interest < overstated); assert.equal(overstated, shadow.state.interest);
  assert.equal(shadow.state.paid, 0n); assert.ok(shadow.state.balance > late.state.balance);
  const stepped = new MortgageBook(400_000_000_000n, 60_000_000n);
  for (let day = 1; day <= 90; day++) stepped.advance(day);
  assert.deepEqual(stepped.state, shadow.state);
});
test("conflicting history and overpayments refuse atomically", () => {
  const book = new MortgageBook(100n, 0n);
  book.advance(10); assert.equal(book.recordPayment("later", 10, 90n), true);
  const before = book.state, history = book.payments;
  for (const [key, day, amount, error] of [["later", 10, 89n, /allocation changed/],
    ["early", 0, 20n, /effective debt/], ["future", 11, 1n, /invalid/], ["zero", 10, 0n, /invalid/]]) {
    assert.throws(() => book.recordPayment(key, day, amount), error);
    assert.deepEqual(book.state, before); assert.deepEqual(book.payments, history);
  }
  assert.equal(book.recordPayment("later", 10, 90n), false);
  book.payments.set("later", { day: 0, amount: 1n });
  assert.deepEqual(book.payments, history);
  assert.throws(() => { book.state = null; }, TypeError);
});
test("invalid mortgage domains and overflow do not advance the clock", () => {
  for (const args of [[true, 0n], [-1n, 0n], [1n << 256n, 0n], [1n, -1n], [1n, 0n, .5]]) assert.throws(() => new MortgageBook(...args));
  const book = new MortgageBook((1n << 256n) - 1n, 1n), before = book.state;
  assert.throws(() => book.advance(1), /overflows/); assert.deepEqual(book.state, before);
  for (const day of [-1, .5, true, 2 ** 53]) assert.throws(() => book.advance(day));
  const arrived = new MortgageBook(100n, 0n, 5);
  arrived.advance(10);
  assert.throws(() => arrived.advance(9), /backwards/);
  assert.throws(() => arrived.at(4), /unavailable/);
});
