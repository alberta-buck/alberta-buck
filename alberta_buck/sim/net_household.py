"""Checkpointed household refinancing on canonical Buck.

An assessed plan is an economic input, not authorization. The existing SDK
rechecks the selected facility, guards the sale and authenticates each receipt.
Only a confirmed bank payment changes the external mortgage. A lost reply
stops at that exact phase until its saved receipt can be reconciled. This
module does not choose an economic plan or implement calendar cash flows.

The append-only public events use decimal strings for money, with stable
decision/sequence identifiers. They are suitable for saved JSON and live UI
consumers; recording an unavailable account observation never invents a zero
or reverses an already applied financial transition.
"""
from dataclasses import asdict, dataclass
import math
from alberta_buck.sim.household_policy import external_premium_due

from buck_core import (IssuancePending, IssueCandidate, TransactionReceiptPending, TransactionReverted,
                       mark_net_basket_positions, observe_net_market)
from buck_core.session import _txhex

RAW = 10**6
PHASES = ("fund", "issue", "export", "mortgage")


@dataclass(frozen=True)
class RefinancePlan:
    candidate: IssueCandidate | None
    export_buck: int
    minimum_usdc: int
    mortgage_limit: int
    cash_buffer: int
    reserve_floor: int
    switch_cost: int = 0
    penalty_numerator: int = 0
    penalty_denominator: int = 1
    reserve_buy_net: int = 0
    reserve_buy_max_usdc: int = 0


