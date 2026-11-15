// Simulated external dollar lender, mirrored by sim/household_calendar.py.
// APR is integer ppb; interest rounds down once per bank day. Actual token
// movements and Buck/Jubilee accounting remain the existing SDK's concern.
import { parseEventLogs, zeroAddress } from "viem";
import { TransactionReceiptPending, TransactionReverted } from "../session.js";
import { firstPayday } from "./household-policy.js";
const MAX = (1n << 256n) - 1n, DENOMINATOR = 365n * 1_000_000_000n;
const DAY = 86400n, BANK = "0x000000000000000000000000000000000000dEaD";
const PHASES = ["income", "living", "insurance", "mortgage"];
const min = (...xs) => xs.reduce((a, b) => a < b ? a : b);
const money = (v) => {
  if (typeof v !== "bigint" || v < 0n || v > MAX) throw new Error("invalid external mortgage amount");
};
const dayValue = (v) => {
  if (!Number.isSafeInteger(v) || v < 0) throw new Error("invalid external mortgage day");
};

/** Fixed-rate debt and dated, caller-authenticated principal payments.
 * No transaction is sent here. Late receipt insertion replays the mark;
 * identical evidence applies once and conflicting allocations fail atomically.
 */
export class MortgageBook {
  #principal; #aprPpb; #startDay; #payments; #state;
  constructor(principal, aprPpb, startDay = 0) {
    money(principal); money(aprPpb); dayValue(startDay);
    this.#principal = principal; this.#aprPpb = aprPpb; this.#startDay = startDay;
    this.#payments = new Map();
    this.#state = Object.freeze({ day: startDay, balance: principal, interest: 0n, paid: 0n });
  }
  get principal() { return this.#principal; }
  get aprPpb() { return this.#aprPpb; }
  get startDay() { return this.#startDay; }
  get payments() { return new Map(this.#payments); }
  get state() { return this.#state; }
  grow(state, day) {
    let balance = state.balance, interest = state.interest;
    if (balance && this.aprPpb) for (let i = state.day; i < day; i++) {
      const accrued = balance * this.aprPpb / DENOMINATOR;
      if (!accrued) break;
      if (balance + accrued > MAX) throw new Error("external mortgage balance overflows uint256");
      balance += accrued; interest += accrued;
    }
    return Object.freeze({ day, balance, interest, paid: state.paid });
  }
  replay(day, payments) {
    let state = { day: this.startDay, balance: this.principal, interest: 0n, paid: 0n };
    const ordered = [...payments].sort(([ka, a], [kb, b]) => a.day - b.day || (ka < kb ? -1 : ka > kb ? 1 : 0));
    for (const [, { day: at, amount }] of ordered) {
      if (at > day) break;
      state = this.grow(state, at);
      if (amount > state.balance) throw new Error("external mortgage payment exceeds effective debt");
      state = { day: at, balance: state.balance - amount, interest: state.interest, paid: state.paid + amount };
    }
    return this.grow(state, day);
  }
  advance(day) {
    dayValue(day);
    if (day < this.state.day) throw new Error("external mortgage clock cannot go backwards");
    this.#state = this.grow(this.state, day);
    return this.state;
  }
  recordPayment(receiptKey, day, amount) {
    dayValue(day); money(amount);
    if (typeof receiptKey !== "string" || !receiptKey || !/^[\x20-\x7e]+$/.test(receiptKey)
        || amount === 0n || day < this.startDay || day > this.state.day) throw new Error("invalid external mortgage payment");
    const previous = this.#payments.get(receiptKey);
    if (previous) {
      if (previous.day !== day || previous.amount !== amount) throw new Error("external mortgage receipt allocation changed");
      return false;
    }
    const proposed = new Map(this.#payments).set(receiptKey, Object.freeze({ day, amount }));
    const state = this.replay(this.state.day, proposed);
    this.#payments = proposed; this.#state = state;
    return true;
  }
  at(day) {
    dayValue(day);
    if (day < this.startDay || day > this.state.day) throw new Error("external mortgage historical day unavailable");
    return this.replay(day, this.#payments);
  }
}

/** Scheduled actual/shadow books and receipt-backed recurring USD flows.
 * Observe every payday, independently of outgoing transaction uncertainty.
 * Only authenticated receipts commit real income/expenses; no hash means no
 * retry. Normal economic policy and saved-world import remain separate gates.
 */
export class HouseholdCalendar {
  constructor(agent) {
    this.agent = agent; this.bound = false; this.queue = []; this.flow = null; this.incomeDue = 0n;
  }
  get active() { return this.bound && this.queue.length > 0; }
  status() {
    const f = this.flow, refi = this.agent.refinanceMachine.flow;
    return { schema: 1, bound: this.bound, day: this.bound ? this.day : null,
      epoch: this.bound ? String(this.epoch) : null,
      queued: this.queue.length, income_due: String(this.incomeDue),
      mortgage_provisional: Boolean(f?.phase === "mortgage" && f.status === "pending"
        || refi?.phase === "mortgage" && refi.status === "pending" && (refi.terms?.principal ?? 1n) > 0n),
      phase: f?.phase ?? null, status: f?.status ?? "idle", hash: f?.hash ?? null };
  }
  sync() {
    const a = this.agent;
    a.mortgage = this.actual.state.balance; a.externalInterest = this.interestOffset + this.actual.state.interest;
    a.externalPaid = this.paidOffset + this.actual.state.paid;
    a.hypoMortgage = this.shadow.state.balance; a.hypoInterest = this.hypoInterestOffset + this.shadow.state.interest;
    a.hypoPaid = this.hypoPaidOffset + this.shadow.state.paid;
  }
  async bind(world, day) {
    const a = this.agent;
    dayValue(day);
    if (this.bound || day !== a.arrive_day || a.started || a.refinanceMachine.active || world.accounting !== "net") {
      throw new Error("calendar must bind an idle canonical household on arrival");
    }
    const stamp = (await world.session.client.getBlock()).timestamp;
    const epoch = world.householdEpoch ?? stamp - BigInt(day) * DAY;
    if (typeof epoch !== "bigint" || epoch < 0n || stamp < epoch || (stamp - epoch) / DAY !== BigInt(day)) {
      throw new Error("household calendar day does not match chain clock");
    }
    const rate = BigInt(Math.trunc(a.p.apr * 1e9));
    this.actual = new MortgageBook(a.mortgage, rate, day); this.shadow = new MortgageBook(a.hypoMortgage, rate, day);
    this.interestOffset = a.externalInterest; this.paidOffset = a.externalPaid;
    this.hypoInterestOffset = a.hypoInterest; this.hypoPaidOffset = a.hypoPaid;
    this.epoch = epoch;
    this.day = day; this.nextPay = firstPayday(day, a.p.pay_day);
    const opening = BigInt(Math.trunc(a.p[a.investor ? "invest_cash_k" : "cash_k"] * 1000 * 1e6));
    money(opening);
    this.queue.push({ kind: "opening", day, income: opening }); this.incomeDue += opening; a.hypoCash += opening;
    this.bound = true;
  }
  async prepare(world, day) {
    if (!this.bound) await this.bind(world, day);
    dayValue(day);
    if (day < this.day) throw new Error("household calendar cannot go backwards");
    if (day > this.nextPay) throw new Error("household calendar must observe each payday");
    // floor((stamp - epoch) / day), as Python's: BigInt division truncates toward zero, so a stamp
    // before the epoch must be refused rather than read as day 0.
    const stamp = (await world.session.client.getBlock()).timestamp;
    if (stamp < this.epoch || (stamp - this.epoch) / DAY !== BigInt(day)) {
      throw new Error("household calendar day does not match chain clock");
    }
    const a = this.agent;
    // Validate both clocks before changing any of this payday's counters.
    this.actual.grow(this.actual.state, day); this.shadow.grow(this.shadow.state, day);
    if (day === this.nextPay) {
      const income = a.incomeAnnual / 12n;
      const living = a.investor ? 0n : income * BigInt(Math.trunc(a.p.living_frac * 1e9)) / 1_000_000_000n;
      const full = a.sold ? 0n : a.face * BigInt(a.p.premium_bp) / 10_000n / 12n;
      const premium = await a.externalPremiumDue(world, full);
      for (const amount of [income, living, full, premium]) money(amount);
      this.queue.push({ kind: "month", day, income }); this.incomeDue += income;
      a.livingArrears += living; a.premiumArrears += premium;
      a.hypoCash += income; a.hypoLivingArrears += living;
      let paid = min(a.hypoCash, a.hypoLivingArrears);
      a.hypoCash -= paid; a.hypoLivingArrears -= paid; a.hypoLiving += paid;
      a.hypoPremiumArrears += full; paid = min(a.hypoCash, a.hypoPremiumArrears);
      a.hypoCash -= paid; a.hypoPremiumArrears -= paid; a.hypoPremium += paid;
      this.shadow.advance(day); paid = min(a.hypoCash, a.payment, this.shadow.state.balance);
      if (paid) { this.shadow.recordPayment(`shadow:${day}`, day, paid); a.hypoCash -= paid; }
      this.nextPay += 30;
    }
    this.actual.advance(day); this.shadow.advance(day);
    this.day = a.lastDay = day; a.nextPay = this.nextPay;
    this.sync();
  }
  /** Queue the insured asset's sale at its insured value on this prepared day
   * (household_calendar.schedule_sale): a receipt-backed dollar credit like income, then arrears
   * and the WHOLE external mortgage from actual dollars in the same period. The counterfactual
   * sells the same asset on the same day and pays its mortgage. Nothing is sent here. */
  scheduleSale(world, day) {
    const a = this.agent;
    if (!this.bound || this.day !== day || a.sold || a.investor || this.queue.some((p) => p.kind === "sale")) {
      throw new Error("asset sale needs a prepared calendar and an unsold insured asset");
    }
    money(a.face);
    this.queue.push({ kind: "sale", day, income: a.face }); this.incomeDue += a.face; a.hypoCash += a.face;
    const paid = min(a.hypoCash, this.shadow.state.balance);
    if (paid) { this.shadow.recordPayment(`shadow-sale:${day}`, day, paid); a.hypoCash -= paid; }
    this.sync();
  }
  /** Queue payment of arrears and the whole remaining mortgage from dollars now held. */
  scheduleSettlement(world, day) {
    const a = this.agent;
    if (!this.bound || this.day !== day || !a.sold) throw new Error("settlement needs a prepared calendar after the asset sale");
    if (this.queue.some((p) => p.kind === "sale" || p.kind === "settle")) return false;
    if (!(this.actual.state.balance || a.livingArrears || a.premiumArrears)) return false;
    this.queue.push({ kind: "settle", day, income: 0n });
    return true;
  }
  async recordMortgage(world, receipt, amount) {
    const stamp = (await world.session.client.getBlock({ blockNumber: receipt.blockNumber })).timestamp;
    const day = Number((stamp - this.epoch) / DAY);
    const changed = this.actual.recordPayment(receipt.transactionHash, day, amount);
    this.sync();
    return { changed, day };
  }
  async emit(world, day, status, detail = {}) {
    const f = this.flow;
    await this.agent.refinanceMachine.event(world, day, status, { kind: "calendar",
      phase: `${f.period.kind}-${f.phase}`, due_day: f.period.day, ...detail });
  }
  incomeFromReceipt(world, receipt, amount, expectedHash) {
    if (receipt?.status !== "success" || receipt.transactionHash?.toLowerCase() !== expectedHash?.toLowerCase()) {
      throw new Error("household income receipt is not successful or has a wrong hash");
    }
    const events = parseEventLogs({ abi: world.usdc.abi, logs: receipt.logs, eventName: "Transfer" })
      .filter((e) => e.address.toLowerCase() === world.usdc.address.toLowerCase());
    const e = events[0]?.args;
    if (events.length !== 1 || e.from.toLowerCase() !== zeroAddress
        || e.to.toLowerCase() !== world.holderAddress(this.agent.account).toLowerCase() || e.value !== amount) {
      throw new Error("household income mint evidence does not match terms");
    }
    return receipt;
  }
  async recover(world) {
    const f = this.flow, owner = world.holderAddress(this.agent.account);
    const receipt = await world.session.client.getTransactionReceipt({ hash: f.hash });
    if (receipt?.transactionHash?.toLowerCase() !== f.hash.toLowerCase()) throw new Error("household calendar receipt hash mismatch");
    if (receipt.status === "reverted") throw new TransactionReverted(f.phase, "household calendar operation reverted", receipt);
    if (f.phase === "income") return this.incomeFromReceipt(world, receipt, f.amount, f.hash);
    return world.netSettlementOps.paymentFromReceipt(owner, BANK, f.amount, receipt, f.hash).receipt;
  }
  nextPhase(f, phase) {
    return f.period.kind === "opening" || phase === "mortgage" ? "complete"
      : PHASES[PHASES.indexOf(phase) + 1];
  }
  async apply(world, day, receipt) {
    const a = this.agent, f = this.flow, phase = f.phase, amount = f.amount;
    let effective = null;
    if (phase === "mortgage") effective = (await this.recordMortgage(world, receipt, amount)).day;
    else if (phase === "income") {
      this.incomeDue -= amount;
      if (f.period.kind === "opening") a.started = true;
      else if (f.period.kind === "sale") a.sold = true; // proceeds, not wages: the asset and its premium are gone
      else a.incomeTotal += amount;
    } else if (phase === "living") { a.livingArrears -= amount; a.livingPaid += amount; }
    else { a.premiumArrears -= amount; a.extPremium += amount; }
    f.phase = this.nextPhase(f, phase); f.hash = null; f.status = "running";
    await this.emit(world, day, "confirmed", { operation: phase, amounts: { usdc: amount }, effective_day: effective,
      transaction_hash: receipt.transactionHash, receipt_block: receipt.blockNumber ?? null });
  }
  async applyKnown(world, day, receipt) {
    const f = this.flow, phase = f.phase;
    f.hash = receipt.transactionHash;
    try { await this.apply(world, day, receipt); return true; }
    catch (err) {
      if (f.phase !== phase) throw err;
      f.status = "pending"; this.agent.lastAction = "calendar-pending";
      this.agent.lastError = `calendar accounting unavailable: ${err}`.slice(0, 200);
      await this.emit(world, day, "pending", { reason: this.agent.lastError, transaction_hash: f.hash }); return false;
    }
  }
  async mint(world, amount) {
    return world.session.send(world.usdc, "mint", [world.holderAddress(this.agent.account), amount],
      { expect: "either", tag: "household:calendar-income" });
  }
  async advance(world, day) {
    await this.prepare(world, day);
    const a = this.agent, s = world.session;
    if (a.refinanceMachine.active || await a.pendingRepayment(world)) return this.active;
    while (this.queue.length) {
      if (!this.flow) {
        a.refinanceMachine.decisions++;
        this.flow = { period: this.queue[0], phase: this.queue[0].kind === "settle" ? "living" : "income",
          status: "running", hash: null, amount: 0n };
        await this.emit(world, day, "planned", { calendar: this.status() });
      }
      const f = this.flow;
      if (f.status === "pending") {
        if (!f.hash) { a.lastError = "calendar hash unavailable; reconcile before continuing"; return true; }
        let receipt;
        try { receipt = await this.recover(world); }
        catch (err) {
          if (err instanceof TransactionReverted) {
            f.status = "refused"; await this.emit(world, day, "refused", { transaction_hash: f.hash });
          } else a.lastError = `calendar receipt unavailable: ${err}`.slice(0, 200);
          return true;
        }
        if (!await this.applyKnown(world, day, receipt)) return true;
      }
      while (f.phase !== "complete") {
        const phase = f.phase, cash = await s.call(world.usdc, "balanceOf", [world.holderAddress(a.account)]);
        const closing = ["sale", "settle"].includes(f.period.kind); // an exit pays the whole balance
        const amount = phase === "income" ? f.period.income
          : min(cash, phase === "living" ? a.livingArrears : phase === "insurance" ? a.premiumArrears
            : closing ? this.actual.state.balance : min(a.payment, this.actual.state.balance));
        f.amount = amount;
        if (!amount) {
          if (phase === "income" && f.period.kind === "opening") a.started = true;
          f.phase = this.nextPhase(f, phase); await this.emit(world, day, "skipped", { operation: phase }); continue;
        }
        f.status = "pending"; f.hash = null;
        let receipt = null;
        try {
          if (phase === "income") {
            receipt = await this.mint(world, amount);
            if (receipt?.status === "reverted" && receipt.transactionHash) throw new TransactionReverted("income", "household income mint reverted", receipt);
            if (receipt?.status !== "success" || !receipt.transactionHash) throw new TransactionReceiptPending(receipt?.transactionHash ?? null);
            this.incomeFromReceipt(world, receipt, amount, receipt.transactionHash);
          } else receipt = (await world.netSettlementOps.payExact(world.holderAddress(a.account), BANK, amount)).receipt;
        } catch (err) {
          f.hash = err.transactionHash ?? receipt?.transactionHash ?? null;
          const known = err instanceof TransactionReverted && err.receipt?.status === "reverted" && f.hash;
          if (known) f.status = "refused";
          a.lastAction = "calendar-pending"; a.lastError = String(err).slice(0, 200);
          await this.emit(world, day, known ? "refused" : "pending", { reason: a.lastError, transaction_hash: f.hash }); return true;
        }
        if (!await this.applyKnown(world, day, receipt)) return true;
      }
      await this.emit(world, day, "complete"); this.queue.shift(); this.flow = null;
    }
    a.lastAction = "calendar"; a.lastError = ""; return false;
  }
}
