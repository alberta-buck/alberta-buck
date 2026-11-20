"""Dated external-debt accounting for the shared household calendar.

This is the simulated dollar lender, not Buck issuance or demurrage. APR
uses integer parts per billion, with daily interest rounded down in raw
settlement-token units. Payment dates are simulation days, after that day's
interest. The counterfactual uses the same convention.
"""
from dataclasses import dataclass
from alberta_buck.sim import household_policy as policy
from alberta_buck.sim.household_policy import first_payday
from buck_core import TransactionReceiptPending, TransactionReverted
from buck_core.session import Expect, _txhex
from web3.logs import DISCARD

MAX = 2**256 - 1
DAY_RATE_DENOMINATOR = 365 * 10**9
DAY_SECONDS = 86400
ZERO = "0x0000000000000000000000000000000000000000"


def _money(value):
    if type(value) is not int or not 0 <= value <= MAX:
        raise ValueError("invalid external mortgage amount")


def _day(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("invalid external mortgage day")


@dataclass(frozen=True)
class MortgageState:
    day: int
    balance: int
    interest: int
    paid: int


class MortgageBook:
    """One fixed-rate external liability and authenticated payment history.

    Pre: integer principal/APR, monotone observation clock, and caller-proved
    positive payments. Post: balance = original + interest - payments;
    no negative balance, duplicate application or silently changed payment.
    A late receipt is inserted at its effective day and the current mark is
    replayed. Advancing or correcting this book sends no transaction.
    """
    def __init__(self, principal, apr_ppb, start_day=0):
        _money(principal)
        _money(apr_ppb)
        _day(start_day)
        self._principal, self._apr_ppb, self._start_day = principal, apr_ppb, start_day
        self._payments = {}  # receipt key -> (effective day, actual principal allocation)
        self._state = MortgageState(start_day, principal, 0, 0)

    principal = property(lambda self: self._principal)
    apr_ppb = property(lambda self: self._apr_ppb)
    start_day = property(lambda self: self._start_day)
    state = property(lambda self: self._state)
    payments = property(lambda self: self._payments.copy())

    def _grow(self, state, day):
        balance, interest = state.balance, state.interest
        # Round once per modeled bank day, independent of observation cadence.
        if balance and self.apr_ppb:
            for _ in range(day - state.day):
                accrued = balance * self.apr_ppb // DAY_RATE_DENOMINATOR
                if not accrued:
                    break  # no payment/rate change can grow this unchanged balance
                if balance + accrued > MAX:
                    raise ValueError("external mortgage balance overflows uint256")
                balance += accrued
                interest += accrued
        return MortgageState(day, balance, interest, state.paid)

    def _replay(self, day, payments):
        state = MortgageState(self.start_day, self.principal, 0, 0)
        for key, (at, amount) in sorted(payments.items(), key=lambda pair: (pair[1][0], pair[0])):
            if at > day:
                break
            state = self._grow(state, at)
            if amount > state.balance:
                raise ValueError("external mortgage payment exceeds effective debt")
            state = MortgageState(at, state.balance - amount, state.interest, state.paid + amount)
        return self._grow(state, day)

    def advance(self, day):
        _day(day)
        if day < self.state.day:
            raise ValueError("external mortgage clock cannot go backwards")
        # No payments are accepted in the future, so normal daily observation
        # needs only the new interval; replay is reserved for late evidence.
        self._state = self._grow(self.state, day)
        return self.state

    def record_payment(self, receipt_key, day, amount):
        _day(day)
        _money(amount)
        if (not isinstance(receipt_key, str) or not receipt_key or not receipt_key.isascii() or not receipt_key.isprintable()
                or amount == 0 or not self.start_day <= day <= self.state.day):
            raise ValueError("invalid external mortgage payment")
        payment = (day, amount)
        if receipt_key in self._payments:
            if self._payments[receipt_key] != payment:
                raise ValueError("external mortgage receipt allocation changed")
            return False
        proposed = self._payments | {receipt_key: payment}
        state = self._replay(self.state.day, proposed)
        # Validate the complete dated history before committing either field.
        self._payments, self._state = proposed, state
        return True

    def at(self, day):
        _day(day)
        if not self.start_day <= day <= self.state.day:
            raise ValueError("external mortgage historical day unavailable")
        return self._replay(day, self._payments)


class HouseholdCalendar:
    """Scheduled external books and one receipt-backed recurring cash flow.

    Bind on arrival, then observe every payday even if owner operations are
    pending. Pure clocks/shadow payments advance independently. USDC income
    is never spendable here until a mint receipt is authenticated. A pending
    owner phase is reconciled before another send; no hash means no retry.
    The normal policy loop remains a separate migration gate.
    """
    PHASES = ("income", "living", "insurance", "mortgage")

    def __init__(self, agent):
        self.agent, self.bound = agent, False
        self.queue, self.flow = [], None
        self.income_due = 0

    @property
    def active(self):
        return self.bound and bool(self.queue)

    def status(self):
        f = self.flow
        refi = self.agent.refinance_machine.flow
        return dict(schema=1, bound=self.bound, day=self.day if self.bound else None,
                    epoch=str(self.epoch) if self.bound else None,
                    queued=len(self.queue), income_due=str(self.income_due),
                    mortgage_provisional=bool(f and f["phase"] == "mortgage" and f["status"] == "pending" or
                                              refi and refi["phase"] == "mortgage" and refi["status"] == "pending" and
                                              (refi.get("terms") is None or refi["terms"]["principal"] > 0)),
                    phase=f["phase"] if f else None, status=f["status"] if f else "idle",
                    hash=f.get("hash") if f else None)

    def _sync(self):
        a = self.agent
        a.mortgage, a.external_interest = self.actual.state.balance, self.interest_offset + self.actual.state.interest
        a.external_paid = self.paid_offset + self.actual.state.paid
        a.hypo_mortgage, a.hypo_interest = self.shadow.state.balance, self.hypo_interest_offset + self.shadow.state.interest
        a.hypo_paid = self.hypo_paid_offset + self.shadow.state.paid

    def bind(self, d, day):
        a = self.agent
        _day(day)
        if (self.bound or day != a.arrive_day or a._started or a.refinance_machine.active or
                getattr(d, "buck_accounting", "") != "net"):
            raise ValueError("calendar must bind an idle canonical household on arrival")
        stamp = d.w3.eth.get_block("latest")["timestamp"]
        epoch = getattr(d, "household_epoch", stamp - day * DAY_SECONDS)
        if type(epoch) is not int or epoch < 0 or (stamp - epoch) // DAY_SECONDS != day:
            raise ValueError("household calendar day does not match chain clock")
        rate = int(a.apr * 10**9)
        self.actual, self.shadow = MortgageBook(a.mortgage, rate, day), MortgageBook(a.hypo_mortgage, rate, day)
        self.interest_offset, self.paid_offset = a.external_interest, a.external_paid
        self.hypo_interest_offset, self.hypo_paid_offset = a.hypo_interest, a.hypo_paid
        self.epoch = epoch
        self.day, self.next_pay = day, first_payday(day, a.p["pay_day"])
        opening = int(a.p["invest_cash_k" if a.investor else "cash_k"] * 1000 * 10**6)
        _money(opening)
        self.queue.append(dict(kind="opening", day=day, income=opening))
        self.income_due += opening
        a.hypo_cash += opening
        self.bound = True

    def prepare(self, d, day):
        """Accrue both debts and schedule bills, without submitting money."""
        if not self.bound:
            self.bind(d, day)
        _day(day)
        if day < self.day:
            raise ValueError("household calendar cannot go backwards")
        if day > self.next_pay:
            raise ValueError("household calendar must observe each payday")
        if (d.w3.eth.get_block("latest")["timestamp"] - self.epoch) // DAY_SECONDS != day:
            raise ValueError("household calendar day does not match chain clock")
        a = self.agent
        # Preflight both bank clocks before accruing a period's household
        # counters; an invalid/overflowing loan must not queue income twice.
        self.actual._grow(self.actual.state, day)
        self.shadow._grow(self.shadow.state, day)
        if day == self.next_pay:
            wages = a.income_annual // 12
            income = wages
            living = 0 if a.investor else wages * int(a.p["living_frac"] * 10**9) // 10**9
            if getattr(a, "lifecycle", False):
                # The lifecycle: the external fund grows a month. An investor (whose wages here are its
                # saving) saves usd_fund_share of them in the fund while it works and nothing once
                # retired. Any other household saves usd_fund_share of what its wages leave after its
                # bills in the fund while it works; retired, it earns pension_frac of its wages while its
                # living costs stay, and draws its savings down for the rest (advance_net_drawdown).
                growth = policy.fund_growth(a.p["usd_fund_return"], 30)
                a.usd_fund = int(a.usd_fund * growth)
                a.hypo_fund = int(a.hypo_fund * growth)          # the counterfactual's fund, alike
                share = int(a.p["usd_fund_share"] * 10**9)
                working = policy.stage(a.p, a.arrive_day, day) == "working"
                if a.investor:
                    fund = wages * share // 10**9 if working else 0
                    income = wages - fund if working else 0
                elif working:
                    bills = living + (0 if a._sold else a._face0 * a.premium_rate // 10_000 // 12) + a.payment
                    fund = max(0, wages - bills) * share // 10**9
                    income = wages - fund
                else:
                    fund, income = 0, wages * int(a.p["pension_frac"] * 10**9) // 10**9
                a.usd_fund += fund
                a.hypo_fund += fund
            full = 0 if a._sold else a._face0 * a.premium_rate // 10_000 // 12
            premium = a._external_premium_due(d, full)  # read before committing this period
            for amount in (income, living, full, premium):
                _money(amount)
            self.queue.append(dict(kind="month", day=day, income=income))
            self.income_due += income
            a.living_arrears += living
            a.premium_arrears += premium
            a.hypo_cash += income
            a.hypo_living_arrears += living
            paid = min(a.hypo_cash, a.hypo_living_arrears)
            a.hypo_cash -= paid
            a.hypo_living_arrears -= paid
            a.hypo_living += paid
            a.hypo_premium_arrears += full
            paid = min(a.hypo_cash, a.hypo_premium_arrears)
            a.hypo_cash -= paid
            a.hypo_premium_arrears -= paid
            a.hypo_premium += paid
            self.shadow.advance(day)
            paid = min(a.hypo_cash, a.payment, self.shadow.state.balance)
            if paid:
                self.shadow.record_payment(f"shadow:{day}", day, paid)
                a.hypo_cash -= paid
            self.next_pay += 30
        self.actual.advance(day)
        self.shadow.advance(day)
        self.day = a._last_day = day
        a._next_pay = self.next_pay
        self._sync()

    def schedule_sale(self, d, day):
        """Queue the insured asset's sale at its insured value on this prepared day.

        The proceeds are a receipt-backed dollar credit like income; arrears
        and then the WHOLE external mortgage are paid from actual dollars in
        the same period. The counterfactual sells the same asset on the same
        day and pays its mortgage. Nothing is sent here.
        """
        a = self.agent
        if (not self.bound or self.day != day or a._sold or a.investor or
                any(period["kind"] == "sale" for period in self.queue)):
            raise ValueError("asset sale needs a prepared calendar and an unsold insured asset")
        _money(a._face0)
        self.queue.append(dict(kind="sale", day=day, income=a._face0))
        self.income_due += a._face0
        a.hypo_cash += a._face0
        paid = min(a.hypo_cash, self.shadow.state.balance)
        if paid:
            self.shadow.record_payment(f"shadow-sale:{day}", day, paid)
            a.hypo_cash -= paid
        self._sync()

    def schedule_settlement(self, d, day):
        """Queue payment of arrears and the whole remaining mortgage from dollars now held."""
        a = self.agent
        if not self.bound or self.day != day or not a._sold:
            raise ValueError("settlement needs a prepared calendar after the asset sale")
        if any(period["kind"] in ("sale", "settle") for period in self.queue):
            return False
        if not (self.actual.state.balance or a.living_arrears or a.premium_arrears):
            return False
        self.queue.append(dict(kind="settle", day=day, income=0))
        return True

    def _after(self, kind, phase):
        if kind == "opening" or phase == "mortgage":
            return "complete"
        return self.PHASES[self.PHASES.index(phase) + 1]

    def record_mortgage(self, d, receipt, amount):
        """Apply actual debt reduction on the receipt's effective model day."""
        stamp = d.w3.eth.get_block(receipt["blockNumber"])["timestamp"]
        day = (stamp - self.epoch) // DAY_SECONDS
        changed = self.actual.record_payment(_txhex(receipt["transactionHash"]), day, amount)
        self._sync()
        return changed, day

    def _emit(self, d, day, status, **detail):
        f = self.flow
        self.agent.refinance_machine._event(d, day, status, kind="calendar",
            phase=f'{f["period"]["kind"]}-{f["phase"]}', due_day=f["period"]["day"], **detail)

    def _income_from_receipt(self, d, receipt, amount, expected_hash):
        if (not receipt or receipt.get("status") != 1 or
                _txhex(receipt.get("transactionHash")).lower() != expected_hash.lower()):
            raise ValueError("household income receipt is not successful or has a wrong hash")
        events = d.usdc.events.Transfer().process_receipt(receipt, errors=DISCARD)
        events = [e for e in events if e["address"].lower() == d.usdc.address.lower()]
        if (len(events) != 1 or events[0]["args"]["from"].lower() != ZERO or
                events[0]["args"]["to"].lower() != self.agent.address.lower() or
                events[0]["args"]["value"] != amount):
            raise ValueError("household income mint evidence does not match terms")
        return receipt

    def _recover(self, d):
        f, a = self.flow, self.agent
        receipt = d.w3.eth.get_transaction_receipt(f["hash"])
        if _txhex(receipt.get("transactionHash")).lower() != f["hash"].lower():
            raise ValueError("household calendar receipt hash mismatch")
        if receipt.get("status") == 0:
            raise TransactionReverted(f["phase"], "household calendar operation reverted", receipt)
        if f["phase"] == "income":
            return self._income_from_receipt(d, receipt, f["amount"], f["hash"])
        return d.net_settlement_ops.payment_from_receipt(a.address, a.SINK, f["amount"], receipt,
                                                        expected_hash=f["hash"]).receipt

    def _apply(self, d, day, receipt):
        a, f = self.agent, self.flow
        phase, amount, effective = f["phase"], f["amount"], None
        if phase == "mortgage":
            _, effective = self.record_mortgage(d, receipt, amount)  # required before financial commit
        elif phase == "income":
            self.income_due -= amount
            if f["period"]["kind"] == "opening":
                a._started = a._growth_arrived = True
            elif f["period"]["kind"] == "sale":
                a._sold = True  # proceeds, not wages: the asset and its external premium are gone
            else:
                a.income_total += amount
        elif phase == "living":
            a.living_arrears -= amount
            a.living_paid += amount
        else:
            a.premium_arrears -= amount
            a.ext_premium += amount
        f["phase"] = self._after(f["period"]["kind"], phase)
        f["hash"], f["status"] = None, "running"
        self._emit(d, day, "confirmed", operation=phase, amounts={"usdc": amount}, effective_day=effective,
                   transaction_hash=_txhex(receipt["transactionHash"]), receipt_block=receipt.get("blockNumber"))

    def _apply_known(self, d, day, receipt):
        f, a = self.flow, self.agent
        f["hash"] = _txhex(receipt["transactionHash"])
        phase = f["phase"]
        try:
            self._apply(d, day, receipt)
            return True
        except Exception as exc:
            if f["phase"] != phase:
                raise  # optional observation failure cannot undo a committed cursor
            # Required receipt-time/book reads can fail before money is
            # booked. Retain this known hash rather than resubmitting.
            a.last_action, a.last_error = "calendar-pending", f"calendar accounting unavailable: {exc}"[:200]
            f["status"] = "pending"
            self._emit(d, day, "pending", reason=a.last_error, transaction_hash=f["hash"])
            return False

    def _mint(self, d, amount):
        return d.chain.send(d.usdc.functions.mint(self.agent.address, amount), expect=Expect.EITHER)

    def advance(self, d, day, ctr):
        self.prepare(d, day)
        a = self.agent
        if a.refinance_machine.active or a._pending_repayment(d):
            return self.active
        while self.queue:
            if self.flow is None:
                a.refinance_machine.decisions += 1
                self.flow = dict(period=self.queue[0], status="running", hash=None, amount=0,
                                 phase="living" if self.queue[0]["kind"] == "settle" else "income")
                self._emit(d, day, "planned", calendar=self.status())
            f = self.flow
            if f["status"] == "pending":
                if not f["hash"]:
                    a.last_error = "calendar hash unavailable; reconcile before continuing"
                    return True
                try:
                    receipt = self._recover(d)
                except TransactionReverted:
                    f["status"] = "refused"  # proved negative evidence permits a fresh attempt next call
                    self._emit(d, day, "refused", transaction_hash=f["hash"])
                    return True
                except Exception as exc:
                    a.last_error = f"calendar receipt unavailable: {exc}"[:200]
                    return True
                if not self._apply_known(d, day, receipt):
                    return True
            while f["phase"] != "complete":
                phase = f["phase"]
                cash = d.usdc.functions.balanceOf(a.address).call()
                closing = f["period"]["kind"] in ("sale", "settle")  # an exit pays the whole balance
                amount = (f["period"]["income"] if phase == "income" else
                          min(cash, a.living_arrears if phase == "living" else a.premium_arrears
                              if phase == "insurance" else self.actual.state.balance if closing
                              else min(a.payment, self.actual.state.balance)))
                f["amount"] = amount
                if not amount:
                    if phase == "income" and f["period"]["kind"] == "opening":
                        a._started = a._growth_arrived = True
                    f["phase"] = self._after(f["period"]["kind"], phase)
                    self._emit(d, day, "skipped", operation=phase)
                    continue
                f["status"], f["hash"] = "pending", None
                receipt, txhash = None, None
                try:
                    if phase == "income":
                        receipt = self._mint(d, amount)
                        txhash = _txhex(receipt.get("transactionHash")) if receipt else ""
                        if receipt and receipt.get("status") == 0 and txhash:
                            raise TransactionReverted("income", "household income mint reverted", receipt)
                        if not receipt or receipt.get("status") != 1 or not txhash:
                            raise TransactionReceiptPending(txhash or None)
                        self._income_from_receipt(d, receipt, amount, txhash)
                    else:
                        receipt = d.net_settlement_ops.pay_exact(a.address, a.SINK, amount).receipt
                except Exception as exc:
                    f["hash"] = getattr(exc, "transaction_hash", None) or txhash
                    known = isinstance(exc, TransactionReverted) and exc.receipt.get("status") == 0 and f["hash"]
                    if known:
                        f["status"] = "refused"
                    a.last_action, a.last_error = "calendar-pending", str(exc)[:200]
                    self._emit(d, day, "refused" if known else "pending", reason=a.last_error, transaction_hash=f["hash"])
                    return True
                if not self._apply_known(d, day, receipt):
                    return True
            self._emit(d, day, "complete")
            self.queue.pop(0)
            self.flow = None
        a.last_action, a.last_error = "calendar", ""
        return False