def _wire(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite household event value")
    if isinstance(value, bool) or value is None or isinstance(value, (str, float)):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    raise TypeError("unsupported household event value")


class HouseholdRefinance:
    """One serialized operation, mirrored by JS HouseholdRefinance.

    Pre: an ordinary household, separately tracked debt, actual proxy SDK
    adapters and no other pending household operation. The caller supplies
    an assessed plan with existing reserves or a bounded reserve purchase,
    and serializes the owner's actions.
    Post: confirmed phases apply once, in order; uncertain phases cannot
    be retried. Neither an issuance nor an export is mortgage repayment.
    This is local-EVM receipt evidence, not a chain-finality policy or a
    complete saved-world import format.
    """
    def __init__(self, agent):
        self.agent = agent
        self.flow = None
        self.events = []
        self.decisions = 0

    @property
    def active(self):
        return self.flow is not None and self.flow["status"] in ("running", "pending")

    def status(self):
        f = self.flow
        return {"schema": 1, "decision": self.decisions, "status": f["status"] if f else "idle",
                "phase": f["phase"] if f else None, "hash": f.get("hash") if f else None,
                "events": len(self.events)}

    def _accounts(self, d):
        a = self.agent
        try:
            block = d.w3.eth.block_number
            read = lambda fn: fn.call(block_identifier=block)
            cash, bucks = read(d.usdc.functions.balanceOf(a.address)), read(d.buck.functions.balanceOf(a.address))
            facilities = []
            for token_id in a._token_ids:
                principal, seconds = read(d.credit.functions.issuanceOf(token_id))
                face, active, _ = read(d.credit.functions.creditInfo(token_id))
                facilities.append({"token_id": token_id, "issued": principal, "claim_seconds": seconds,
                                   "face": face, "activated": active,
                                   "premium_nominal": read(d.buck.functions.premiumPrincipal(token_id))})
            issued = sum(f["issued"] for f in facilities)
            premium = sum(f["premium_nominal"] for f in facilities)
            limit = read(d.buck.functions.creditLimit(a.address))
            buffer = self.flow["plan"].cash_buffer if self.flow else a.cash_buffer
            reserve = self.flow["plan"].reserve_floor if self.flow else 0
            monthly_premium = 0 if a._sold else a._face0 * a.premium_rate // 10_000 // 12
            external_premium = external_premium_due(monthly_premium, a._face0, sum(f["activated"] for f in facilities))
            custody = {}
            for name, proxy in (("savings", a._savings_proxy), ("wallet", a._wallet)):
                if proxy is not None:
                    custody[name] = {"usdc": read(d.usdc.functions.balanceOf(proxy.address)),
                                     "buck_scaled": read(d.buck.functions.netBalanceScaled(proxy.address))}
            return _wire({"usdc": cash, "buck": bucks, "buck_scaled": read(d.buck.functions.netBalanceScaled(a.address)),
                          "issued": issued, "credit_limit": limit, "custody": custody, "facilities": facilities,
                          "premium_nominal": premium, "mortgage": a.mortgage,
                          "external_paid": a.external_paid, "premium_paid": a.premium_paid,
                          "switch_paid": a.switch_paid, "penalty_paid": a.penalty_paid,
                          "external_premium_monthly": external_premium,
                          "counterfactual_premium_monthly": monthly_premium,
                          "cash_buffer": buffer, "reserve_floor": reserve,
                          "ratios": {"utilization": issued / limit if limit else None,
                                     "cash_buffer_cover": cash / buffer if buffer else None,
                                     "reserve_cover": bucks / reserve if reserve else None}})
        except Exception as exc:
            return {"unavailable": str(exc)[:200]}

    def _market(self, d):
        """One pinned decision observation; it does not predict execution."""
        try:
            block = d.w3.eth.block_number
            read = lambda fn: fn.call(block_identifier=block)
            quote = observe_net_market(d.chain, d.buck, d.usdc, d.pool_ub, d.fee_ub, block_number=block)
            return _wire({"block": block, "k_wad": read(d.kctrl.functions.currentBuckK()),
                          "bvib_wad": read(d.basket.functions.basketValueInBuck()),
                          "nominal_usdc_per_buck": {"numerator": quote.price_num, "denominator": quote.price_den},
                          "active_reserves": {"usdc": quote.reserve_usdc, "buck": quote.reserve_buck},
                          "pool_nominal_scaled": quote.gross_scaled, "pool_net_scaled": quote.net_scaled, "fee_ppm": d.fee_ub})
        except Exception as exc:
            return {"unavailable": str(exc)[:200]}

    def _basket(self, d):
        a = self.agent
        if not a._basket_receipts:
            return {"basis": "0", "mark": "0", "roi": None, "positions": []}
        try:
            owner = a._savings_proxy.address if a._savings_proxy is not None else a.address
            mark = mark_net_basket_positions(d.chain, d.basket, d.buck, owner, a._basket_receipts)
            return _wire({"basis": mark.basis, "mark": mark.mark, "roi": mark.roi,
                          "positions": [{"token_id": p.token_id, "shares": p.shares, "basis": p.basis, "mark": p.mark}
                                        for p in mark.positions]})
        except Exception as exc:
            return {"unavailable": str(exc)[:200]}

    def _event(self, d, day, status, **detail):
        f = self.flow
        try:
            block = d.w3.eth.block_number
        except Exception:
            block = None
        self.events.append({"schema": 1, "seq": len(self.events) + 1, "member": self.agent.member,
                            "decision": self.decisions, "day": day, "block": block,
                            "phase": f["phase"], "status": status, "accounts": self._accounts(d),
                            **_wire(detail)})

    def start(self, d, plan, day, ctr, *, context=None):
        if self.active:
            raise ValueError("household refinancing already active")
        if not isinstance(plan, RefinancePlan):
            raise ValueError("invalid household refinance plan")
        values = (plan.export_buck, plan.minimum_usdc, plan.mortgage_limit, plan.cash_buffer,
                  plan.reserve_floor, plan.switch_cost, plan.penalty_numerator, plan.penalty_denominator,
                  plan.reserve_buy_net, plan.reserve_buy_max_usdc)
        if (any(type(v) is not int or not 0 <= v < 2**256 for v in values) or
                min(plan.export_buck, plan.minimum_usdc, plan.mortgage_limit, plan.penalty_denominator) <= 0 or
                plan.mortgage_limit > min(plan.minimum_usdc, self.agent.mortgage) or
                plan.cash_buffer < self.agent.cash_buffer):
            raise ValueError("invalid household refinance bounds")
        c = plan.candidate
        funding = plan.reserve_buy_net > 0
        if bool(plan.reserve_buy_max_usdc) != funding or funding and c is None:
            raise ValueError("invalid household reserve funding")
        if c is not None and (not isinstance(c, IssueCandidate) or
                              any(reason != "reserve-shortfall" or not funding for reason in c.reasons) or
                              c.token_id not in self.agent._token_ids):
            raise ValueError("unavailable household issuance candidate")
        if c is not None and plan.reserve_buy_net < c.reserve_shortfall:
            raise ValueError("refinance reserve purchase is insufficient")
        if c is not None and plan.reserve_floor < c.reserve_required + c.reserve_buffer:
            raise ValueError("refinance must retain the quoted BUCK reserve")
        if c is None and plan.switch_cost:
            raise ValueError("wallet prepayment cannot switch insurance")
        book = d.net_credit_ops.household(self.agent.address, self.agent._token_ids)
        if any(d.buck.functions.reductionAuthorization(t).call()[0] for t in self.agent._token_ids):
            raise ValueError("household reduction authorization pending")
        fresh = c.requested_spendable if c else 0
        if book.spendable + fresh + plan.reserve_buy_net < plan.export_buck + plan.reserve_floor:
            raise ValueError("refinance would consume the BUCK reserve")
        penalty = (plan.mortgage_limit * plan.penalty_numerator + plan.penalty_denominator - 1) // plan.penalty_denominator
        if plan.mortgage_limit + plan.switch_cost + penalty >= 2**256:
            raise ValueError("household bank allocation overflows uint256")
        cash = d.usdc.functions.balanceOf(self.agent.address).call()
        if funding and cash < plan.cash_buffer + plan.reserve_buy_max_usdc:
            raise ValueError("reserve purchase would consume the USDC buffer")
        if cash + plan.minimum_usdc < (plan.cash_buffer + plan.reserve_buy_max_usdc + plan.switch_cost +
                                        plan.mortgage_limit + penalty):
            raise ValueError("refinance would consume the USDC buffer")
        context = _wire(context or {})
        self.decisions += 1
        self.flow = {"plan": plan, "phase": "fund" if funding else "issue" if c else "export",
                     "status": "running", "hash": None, "issue_candidate": None,
                     "initial_joined": self.agent.joined, "terms": None}
        self.agent.funding_pending = True
        limits = {key: self.agent.p[key] for key in
                  ("surplus_policy", "theta", "issue_premium", "retire_disc", "utilization", "max_impact_bp")}
        self._event(d, day, "planned", plan=asdict(plan), context=context, limits=limits,
                    market=self._market(d), basket=self._basket(d))
        return self.advance(d, day, ctr)

    def _refuse(self, d, day, reason):
        self.flow["status"] = "refused"
        self.agent.funding_pending = False
        self.agent.last_error = reason[:200]
        self.agent.last_action = "refinance-refused"
        self._event(d, day, "refused", reason=reason[:200], transaction_hash=self.flow.get("hash"))

    def _apply(self, d, day, result, ctr):
        a, f = self.agent, self.flow
        phase, receipt = f["phase"], result.receipt
        moved = {}
        if phase == "fund":
            p = f["plan"]
            if not 0 < result.paid <= p.reserve_buy_max_usdc or result.received < p.reserve_buy_net:
                raise ValueError("household reserve purchase does not match bounds")
            a.trade_loss += result.paid - result.received
            moved = {"usdc": result.paid, "net_buck_scaled": result.net_scaled, "nominal_buck": result.nominal_received}
        elif phase == "issue":
            a.premium_paid += result.premium
            a.joined = True
            moved = {"principal": result.principal, "premium": result.premium, "spendable": result.spendable}
        elif phase == "export":
            a.trade_loss += result.paid - result.received
            ctr["rtrBuckSold"] = ctr.get("rtrBuckSold", 0) + result.paid // RAW
            moved = {"buck": result.paid, "nominal_buck": result.nominal_paid, "usdc": result.received}
        else:
            terms = f["terms"]
            if result.amount != terms["amount"] or a.mortgage < terms["principal"]:
                raise ValueError("household bank allocation changed")
            a.mortgage -= terms["principal"]
            a.external_paid += terms["principal"]
            a.switch_paid += terms["switch"]
            a.penalty_paid += terms["penalty"]
            a.refis += 1
            a.deploys += 1
            key = "rtrTopUps" if f["initial_joined"] else "rtrRefis"
            ctr[key] = ctr.get(key, 0) + 1
            ctr["rtrUsdRetired"] = ctr.get("rtrUsdRetired", 0) + terms["principal"] // RAW
            moved = {"usdc": result.amount, "principal_usdc": terms["principal"],
                     "switch_usdc": terms["switch"], "penalty_usdc": terms["penalty"]}
        # Commit the financial transition and cursor before optional observations.
        f["hash"], f["terms"], f["status"] = None, None, "running"
        f["phase"] = PHASES[PHASES.index(phase) + 1] if phase != "mortgage" else "complete"
        self._event(d, day, "confirmed", operation=phase, amounts=moved,
                    transaction_hash=_txhex(receipt["transactionHash"]), receipt_block=receipt.get("blockNumber"))

    def _recover(self, d):
        f, a = self.flow, self.agent
        receipt = d.w3.eth.get_transaction_receipt(f["hash"])
        if _txhex(receipt.get("transactionHash")).lower() != f["hash"].lower():
            raise ValueError("household receipt hash mismatch")
        if receipt.get("status") == 0:
            raise TransactionReverted(f["phase"], "household operation reverted", receipt)
        p = f["plan"]
        if f["phase"] == "fund":
            return d.net_market_ops.purchase_from_receipt(a.address, d.pool_ub, a.address, receipt,
                                                          expected_hash=f["hash"])
        if f["phase"] == "issue":
            return d.net_credit_ops.issuance_from_receipt(a.address, f["issue_candidate"], receipt, expected_hash=f["hash"])
        if f["phase"] == "export":
            return d.net_market_ops.sale_from_receipt(a.address, d.pool_ub, a.address, p.export_buck,
                                                      p.minimum_usdc, receipt, expected_hash=f["hash"])
        return d.net_settlement_ops.payment_from_receipt(a.address, a.SINK, f["terms"]["amount"], receipt,
                                                         expected_hash=f["hash"])

    def advance(self, d, day, ctr):
        """Advance at most four sends, or reconcile the single pending phase.

        A missing/hashless/mismatched receipt leaves the flow pending, with
        no new send and no book changes. A proved revert ends this attempt;
        earlier confirmed cash, issuance and payments remain real holdings.
        """
        if not self.active:
            return False
        f, a = self.flow, self.agent
        if f["status"] == "pending":
            if not f["hash"]:
                a.last_error = "refinance hash unavailable; reconcile before continuing"
                return True
            try:
                result = self._recover(d)
            except TransactionReverted as exc:
                self._refuse(d, day, str(exc))
                return True
            except Exception as exc:
                a.last_error = f"refinance receipt unavailable: {exc}"[:200]
                return True
            self._apply(d, day, result, ctr)
        while f["phase"] != "complete":
            p, phase = f["plan"], f["phase"]
            if phase == "issue":
                # Funding is a real prior transaction. A failed view can be
                # retried here without buying twice or submitting an issue.
                c = p.candidate
                try:
                    fresh = d.net_credit_ops.assess_issue_candidate(a.address, c.token_id, c.coverage,
                        c.requested_spendable, c.max_premium, reserve_buffer=c.reserve_buffer)
                except Exception as exc:
                    a.last_error = f"refinance issuance observation unavailable: {exc}"[:200]
                    self._event(d, day, "waiting", reason=a.last_error)
                    return True
                unchanged = ("prior_principal", "before_coverage", "after_coverage", "premium")
                if (fresh.reasons or any(getattr(fresh, key) != getattr(c, key) for key in unchanged) or
                        fresh.premium_pool.lower() != c.premium_pool.lower() or
                        fresh.reserve_required + fresh.reserve_buffer > p.reserve_floor):
                    self._refuse(d, day, "refinance issuance terms changed; reassess retained holdings")
                    return True
                f["issue_candidate"] = fresh
            elif phase == "fund":
                if d.usdc.functions.balanceOf(a.address).call() < p.reserve_buy_max_usdc + p.cash_buffer:
                    self._refuse(d, day, "refinance reserve USDC buffer changed")
                    return True
            elif phase == "mortgage":
                penalty = (p.mortgage_limit * p.penalty_numerator + p.penalty_denominator - 1) // p.penalty_denominator
                amount = p.mortgage_limit + p.switch_cost + penalty
                if d.usdc.functions.balanceOf(a.address).call() < amount + p.cash_buffer:
                    self._refuse(d, day, "refinance USDC buffer changed")
                    return True
                f["terms"] = {"amount": amount, "principal": p.mortgage_limit, "switch": p.switch_cost, "penalty": penalty}
            elif phase == "export" and d.buck.functions.balanceOf(a.address).call() < p.export_buck + p.reserve_floor:
                self._refuse(d, day, "refinance BUCK reserve changed")
                return True
            # Save the phase before dispatch. Exceptions after dispatch can
            # never turn an unknown outcome into permission to send again.
            f["status"], f["hash"] = "pending", None
            try:
                if phase == "fund":
                    result = d.net_market_ops.buy_exact_output(a.address, d.pool_ub, p.reserve_buy_net,
                                                               p.reserve_buy_max_usdc, a.address)
                elif phase == "issue":
                    result = d.net_credit_ops.issue_candidate(a.address, f["issue_candidate"])
                elif phase == "export":
                    result = d.net_market_ops.sell_exact_input(a.address, d.pool_ub, p.export_buck,
                                                                p.minimum_usdc, a.address, d.fee_ub)
                else:
                    result = d.net_settlement_ops.pay_exact(a.address, a.SINK, f["terms"]["amount"])
            except (TransactionReceiptPending, IssuancePending, TransactionReverted) as exc:
                f["hash"] = exc.transaction_hash or None
                known_revert = (isinstance(exc, IssuancePending) and exc.reverted or
                                isinstance(exc, TransactionReverted) and exc.receipt.get("status") == 0)
                if known_revert and f["hash"]:
                    self._refuse(d, day, str(exc))
                else:
                    a.last_action = "refinance-pending"
                    a.last_error = str(exc)[:200]
                    self._event(d, day, "pending", reason=a.last_error, transaction_hash=f["hash"])
                return True
            except Exception as exc:
                a.last_action, a.last_error = "refinance-pending", str(exc)[:200]
                self._event(d, day, "pending", reason=a.last_error, transaction_hash=None)
                return True
            self._apply(d, day, result, ctr)
        f["status"] = "complete"
        a.funding_pending, a.last_action, a.last_error = False, "refinance", ""
        self._event(d, day, "complete")
        return True
