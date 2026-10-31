"""Households, insured savers and investors with portable decisions and separate, real contract accounts."""

import math

from alberta_buck.sim import carry, household_policy as policy
from alberta_buck.sim.agents import Agent, _register
from alberta_buck.sim.equilibrium_agents import _HouseholdAccounts, PARITY, _impact_cap, mint_amount, mint_capacity
from alberta_buck.sim.experiment import spec
from alberta_buck.sim.gauge import active_reserves, buck_usd6
from buck_core import ReductionPlan, TransactionReceiptPending, TransactionReverted
from buck_core.session import _txhex

# The basket's price guard, by name or (as a bare revert reports it) by selector: keccak("Slippage()").
SLIPPAGE                        = ("Slippage", "0x7dd37f70")


@_register
class CreditHouseholdAgent(_HouseholdAccounts):
    """One household with its own decisions and shared contract/accounting operations."""
    OCTL_STRIDE                 = 5                     # its books are read every fifth day (and the last)

    def setup(self, d, scenario, rng):
        if getattr(d, "buck_accounting", "") == "net":
            raise NotImplementedError("BuckNet household setup is not yet integrated")
        cls                     = type(self).__name__
        overrides               = {k: spec(scenario, cls, k, v) for k, v in policy.DEFAULTS.items()}
        overrides.update(getattr(self, "settings", {}))
        self.member             = getattr(self, "member", str(self.idx))
        self.p                  = policy.profile(scenario.seed, self.member, overrides)
        p                       = self.p
        self.arrive_day         = getattr(self, "arrive_day", int(spec(scenario, cls, "arrive_day", 0)))
        self._last_day          = self.arrive_day
        self._next_pay          = self.arrive_day + 30
        self._next_decision     = self.arrive_day + p["phase"]
        self._started           = False
        self._growth_arrived    = False
        self._otc               = False
        # An investor brings dollars and a yearly saving; no insured asset, mortgage or cash buffer.
        self.investor           = p["role"] == "investor"
        self._face0             = 0 if self.investor else int(p["home_k"] * 1000 * PARITY)
        self._face              = self._face0
        self.mortgage           = int(self._face0 * p["ltv"]) if p["role"] == "debtor" else 0
        self._mortgage0         = self.mortgage
        self.hypo_mortgage      = self.mortgage
        self.hypo_cash          = 0
        self.income_annual      = (int(p["invest_income_k"] * 1000 * PARITY) if self.investor
                                   else int(self._face0 * p["income_frac"]))
        self.apr                = p["apr"]
        rate                    = self.apr / 12
        self.payment            = (int(self.mortgage * rate / (1 - (1 + rate) ** -300))
                                   if rate else self.mortgage // 300)
        self.premium_rate       = p["premium_bp"]
        self.cash_buffer        = 0 if self.investor else int(p["buffer_k"] * 1000 * PARITY)
        self._exit_day          = self.arrive_day + int(365 * p["hold_years"]) if self.investor else None
        self._divest_day        = (None if self.investor
                                   else policy.divest_day(scenario.seed, self.member, self.arrive_day, p["divest_rate"]))
        self._wallet            = None                  # the BUCK working balance, apart from the lien
        self._net_repayment_reserve = None              # pre-buyback balance while acquired BUCK waits
        self._net_pending_token = None                  # uncertain settlement must be reconciled
        self._net_pending_principal = 0
        self._net_pending_minimum = None
        self._net_pending_deadline = None
        self._net_pending_hash = None
        self._net_pending_reverted = False
        self._net_pending_phase = None
        self._net_authorization_confirmed = False
        self._net_repayment_interrupted = False         # re-read state on a later turn after a failed read
        self._net_cancel_uncertain = False
        self._net_cancel_hash = None
        self._net_trade_uncertain = False               # uncertain guarded buy must be reconciled
        self._net_pending_buy_hash = None
        self.wallet_bought      = 0
        self._sold              = False                 # the insured asset has been sold
        self._released          = False                 # its coverage has been released
        self.exit_started       = None
        self.exit_stage         = ""
        self.exit_retries       = 0
        self.exit_short         = False
        self.exited             = False
        self.withdrawn_usd      = 0
        self.refusals           = 0
        self._receipt_book      = {}                    # receipt -> (principal, shares), taken off at exit
        self.max_impact_bp      = p["max_impact_bp"]
        self.theta              = p["theta"]
        self.pattern            = "salary"
        self.surplus_policy     = p["surplus_policy"]
        self.joined             = False
        self.refis              = 0
        self.deploys            = 0
        self.throttled          = 0
        self.premium_paid        = 0
        self.ext_premium         = 0
        self.hypo_premium        = 0
        self.switch_paid         = 0
        self.penalty_paid        = 0
        self.external_interest  = 0
        self.hypo_interest      = 0
        self.income_total       = 0
        self.living_paid        = 0
        self.hypo_living        = 0
        self.living_arrears     = 0
        self.hypo_living_arrears = 0
        self.premium_arrears    = 0
        self.hypo_premium_arrears = 0
        self.external_paid      = 0
        self.hypo_paid          = 0
        self.trade_loss         = 0
        self.unwound            = 0
        self.unwind_loss        = 0
        self._basket_in         = 0
        self._basket_shares     = 0
        self._basket_receipt    = None
        self._basket_receipts   = []
        self._savings_proxy     = None
        self.invested_usd       = 0
        self.issued_invest      = 0
        self.funding_pending    = False
        self._last_verdict      = None
        self.last_action        = "waiting"
        self.last_issue         = "waiting"
        self.last_surplus       = "waiting"
        self.last_error         = ""
        self._token_idx         = int(policy.uniform(scenario.seed, self.member, "token") * len(d.tokens))
        self._bind_proxy(d)
        if self.investor:
            self._token_ids     = []
            return
        self._proxy_exec(d, d.credit.address, d.credit.encode_abi(
            "setCreditIssuer", args=[getattr(d.chain.deployer, "address", d.chain.deployer), True]))
        now                     = d.w3.eth.get_block("latest")["timestamp"]
        per                     = self._face0 // 4
        for face in (per, per, per, self._face0 - 3 * per):
            d.chain.send(d.credit.functions.createCredit(self.proxy.address, 0, face, 0, 0, 0, now, self.premium_rate))
        self._token_ids         = [d.credit.functions.tokenOfOwnerByIndex(self.proxy.address, i).call() for i in range(4)]

    def bootstrap(self, d, scenario, ctr):
        pass  # arrivals use the live market; they are not an opening OTC placement

    def _monthly(self, d):
        income                  = self.income_annual // 12
        d.chain.send(d.usdc.functions.mint(self.proxy.address, income))
        self.income_total      += income
        self.hypo_cash         += income
        living                  = 0 if self.investor else int(income * self.p["living_frac"])
        self.living_arrears    += living
        paid                    = self._pay_bank(d, self.living_arrears)
        self.living_arrears    -= paid
        self.living_paid       += paid
        self.hypo_living_arrears += living
        paid                    = min(self.hypo_living_arrears, self.hypo_cash)
        self.hypo_living_arrears -= paid
        self.hypo_cash         -= paid
        self.hypo_living       += paid
        premium                 = 0 if self._sold else self._face0 * self.premium_rate // 10_000 // 12
        if not self.joined:
            self.premium_arrears += premium
        paid                    = self._pay_bank(d, self.premium_arrears)
        self.premium_arrears   -= paid
        self.ext_premium       += paid
        self.hypo_premium_arrears += premium
        paid                    = min(self.hypo_premium_arrears, self.hypo_cash)
        self.hypo_premium_arrears -= paid
        self.hypo_cash         -= paid
        self.hypo_premium      += paid
        paid                    = self._pay_bank(d, min(self.payment, self.mortgage))
        self.mortgage          -= paid
        self.external_paid     += paid
        paid                    = min(self.payment, self.hypo_mortgage, self.hypo_cash)
        self.hypo_cash         -= paid
        self.hypo_mortgage     -= paid
        self.hypo_paid         += paid
        self._tend_wallet(d)

    def _tend_wallet(self, d):
        """Payday: bring the BUCK working balance to its target (household_policy.money_target), buying
        the shortfall with dollars or selling the excess, a capped bite either way."""
        spending                = self.income_annual * self.p["living_frac"] / PARITY
        if self.investor or self._sold or spending <= 0 or self.p["money_months"] <= 0 or not d.pool_ub:
            return
        ru, rb                  = active_reserves(d.chain, d.pool_ub, d.usdc, d.buck)
        fee                     = d.fee_ub / 1e6
        d_now                   = policy.discount(ru / rb, self._basket_usd(d), fee)
        target                  = int(policy.money_target(self._last_day - self.arrive_day, spending, d_now, self.p) * PARITY)
        if self._wallet is None:
            self._wallet        = self._new_proxy(d)
        held                    = d.buck.functions.balanceOf(self._wallet.address).call()
        if held < target * 95 // 100:
            want                = min(target - held, rb // 2)
            cost                = int(want * ru / ((rb - want) * (1 - fee))) + 1
            cash                = d.chain.balance_of(d.usdc, self.proxy.address)
            spend               = min(cash, cost, _impact_cap(ru, self.max_impact_bp))
            if spend > PARITY:
                self._swap_via_simlp(d, d.pool_ub, d.usdc, spend, self._wallet.address)
                got             = d.buck.functions.balanceOf(self._wallet.address).call() - held
                self.trade_loss += spend - got
                self.wallet_bought += got
        elif held > target * 110 // 100:
            bite                = min(held - target, _impact_cap(rb, self.max_impact_bp))
            if bite > PARITY:
                self._swap_via_simlp(d, d.pool_ub, d.buck, bite, self.proxy.address, from_proxy=self._wallet)

    def _advance(self, d, day):
        while self._last_day < day:
            end                 = min(day, self._next_pay)
            growth              = (1 + self.apr / 365) ** (end - self._last_day)
            actual              = int(self.mortgage * growth)
            hypo                = int(self.hypo_mortgage * growth)
            self.external_interest += actual - self.mortgage
            self.hypo_interest  += hypo - self.hypo_mortgage
            self.mortgage, self.hypo_mortgage = actual, hypo
            self._last_day      = end
            if end == self._next_pay:
                self._monthly(d)
                self._next_pay += 30

    def _state(self, d, day):
        if getattr(d, "buck_accounting", "") == "net":
            position            = d.net_credit_ops.household(self.proxy.address, self._token_ids)
            available           = d.net_credit_ops.quote_household_activated_capacity(self.proxy.address, self._token_ids)
            drawn, unactivated  = position.issued, available.unactivated_present
        else:
            signed              = d.buck.functions.signedBalanceOf(self.proxy.address).call()
            drawn               = max(0, -signed)
            unactivated         = sum(max(0, f - a) for f, a, _ in (
                d.credit.functions.creditInfo(t).call() for t in self._token_ids))
        ru, rb                  = active_reserves(d.chain, d.pool_ub, d.usdc, d.buck)
        month                   = (day - self.arrive_day) // 30
        state                   = dict(mortgage=self.mortgage / PARITY, face=self._face0 / PARITY, joined=self.joined,
                                       k=d.kctrl.functions.buckK().call() / 1e18, drawn=drawn / PARITY,
                                       spendable=d.buck.functions.balanceOf(self.proxy.address).call() / PARITY,
                                       unactivated=unactivated / PARITY, ru=ru / PARITY, rb=rb / PARITY,
                                       fee=d.fee_ub / 1e6, basket_usd=self._basket_usd(d),
                                       at_renewal=(month - self.p["renew_in"]) % self.p["term_months"] == 0)
        if getattr(d, "buck_accounting", "") == "net":
            state["accounting"] = "net"  # policy refuses absent explicit forecasts
            state["activated_capacity"] = available.capacity / PARITY
            state["issuance_limit"] = (0 if available.maturities_pending else available.shared_limit / PARITY)
            state["maturities_pending"] = available.maturities_pending
            if available.maturities_pending:
                state["unactivated"] = 0  # no issuance can pass until the keeper advances the claim book
        return state

    def _buy_cash(self, d, budget, minimum_net=0, *, for_repayment=False):
        if getattr(d, "buck_accounting", "") == "net":
            cash                = d.chain.balance_of(d.usdc, self.proxy.address)
            spend               = min(cash, budget)
            if spend <= 0:
                return 0
            if for_repayment:
                self._net_repayment_reserve = d.buck.functions.balanceOf(self.proxy.address).call() + 1
                self._net_trade_uncertain = True
                self._net_pending_buy_hash = None
            try:
                purchase        = d.net_market_ops.buy_exact_input(
                    self.proxy.address, d.pool_ub, spend, minimum_net, self.proxy.address)
            except TransactionReverted:
                if for_repayment:
                    self._net_trade_uncertain = False
                    self._net_repayment_reserve = None
                raise
            except TransactionReceiptPending as exc:
                if for_repayment:
                    self._net_pending_buy_hash = exc.transaction_hash
                raise
            if for_repayment:
                self._net_trade_uncertain = False
                if not purchase.received:
                    self._net_repayment_reserve = None
            self.trade_loss    += purchase.paid - purchase.received
            return purchase.received
        before                  = d.buck.functions.signedBalanceOf(self.proxy.address).call()
        cash                    = d.chain.balance_of(d.usdc, self.proxy.address)
        self._swap_via_simlp(d, d.pool_ub, d.usdc, min(cash, budget), self.proxy.address)
        got                     = d.buck.functions.signedBalanceOf(self.proxy.address).call() - before
        spent                   = cash - d.chain.balance_of(d.usdc, self.proxy.address)
        self.trade_loss         += spent - got
        return got

    def _drawn(self, d):
        if getattr(d, "buck_accounting", "") == "net":
            return d.net_credit_ops.household(self.proxy.address, self._token_ids).issued
        return max(0, -d.buck.functions.signedBalanceOf(self.proxy.address).call())

    def _pending_repayment(self, d):
        if getattr(d, "buck_accounting", "") != "net":
            return False
        if getattr(self, "_net_trade_uncertain", False):
            self.last_error     = "buyback outcome uncertain; reconcile trade receipt before buying more"
            self.last_action    = "buy-pending"
            return True
        if getattr(self, "_net_cancel_uncertain", False):
            self.last_error     = "repayment cancellation outcome uncertain; reconcile receipt"
            self.last_action    = "cancel-pending"
            return True
        tracked                 = getattr(self, "_net_pending_token", None)
        if tracked is not None:
            principal           = d.buck.functions.reductionAuthorization(tracked).call()[0]
            self.last_error     = (f"facility {tracked} repayment pending; reconcile or cancel before buying more"
                                   if principal else f"facility {tracked} repayment outcome uncertain; reconcile receipt")
            self.last_action    = "repay-pending"
            return True
        if getattr(self, "_net_repayment_interrupted", False):
            self.last_action    = "repay-wait"
            return True
        for token_id in self._token_ids:
            if d.buck.functions.reductionAuthorization(token_id).call()[0]:
                self.last_error = f"facility {token_id} repayment pending; reconcile or cancel before buying more"
                self.last_action = "repay-pending"
                return True
        return False

    def _recover_pending_operations(self, d, ctr):
        """Reconcile one known receipt before this household submits more work.

        A recovered outcome consumes this turn. Unknown hashes, an untracked
        authorization remains stopped for external reconciliation. A confirmed
        authorization or reverted settlement can be canceled by this dispatcher.
        This simulation assumes a single dispatcher and immediate finality.
        """
        if getattr(d, "buck_accounting", "") != "net":
            return False
        try:
            if getattr(self, "_net_trade_uncertain", False):
                if getattr(self, "_net_pending_buy_hash", None):
                    self.reconcile_pending_buy(d)
                else:
                    self._pending_repayment(d)
                return True
            if getattr(self, "_net_cancel_uncertain", False):
                if getattr(self, "_net_cancel_hash", None):
                    self.reconcile_pending_cancellation(d)
                else:
                    self._pending_repayment(d)
                return True
            if getattr(self, "_net_pending_token", None) is not None:
                phase           = getattr(self, "_net_pending_phase", None)
                auth_confirmed  = getattr(self, "_net_authorization_confirmed", False)
                reverted        = getattr(self, "_net_pending_reverted", False)
                recover         = (phase == "authorization" and not auth_confirmed) or (
                                   phase == "settlement" and not reverted)
                if recover and getattr(self, "_net_pending_hash", None):
                    self.reconcile_pending_repayment(d, ctr)
                elif auth_confirmed or (phase == "settlement" and reverted):
                    self.cancel_pending_repayment(d)
                else:
                    self._pending_repayment(d)
                return True
            if getattr(self, "_net_repayment_interrupted", False):
                self._net_repayment_interrupted = False
                self.last_action = "repay-recheck"
                return True
            return self._pending_repayment(d)
        except Exception as exc:
            self.last_action    = ("buy-pending" if getattr(self, "_net_trade_uncertain", False) else
                                   "cancel-pending" if getattr(self, "_net_cancel_uncertain", False) else "repay-pending")
            self.last_error     = f"transaction reconciliation: {exc!r}"[:200]
            ctr["household_err"] = self.last_error
            return True

    def cancel_pending_repayment(self, d):
        """Cancel a still-present matching authorization; never infer an absent transaction failed."""
        if getattr(self, "_net_cancel_uncertain", False):
            raise ValueError("cancellation outcome uncertain; reconcile receipt before canceling again")
        token_id                = getattr(self, "_net_pending_token", None)
        expected                = (getattr(self, "_net_pending_principal", 0),
                                   getattr(self, "_net_pending_minimum", None),
                                   getattr(self, "_net_pending_deadline", None))
        if token_id is None or not expected[0] or expected[1] is None or expected[2] is None:
            raise ValueError("pending repayment needs transaction reconciliation")
        phase                   = getattr(self, "_net_pending_phase", "settlement")
        safe                    = (phase == "settlement" and getattr(self, "_net_pending_reverted", False)) or (
                                   phase == "authorization" and getattr(self, "_net_authorization_confirmed", False))
        if not safe:
            raise ValueError("settlement outcome uncertain; reconcile transaction before canceling")
        actual                  = tuple(d.buck.functions.reductionAuthorization(token_id).call())
        if actual != expected:
            raise ValueError("pending repayment changed; reconcile transaction before canceling")
        self._net_cancel_uncertain = True
        self._net_cancel_hash = None
        receipt                 = None
        try:
            receipt             = d.net_credit_ops.cancel_reduction(self.proxy.address, token_id)
            tx_hash             = _txhex(receipt.get("transactionHash")) if receipt else ""
            if receipt and receipt.get("status") == 0 and tx_hash:
                raise TransactionReverted("cancelReduction", "cancellation reverted", receipt)
            if not tx_hash or receipt.get("status") != 1:
                raise TransactionReceiptPending(tx_hash)
            plan                = ReductionPlan(token_id, 0, 0, 0, 0, "cancel")
            d.net_credit_ops.authorization_from_receipt(self.proxy.address, plan, 0, receipt, expected_hash=tx_hash)
            if tuple(d.buck.functions.reductionAuthorization(token_id).call()) != (0, 0, 0):
                raise ValueError("repayment cancellation not confirmed")
        except TransactionReverted as exc:
            if exc.transaction_hash and exc.receipt.get("status") == 0:
                self._net_cancel_uncertain = False
            raise
        except Exception as exc:
            self._net_cancel_hash = getattr(exc, "transaction_hash", None) or None
            if not self._net_cancel_hash:
                try:
                    self._net_cancel_hash = _txhex(receipt.get("transactionHash")) or None
                except Exception:
                    pass
            raise
        self._clear_pending_repayment()
        self.last_error         = ""
        self.last_action        = "repay-canceled"

    def _clear_pending_repayment(self):
        self._net_pending_token = None
        self._net_pending_principal = 0
        self._net_pending_minimum = self._net_pending_deadline = None
        self._net_pending_hash = None
        self._net_pending_reverted = False
        self._net_pending_phase = None
        self._net_authorization_confirmed = False
        self._net_cancel_uncertain = False
        self._net_cancel_hash = None

    def reconcile_pending_cancellation(self, d):
        """Resolve a submitted cancellation without submitting it again."""
        tx_hash                 = getattr(self, "_net_cancel_hash", None)
        token_id                = getattr(self, "_net_pending_token", None)
        if not getattr(self, "_net_cancel_uncertain", False) or not tx_hash or token_id is None:
            raise ValueError("cancellation needs a known transaction hash for reconciliation")
        receipt                 = d.w3.eth.get_transaction_receipt(tx_hash)
        if _txhex(receipt.get("transactionHash")).lower() != tx_hash.lower():
            raise ValueError("cancellation transaction hash mismatch")
        if receipt.get("status") == 0:
            self._net_cancel_uncertain = False
            self._net_cancel_hash = None
            self.last_error     = ""
            self.last_action    = "cancel-reverted"
            return
        plan                    = ReductionPlan(token_id, 0, 0, 0, 0, "cancel")
        d.net_credit_ops.authorization_from_receipt(self.proxy.address, plan, 0, receipt, expected_hash=tx_hash)
        if tuple(d.buck.functions.reductionAuthorization(token_id).call()) != (0, 0, 0):
            raise ValueError("repayment cancellation changed; reconcile before proceeding")
        self._clear_pending_repayment()
        self.last_error         = ""
        self.last_action        = "repay-canceled"

    def reconcile_pending_buy(self, d):
        """Resolve a submitted guarded buy by its exact mined transaction receipt."""
        tx_hash                 = getattr(self, "_net_pending_buy_hash", None)
        if not getattr(self, "_net_trade_uncertain", False) or not tx_hash:
            raise ValueError("buyback needs a known transaction hash for reconciliation")
        receipt                 = d.w3.eth.get_transaction_receipt(tx_hash)
        if _txhex(receipt.get("transactionHash")).lower() != tx_hash.lower():
            raise ValueError("buyback transaction hash mismatch")
        if receipt.get("status") == 0:
            self._net_trade_uncertain = False
            self._net_pending_buy_hash = None
            self._net_repayment_reserve = None
            self.last_error     = ""
            self.last_action    = "buy-reverted"
            return 0
        purchase                = d.net_market_ops.purchase_from_receipt(
            self.proxy.address, d.pool_ub, self.proxy.address, receipt, expected_hash=tx_hash)
        self.trade_loss        += purchase.paid - purchase.received
        self._net_trade_uncertain = False
        self._net_pending_buy_hash = None
        if not purchase.received:
            self._net_repayment_reserve = None
        self.last_error         = ""
        self.last_action        = "buy-reconciled"
        return purchase.received

    def reconcile_pending_repayment(self, d, ctr):
        """Record an exact confirmed settlement, or mark its mined revert for cancellation."""
        token_id                = getattr(self, "_net_pending_token", None)
        principal               = getattr(self, "_net_pending_principal", 0)
        tx_hash                 = getattr(self, "_net_pending_hash", None)
        if token_id is None or not principal or not tx_hash:
            raise ValueError("repayment needs a known transaction hash for reconciliation")
        receipt                 = d.w3.eth.get_transaction_receipt(tx_hash)
        if _txhex(receipt.get("transactionHash")).lower() != tx_hash.lower():
            raise ValueError("repayment transaction hash mismatch")
        if getattr(self, "_net_pending_phase", "settlement") == "authorization":
            expected            = (principal, self._net_pending_minimum, self._net_pending_deadline)
            actual              = tuple(d.buck.functions.reductionAuthorization(token_id).call())
            if receipt.get("status") == 0:
                if actual[0]:
                    raise ValueError("authorization changed; reconcile before retrying")
                self._clear_pending_repayment()
                self.last_error = ""
                self.last_action = "authorize-reverted"
                return 0
            plan                = ReductionPlan(token_id, principal, 0, expected[1], 0, "ready")
            d.net_credit_ops.authorization_from_receipt(self.proxy.address, plan, expected[2], receipt,
                                                        expected_hash=tx_hash)
            if actual != expected:
                raise ValueError("authorization changed; reconcile before canceling")
            self._net_authorization_confirmed = True
            self.last_error     = ""
            self.last_action    = "authorize-reconciled"
            return 0
        if receipt.get("status") == 0:
            self._net_pending_reverted = True
            self.last_error     = ""
            self.last_action    = "repay-reverted"
            return 0
        settled                 = d.net_credit_ops.reduction_from_receipt(
            self.proxy.address, token_id, principal, receipt, expected_hash=tx_hash)
        self.unwound           += settled.principal
        ctr["bcdRetired"]       = ctr.get("bcdRetired", 0) + settled.principal
        if self.p["surplus_policy"] == "adaptive":
            ctr["hhPaidDown"]   = ctr.get("hhPaidDown", 0) + settled.principal
        self._clear_pending_repayment()
        self.last_error         = ""
        self.last_action        = "repay-reconciled"
        return settled.principal

    def _retire_acquired(self, d, target, acquired):
        """Legacy purchases repay implicitly; BuckNet settles selected NFTs explicitly."""
        if getattr(d, "buck_accounting", "") != "net":
            return acquired
        if target <= 0:
            return 0
        buffer                  = getattr(self, "_net_repayment_reserve", None)
        if buffer is None:
            if acquired <= 0:
                return 0
            held                = d.buck.functions.balanceOf(self.proxy.address).call()
            # Keep at least the full balance held before the first purchase;
            # this conservative floor also protects the issuance reserve.
            buffer              = max(0, held - acquired) + 1
            self._net_repayment_reserve = buffer
        deadline                = d.w3.eth.get_block("latest")["timestamp"] + 3600
        result                  = d.net_credit_ops.retire_household(
            self.proxy.address, self._token_ids, target, deadline, buffer=buffer)
        if result.status == "authorization-pending":
            self._net_pending_token = result.blocked_token_id
            self._net_pending_principal = result.pending_principal or 0
            self._net_pending_minimum = result.pending_minimum_relief_scaled
            self._net_pending_deadline = result.pending_deadline
            self._net_pending_hash = result.pending_transaction_hash
            self._net_pending_reverted = result.pending_reverted
            self._net_pending_phase = result.pending_phase
            self._net_authorization_confirmed = False
            # Do not let another RPC failure discard earlier successful
            # settlements or the pending transaction's evidence.
            self.last_error     = f"repayment {result.status} at facility {result.blocked_token_id}"
            return result.retired
        if result.retired >= target:
            self._net_repayment_reserve = None
        else:
            try:
                held            = d.buck.functions.balanceOf(self.proxy.address).call()
                if held <= buffer:
                    self._net_repayment_reserve = None
            except Exception as exc:
                # Preserve the reserve conservatively and report completed
                # settlements even if the follow-up balance is unavailable.
                self._net_repayment_interrupted = True
                self.last_error = f"repayment balance unavailable: {exc!r}"[:200]
        if result.status not in ("complete", "partial"):
            self._net_repayment_interrupted = True
            detail              = f": {result.error}" if result.error else ""
            self.last_error     = f"repayment {result.status} at facility {result.blocked_token_id}{detail}"[:200]
        return result.retired

    def _issue(self, d, state, kind, v, ctr):
        if getattr(d, "buck_accounting", "") == "net":
            raise NotImplementedError("BuckNet household issuance is not yet integrated")
        # Reassess every decision: only a currently worthwhile issuance takes priority
        # over discretionary saving. Cash fees stay in USDC; gate purchases stay in BUCKs.
        self.funding_pending    = v.go and v.buck >= 1
        if not v.go or v.buck < 1:
            return
        fixed                   = int((-v.parts["fixed"]) * PARITY)
        penalty                 = int((-v.parts["penalty"]) * PARITY)
        cash                    = d.chain.balance_of(d.usdc, self.proxy.address)
        if cash < self.cash_buffer + fixed + penalty:
            self.last_action    = "cash-wait"
            return
        want                    = int(v.buck * PARITY)
        k                       = d.kctrl.functions.buckK().call()
        cap                     = mint_capacity(int(state["unactivated"] * PARITY), k, self.premium_rate)
        spendable               = d.buck.functions.balanceOf(self.proxy.address).call()
        amount                  = min(max(0, want - spendable) * 1002 // 1000, cap)
        if amount >= PARITY:
            ff                  = self._gate_factor(d, 0)
            arg                 = mint_amount(amount, cap)
            _, principal        = d.buck.functions.quoteMint(arg, self._token_ids).call()
            required            = principal * ff // 10 ** 18 * 1005 // 1000
            short               = max(0, required - d.buck.functions.balanceOf(self.proxy.address).call())
            if short:
                ru, rb          = active_reserves(d.chain, d.pool_ub, d.usdc, d.buck)
                target          = min(rb * .5, short * 1.01 + PARITY)
                cost            = int(target * ru / ((rb - target) * (1 - d.fee_ub / 1e6))) + 1
                self._buy_cash(d, min(cost, max(0, cash - self.cash_buffer - fixed - penalty)))
            if d.buck.functions.balanceOf(self.proxy.address).call() < required:
                self.last_action = "gate-wait"
                self.throttled += 1
                return
            before              = d.buck.functions.signedBalanceOf(self.proxy.address).call()
            self._proxy_exec(d, d.buck.address, d.buck.encode_abi("mint(uint256)", args=[arg]))
            self.premium_paid  += max(0, before - d.buck.functions.signedBalanceOf(self.proxy.address).call())
        if kind == "invest" and self.p["surplus_policy"] in ("adaptive", "basket"):
            # Borrowed to save: the BUCKs go straight into the basket, never through dollars.
            if self._savings_proxy is None:
                self._savings_proxy = self._new_proxy(d)
            moved               = min(want, d.buck.functions.balanceOf(self.proxy.address).call())
            if moved < PARITY:
                return
            self._proxy_exec(d, d.buck.address, d.buck.encode_abi(
                "transfer", args=[self._savings_proxy.address, moved]))
            self._deposit_bucks(d, ctr, proxy=self._savings_proxy, prefix="bcd")
            self.funding_pending = False
            self.switch_paid   += self._pay_bank(d, fixed)
            self.joined         = True
            self.deploys       += 1
            self.issued_invest += moved
            self.last_action    = kind
            self.note(d, kind, moved=moved, **v.why())
            return
        before                  = d.chain.balance_of(d.usdc, self.proxy.address)
        signed                  = d.buck.functions.signedBalanceOf(self.proxy.address).call()
        self._sell_capped(d, d.pool_ub, want)
        sold                    = signed - d.buck.functions.signedBalanceOf(self.proxy.address).call()
        got                     = d.chain.balance_of(d.usdc, self.proxy.address) - before
        if sold <= 0:
            return
        self.funding_pending    = False
        self.trade_loss         += sold - got
        self.switch_paid        += self._pay_bank(d, fixed)
        first                   = not self.joined
        self.joined             = True
        self.deploys           += 1
        self.last_action        = kind
        ctr["rtrBuckSold"]      = ctr.get("rtrBuckSold", 0) + sold // PARITY
        if kind == "refinance":
            # A partially filled swap owes penalty only on principal actually retired.
            paid                = self._pay_bank(d, min(got, self.mortgage))
            self.mortgage      -= paid
            self.external_paid += paid
            due                 = 0 if state["at_renewal"] else int(paid * self.apr * self.p["penalty_months"] / 12)
            self.penalty_paid  += self._pay_bank(d, due)
            self.refis         += 1
            counter             = "rtrRefis" if first else "rtrTopUps"
            ctr[counter]        = ctr.get(counter, 0) + 1
            ctr["rtrUsdRetired"] = ctr.get("rtrUsdRetired", 0) + paid // PARITY
        else:
            self.issued_invest += sold
            self._invest_cash(d, got, ctr)
        self.note(d, kind, sold=sold, received=got, **v.why())

    def _invest_cash(self, d, budget, ctr):
        try:
            self._invest_cash_once(d, budget, ctr)
        except Exception as exc:
            # As in the JS adapter, an independently refused savings operation does not undo
            # completed refinancing or hide the TOKEN already acquired. Retry it next decision.
            self._refused(exc, ctr)

    def _refused(self, exc, ctr) -> bool:
        """The basket's price guard refusing is a refusal (True), retried smaller or later; else an error."""
        why                     = repr(exc)[:200]
        if any(s in why for s in SLIPPAGE):
            self.refusals      += 1
            ctr["household_refused"] = ctr.get("household_refused", 0) + 1
            return True
        self.last_error         = why
        ctr["household_err"]    = why
        return False

    def _book_receipts(self, d, receipt, ctr, prefix):
        """Record the receipts a deposit's logs name: principal, and shares on an equity basket."""
        for log in receipt["logs"]:
            if log["topics"][0] == d.deposited_topic:
                rid             = int.from_bytes(log["topics"][2], "big")
                principal       = d.basket.functions.deposits(rid).call()[0]
                shares          = (d.basket.functions.holdings(rid).call()[0]
                                   if str(d.basket_impl).startswith("equity") else 0)
                self._basket_receipts.append(rid)
                self._basket_in += principal
                self._basket_shares += shares
                self._receipt_book[rid] = (principal, shares)
                ctr[f"{prefix}Invested"] = ctr.get(f"{prefix}Invested", 0) + principal
                ctr[f"{prefix}Investments"] = ctr.get(f"{prefix}Investments", 0) + 1

    def _refresh_receipt(self, d, rid):
        """A successful redemption may leave shares. Book the remaining claim immediately, even if
        a later transfer fails; retrying must never redeem a destroyed receipt or forget a live one."""
        old_principal, old_shares = self._receipt_book[rid]
        if str(d.basket_impl).startswith("equity"):
            shares, principal   = d.basket.functions.holdings(rid).call()
        else:
            principal           = d.basket.functions.deposits(rid).call()[0]
            shares              = 0
        self._basket_in        += principal - old_principal
        self._basket_shares    += shares - old_shares
        if principal or shares:
            self._receipt_book[rid] = (principal, shares)
        else:
            self._receipt_book.pop(rid)
            self._basket_receipts.remove(rid)

    def _invest_cash_once(self, d, budget, ctr):
        if self._savings_proxy is None:
            self._savings_proxy = self._new_proxy(d)
        saver                   = self._savings_proxy
        i                       = self._token_idx
        token                   = d.tokens[i]
        held                    = d.chain.balance_of(token, saver.address)
        if held == 0 and budget > PARITY:
            cash                = d.chain.balance_of(d.usdc, self.proxy.address)
            ru, _               = active_reserves(d.chain, d.pool_usdc[i], d.usdc, token)
            spend               = min(budget, max(0, cash - self.cash_buffer), int(ru * self.max_impact_bp / 20_000))
            if spend > PARITY:
                self._swap_via_simlp(d, d.pool_usdc[i], d.usdc, spend, saver.address)
                self.invested_usd += cash - d.chain.balance_of(d.usdc, self.proxy.address)
                held            = d.chain.balance_of(token, saver.address)
        if not held:
            return
        self._proxy_exec(d, token.address, token.encode_abi("approve", args=[d.basket.address, held]), proxy=saver)
        receipt                 = self._proxy_exec(d, d.basket.address, d.basket.encode_abi(
            "depositToken", args=[token.address, held, 0]), proxy=saver)
        self._book_receipts(d, receipt, ctr, "bcd")

    # -- the investor ------------------------------------------------------------------------------- #

    def _investor_decision(self, d, day, ctr):
        """Hold BUCKs as money: buy them with the dollars saved, unless they are dear; then convert
        those held into basket shares. Once the horizon comes, withdraw everything."""
        if day >= self._exit_day:
            self.last_action    = "exit"
            self._withdraw(d, ctr)
            return
        state                   = self._state(d, day)
        action                  = policy.investor_action(state["ru"] / state["rb"], state["basket_usd"],
                                                         state["fee"], self.p["issue_premium"])
        if action == "buy":
            cash                = d.chain.balance_of(d.usdc, self.proxy.address)
            budget              = min(cash, int(state["ru"] * PARITY * self.max_impact_bp / 20_000))
            if budget > PARITY:
                self._buy_cash(d, budget)
                self.invested_usd += cash - d.chain.balance_of(d.usdc, self.proxy.address)
        else:
            self._deposit_bucks(d, ctr)
        self.last_action        = action

    def _deposit_bucks(self, d, ctr, proxy=None, prefix="inv"):
        """Deposit the BUCKs held (by the household's own account, or its savings account). When the
        basket's price guard refuses, offer half, up to four times; what is still refused stays."""
        proxy                   = proxy or self.proxy
        amount                  = d.buck.functions.balanceOf(proxy.address).call()
        for _ in range(4):
            if amount <= PARITY:
                return
            try:
                self._proxy_exec(d, d.buck.address, d.buck.encode_abi("approve", args=[d.basket.address, amount]),
                                 proxy=proxy)
                receipt         = self._proxy_exec(d, d.basket.address, d.basket.encode_abi(
                    "depositToken", args=[d.buck.address, amount, 0]), proxy=proxy)
            except Exception as exc:
                if not self._refused(exc, ctr):
                    return
                amount         //= 2
                continue
            self._book_receipts(d, receipt, ctr, prefix)
            return

    def _withdraw(self, d, ctr):
        """Redeem every receipt, then sell what comes back for dollars, a capped bite at a time; then stop."""
        for rid in list(self._basket_receipts):
            try:
                self._proxy_exec(d, d.basket.address, d.basket.encode_abi(
                    "redeem(uint256,uint256,uint256)", args=[rid, 0, 0]))
            except Exception as exc:
                self._refused(exc, ctr)
                return
            self._refresh_receipt(d, rid)
            ctr["invRedeemed"]  = ctr.get("invRedeemed", 0) + 1
        before                  = d.chain.balance_of(d.usdc, self.proxy.address)
        left                    = False
        held                    = d.buck.functions.balanceOf(self.proxy.address).call()
        if held > PARITY:
            # Leave half a BUCK: demurrage accrues between the read and the swap.
            _, rb               = active_reserves(d.chain, d.pool_ub, d.usdc, d.buck)
            bite                = min(held - PARITY // 2, max(PARITY, _impact_cap(rb, self.max_impact_bp)))
            self._swap_via_simlp(d, d.pool_ub, d.buck, bite, self.proxy.address)
            left                = held - bite > PARITY
        for i, token in enumerate(d.tokens):
            amount              = d.chain.balance_of(token, self.proxy.address)
            if amount:
                _, rt           = active_reserves(d.chain, d.pool_usdc[i], d.usdc, token)
                bite            = min(amount, max(1, _impact_cap(rt, self.max_impact_bp)))
                self._swap_via_simlp(d, d.pool_usdc[i], token, bite, self.proxy.address)
                left           |= bite < amount
        self.withdrawn_usd     += d.chain.balance_of(d.usdc, self.proxy.address) - before
        if not left and not self._basket_receipts:
            self.exited         = True
            ctr["invExits"]     = ctr.get("invExits", 0) + 1

    def _surplus(self, d, state, ctr):
        if self.funding_pending:
            self.last_action    = "fund-issue"
            return
        p                       = self.p
        cash                    = d.chain.balance_of(d.usdc, self.proxy.address)
        spare                   = max(0, cash - self.cash_buffer)
        if p["surplus_policy"] in ("adaptive", "basket"):
            self._save_or_pay_down(d, state, spare, ctr)
            return
        action                  = policy.surplus_action(p["surplus_policy"], state["ru"] / state["rb"],
                                                       state["basket_usd"], state["fee"], p["retire_disc"],
                                                       p["issue_premium"])
        if action == "invest":
            self._invest_cash(d, spare, ctr)
        elif action == "retire" and state["drawn"] > 1:
            if self._pending_repayment(d):
                return
            ru, rb, fee          = state["ru"], state["rb"], state["fee"]
            budget              = min(spare / PARITY, ru * self.max_impact_bp / 20_000)
            want                = (min(state["drawn"], carry.usdc_out(budget, rb, ru, fee))
                                   if p["surplus_policy"] == "retire" else carry.discounted_buyback(
                                       budget, state["drawn"], ru, rb, fee, state["basket_usd"], p["retire_disc"]))
            if getattr(d, "buck_accounting", "") == "net" and getattr(self, "_net_repayment_reserve", None) is not None:
                retired         = self._retire_acquired(d, int(state["drawn"] * PARITY), 0)
                self.unwound   += retired
                ctr["bcdRetired"] = ctr.get("bcdRetired", 0) + retired
                want            = max(0., want - retired / PARITY)
                if self._pending_repayment(d):
                    return
                if self._net_repayment_reserve is not None:
                    self.last_action = "pay-down-wait"
                    return
            if want > 1:
                cost            = int(want * ru / ((rb - want) * (1 - fee)) * PARITY)
                spend           = min(int(budget * PARITY), cost)
                ceiling         = state["basket_usd"] * (1 - p["retire_disc"])
                min_net         = (math.floor(spend / ceiling) + 1 if p["surplus_policy"] == "discount"
                                   and getattr(d, "buck_accounting", "") == "net" and ceiling > 0 else 0)
                got             = self._buy_cash(d, spend, min_net, for_repayment=True)
                retired         = self._retire_acquired(d, int(want * PARITY), got)
                self.unwound   += retired
                ctr["bcdRetired"] = ctr.get("bcdRetired", 0) + retired
                if self._pending_repayment(d):
                    return
            self._redeem_to_retire(d, ctr)
        self.last_action        = action

    def _divest(self, d, ctr):
        """Sell the insured asset. Its price pays any mortgage left; the basket shares are redeemed and
        their BUCKs repay the lien; the rest of the lien is bought back, a capped bite a day; then the
        coverage is released (its deposit refunded), the BUCKs left are sold, and the household leaves.
        The counterfactual sells the same asset on the same day and pays its mortgage."""
        if getattr(d, "buck_accounting", "") == "net":
            raise NotImplementedError("BuckNet household asset exits are not yet integrated")
        self.funding_pending    = False
        if not self._sold:
            self.exit_stage     = "sell-asset"
            d.chain.send(d.usdc.functions.mint(self.proxy.address, self._face0))
            self._sold          = True
            self.hypo_cash     += self._face0
            paid                = min(self.hypo_mortgage, self.hypo_cash)
            self.hypo_cash     -= paid
            self.hypo_mortgage -= paid
            self.hypo_paid     += paid
            ctr["hhDivestments"] = ctr.get("hhDivestments", 0) + 1
        self.exit_stage         = "settle-mortgage"
        paid                    = self._pay_bank(d, self.mortgage)
        self.mortgage          -= paid
        self.external_paid     += paid
        self.exit_stage         = "collect-savings"
        if self._wallet is not None:                    # retry collection if an earlier transfer failed
            held                = d.buck.functions.balanceOf(self._wallet.address).call()
            if held > PARITY:
                self._proxy_exec(d, d.buck.address, d.buck.encode_abi(
                    "transfer", args=[self.proxy.address, held - PARITY // 2]), proxy=self._wallet)
        if not self._redeem_all(d, ctr):
            return                                      # keep a refused or partial claim for tomorrow
        self.exit_stage         = "repay-lien"
        drawn                   = self._drawn(d)
        cash                    = d.chain.balance_of(d.usdc, self.proxy.address)
        if self.mortgage or drawn > 0:
            if cash <= 0:
                if not self.exit_short:
                    ctr["hhDivestShort"] = ctr.get("hhDivestShort", 0) + 1
                self.exit_short = True                  # insolvency is not a completed exit
                self.exit_stage = "cash-shortfall"
                return
            ru, rb              = active_reserves(d.chain, d.pool_ub, d.usdc, d.buck)
            want                = min(drawn + max(PARITY // 100, drawn // 100), rb // 2)
            cost                = int(want * ru / ((rb - want) * (1 - d.fee_ub / 1e6))) + 1
            got                 = self._buy_cash(d, min(cost, _impact_cap(ru, self.max_impact_bp)))
            self.unwound       += got
            ctr["hhDivestBought"] = ctr.get("hhDivestBought", 0) + got
            return
        if not self._released:
            self.exit_stage     = "release-coverage"
            if not self._release_coverage(d):
                return
        self.exit_stage         = "sell-owned-buck"
        # balanceOf includes the credit line. Only signed positive BUCKs belong to the seller.
        held                    = d.buck.functions.signedBalanceOf(self.proxy.address).call()
        if held < 0:
            return                                      # settle any aged refund on the next pass
        if held > PARITY:
            _, rb               = active_reserves(d.chain, d.pool_ub, d.usdc, d.buck)
            bite                = min(held - PARITY // 2, max(PARITY, _impact_cap(rb, self.max_impact_bp)))
            self._swap_via_simlp(d, d.pool_ub, d.buck, bite, self.proxy.address)
            if held - bite > PARITY:
                return
        self.exited             = True
        self.exit_stage         = "complete"
        ctr["hhDivested"]       = ctr.get("hhDivested", 0) + 1

    def _release_coverage(self, d):
        """Quote cancellable cover at the current K, with exact per-NFT rounding.
        A changed quote is retried twice, then on the next day. Never sell from a line being closed."""
        for _ in range(2):
            d.chain.send(d.kctrl.functions.compute())
            k                   = d.kctrl.functions.buckK().call()
            slices              = d.credit.functions.batchCreditInfo(self._token_ids).call()
            amount              = 0
            for tid, (_, face, dep, active, rate) in zip(self._token_ids, slices):
                used            = min(active, d.buck.functions.mintsBacked(tid).call())
                deposit         = d.buck.functions.mintsPrincipal(tid).call()
                if used and dep and rate * 10 < 10_000:
                    amount     += max(0, used * dep // face * k // 10**18 - deposit)
            if not amount:
                break
            try:
                d.buck.functions.quoteBurn(amount, self._token_ids).call()
                self._proxy_exec(d, d.buck.address, d.buck.encode_abi(
                    "burn(uint256,uint256[])", args=[amount, self._token_ids]))
            except Exception as exc:
                if "BUCK: insufficient coverage to unwind" not in str(exc):
                    raise
                self.exit_retries += 1
                continue
            break
        self._released          = all(d.credit.functions.creditInfo(t).call()[1] == 0
                                      and d.buck.functions.mintsBacked(t).call() == 0
                                      and d.buck.functions.mintsPrincipal(t).call() == 0 for t in self._token_ids)
        return self._released

    def _redeem_all(self, d, ctr) -> bool:
        """Redeem every savings receipt and move what it pays (and any TOKEN waiting) to the borrower's
        account. False while a claim remains or a capped TOKEN sale needs another day."""
        saver                   = self._savings_proxy
        if saver is None:
            return True
        for rid in list(self._basket_receipts):
            try:
                self._proxy_exec(d, d.basket.address, d.basket.encode_abi(
                    "redeem(uint256,uint256,uint256)", args=[rid, 0, 0]), proxy=saver)
            except Exception as exc:
                self._refused(exc, ctr)
                break
            self._refresh_receipt(d, rid)
        held                    = d.buck.functions.balanceOf(saver.address).call()
        if held > PARITY:
            # Like the final sale, leave half a BUCK for demurrage between quotation and mining.
            self._proxy_exec(d, d.buck.address, d.buck.encode_abi(
                "transfer", args=[self.proxy.address, held - PARITY // 2]),
                             proxy=saver)
        pending                 = False
        for i, token in enumerate(d.tokens):
            amount              = d.chain.balance_of(token, saver.address)
            if amount:
                ru, rt          = active_reserves(d.chain, d.pool_usdc[i], d.usdc, token)
                if amount * ru // rt <= PARITY:
                    continue                            # at most one dollar's TOKEN dust per constituent
                bite            = min(amount, max(1, _impact_cap(rt, self.max_impact_bp)))
                self._swap_via_simlp(d, d.pool_usdc[i], token, bite, self.proxy.address, from_proxy=saver)
                pending        |= bite < amount
        return not self._basket_receipts and not pending

    def _redeem_to_retire(self, d, ctr, rid=None):
        """The other half of borrowing to invest: while the BUCK is cheap, redeem a basket receipt (an
        equity basket pays its exits in BUCKs) and repay the lien with what comes back. One receipt a
        decision, oldest first; what exceeds the lien stays in savings. Returns the BUCKs repaid, or
        None when there was nothing to redeem or the basket's guard refused."""
        if (not self._basket_receipts or self._savings_proxy is None
                or not str(d.basket_impl).startswith("equity")):
            return None
        if getattr(d, "buck_accounting", "") == "net":
            raise NotImplementedError("BuckNet household basket redemption is not yet integrated")
        if self._pending_repayment(d):
            return None
        drawn                   = self._drawn(d)
        if drawn <= PARITY:
            return None
        saver                   = self._savings_proxy
        rid                     = self._basket_receipts[0] if rid is None else rid
        before                  = d.buck.functions.balanceOf(saver.address).call()
        try:
            self._proxy_exec(d, d.basket.address, d.basket.encode_abi(
                "redeem(uint256,uint256,uint256)", args=[rid, 0, 0]), proxy=saver)
        except Exception as exc:
            self._refused(exc, ctr)
            return None
        self._refresh_receipt(d, rid)
        received                = d.buck.functions.balanceOf(saver.address).call() - before
        available               = max(0, d.buck.functions.balanceOf(saver.address).call() - PARITY // 2)
        repay                   = min(max(0, received), drawn, available)
        if repay > 0:
            self._proxy_exec(d, d.buck.address, d.buck.encode_abi("transfer", args=[self.proxy.address, repay]),
                             proxy=saver)
        retired                 = self._retire_acquired(d, repay, repay)
        self.unwound           += retired
        ctr["bcdRetired"]       = ctr.get("bcdRetired", 0) + retired
        ctr["bcdRedeemedToRetire"] = ctr.get("bcdRedeemedToRetire", 0) + 1
        return retired

    # -- saving in BUCK deposits, and paying down at a discount ----------------------------------------- #

    def _save_or_pay_down(self, d, state, spare, ctr):
        """Adaptive saving, or the always-invest comparison. At a discount, adaptive pays down with an urgency
        that compounds with the discount (household_policy.paydown): basket shares first, then spare
        dollars. Otherwise save: buy BUCKs with a share of the spare dollars that halves with the
        premium (household_policy.saving), and deposit them in the basket."""
        if self._pending_repayment(d):
            return
        adaptive                = self.p["surplus_policy"] == "adaptive"
        share                   = (policy.paydown(policy.discount(state["ru"] / state["rb"], state["basket_usd"],
                                                                 state["fee"]), self.p) if adaptive else 0.)
        drawn                   = self._drawn(d)
        if share > 0 and drawn > PARITY:
            target              = int(drawn * share)
            repaid              = 0
            if getattr(d, "buck_accounting", "") == "net" and getattr(self, "_net_repayment_reserve", None) is not None:
                retired         = self._retire_acquired(d, target, 0)
                self.unwound   += retired
                repaid         += retired
                ctr["bcdRetired"] = ctr.get("bcdRetired", 0) + retired
                if self._pending_repayment(d):
                    ctr["hhPaidDown"] = ctr.get("hhPaidDown", 0) + repaid
                    return
                if self._net_repayment_reserve is not None:
                    ctr["hhPaidDown"] = ctr.get("hhPaidDown", 0) + repaid
                    self.last_action = "pay-down-wait"
                    return
            # Only receipts that fit within what is left to pay down, smallest first: a small urgency
            # must not cash out a whole savings position.
            for rid in sorted(self._basket_receipts, key=lambda r: self._receipt_book.get(r, (0, 0))[0]):
                if repaid >= target or self._receipt_book.get(rid, (0, 0))[0] > target - repaid:
                    break
                got             = self._redeem_to_retire(d, ctr, rid)
                if got is None:
                    break
                repaid         += got
            if repaid < target and spare > PARITY:
                ru, rb          = active_reserves(d.chain, d.pool_ub, d.usdc, d.buck)
                want            = min(target - repaid, rb // 2)
                cost            = int(want * ru / ((rb - want) * (1 - d.fee_ub / 1e6))) + 1
                spend           = min(spare, cost, _impact_cap(ru, self.max_impact_bp))
                min_net         = (math.floor(spend / state["basket_usd"]) + 1
                                   if getattr(d, "buck_accounting", "") == "net" and state["basket_usd"] > 0 else 0)
                got             = self._buy_cash(d, spend, min_net, for_repayment=True)
                retired         = self._retire_acquired(d, target - repaid, got)
                self.unwound   += retired
                repaid         += retired
                ctr["bcdRetired"] = ctr.get("bcdRetired", 0) + retired
                if self._pending_repayment(d):
                    ctr["hhPaidDown"] = ctr.get("hhPaidDown", 0) + repaid
                    return
            ctr["hhPaidDown"]   = ctr.get("hhPaidDown", 0) + repaid
            self.last_action    = "pay-down"
            return
        budget                  = (int(spare * policy.saving(policy.premium(state["ru"] / state["rb"],
                                                    state["basket_usd"], state["fee"]), self.p)) if adaptive else spare)
        if budget > PARITY:
            if self._savings_proxy is None:
                self._savings_proxy = self._new_proxy(d)
            ru, rb              = active_reserves(d.chain, d.pool_ub, d.usdc, d.buck)
            net                 = min(budget, _impact_cap(ru, self.max_impact_bp)) * (1 - d.fee_ub / 1e6)
            before              = d.chain.balance_of(d.usdc, self.proxy.address)
            got                 = self._buy_buck(d, d.pool_ub, d.usdc, int(rb * net / (ru + net)), d.fee_ub,
                                                 recipient=self._savings_proxy.address)
            spent               = before - d.chain.balance_of(d.usdc, self.proxy.address)
            self.invested_usd  += spent
            self.trade_loss    += spent - got
        if self._savings_proxy is not None:
            self._deposit_bucks(d, ctr, proxy=self._savings_proxy, prefix="bcd")
        self.last_action        = "save"

    def act(self, d, scenario, day, tick, ctr):
        if tick or day < self.arrive_day or self.exited:
            return
        if self._recover_pending_operations(d, ctr):
            return
        if not self._started:
            cash                = int(self.p["invest_cash_k" if self.investor else "cash_k"] * 1000 * PARITY)
            d.chain.send(d.usdc.functions.mint(self.proxy.address, cash))
            self.hypo_cash      = cash
            self._started      = self._growth_arrived = True
        self._advance(d, day)
        if self._divest_day is not None and day >= self._divest_day:
            # Selling the asset is worked every day until the lien is cleared and the household leaves.
            self.last_error     = ""
            self.last_action    = "divest"
            if self.exit_started is None:
                self.exit_started = day
            try:
                self._divest(d, ctr)
            except Exception as exc:
                self.last_error = f"{self.exit_stage}: {exc!r}"[:200]
                ctr["household_err"] = self.last_error
            return
        if day < self._next_decision:
            return
        period                  = self.p["decision_days"]
        self._next_decision    += ((day - self._next_decision) // period + 1) * period
        self.last_error         = ""
        if self.investor:
            try:
                self._investor_decision(d, day, ctr)
            except Exception as exc:
                self.last_error = repr(exc)[:200]
                ctr["household_err"] = self.last_error
            return
        try:
            state               = self._state(d, day)
            kind, verdict       = policy.issuance(state, self.p)
            self._last_verdict  = verdict.why()
            self.last_action    = kind if verdict.go else "declined"
            self._issue(d, state, kind, verdict, ctr)
            self.last_issue     = self.last_action
            self._surplus(d, self._state(d, day), ctr)
            self.last_surplus   = self.last_action
            # Retry pending TOKEN deposits independently of today's parity choice.
            if self._savings_proxy is not None:
                self._invest_cash(d, 0, ctr)
        except Exception as exc:
            self.last_error     = repr(exc)[:200]
            ctr["household_err"] = self.last_error

    def telemetry_static(self):
        return self.p | dict(member=self.member, arrive_day=self.arrive_day, face=self._face0,
                             mortgage0=self._mortgage0)

    def octl_state(self, d):
        if getattr(d, "buck_accounting", "") == "net":
            raise NotImplementedError("BuckNet household financial marks are not yet integrated")
        st                      = super().octl_state(d)
        if st is None:
            return None
        spot                    = buck_usd6(d.chain, d.pool_ub, d.buck) / PARITY
        pending                 = 0
        if self._savings_proxy is not None:
            token               = d.tokens[self._token_idx]
            held                = d.chain.balance_of(token, self._savings_proxy.address)
            ru, rt              = active_reserves(d.chain, d.pool_usdc[self._token_idx], d.usdc, token)
            pending             = int(held * ru / rt) if rt else 0
        basket                  = st["basket_mark"] if st["basket_mark"] is not None else st["basket"]
        insurance_age           = 0
        if st["deposit"]:
            raw                 = d.buck.functions.rawBalanceOf(d.pool_acct).call()
            fee                 = d.buck.functions.feeOwing(d.pool_acct).call()
            insurance_age       = min(st["deposit"], fee * st["deposit"] // raw) if raw else st["deposit"]
        held                    = max(0, d.buck.functions.signedBalanceOf(self.proxy.address).call())
        wallet                  = (d.buck.functions.balanceOf(self._wallet.address).call()
                                   if self._wallet is not None else 0)
        financial               = st["cash"] + pending - self.mortgage + int(
            (basket + st["deposit"] - insurance_age - st["drawn"] + st["jub"] + st["savings_held"] + held + wallet)
            * spot)
        arrears                 = self.living_arrears + self.premium_arrears
        hypo_arrears            = self.hypo_living_arrears + self.hypo_premium_arrears
        st.update(nw=financial - arrears, hypo=self.hypo_cash - self.hypo_mortgage - hypo_arrears,
                  hypo_cash=self.hypo_cash, hypo_mortgage=self.hypo_mortgage, arrears=arrears,
                  hypo_arrears=hypo_arrears, external_paid=self.external_paid, hypo_paid=self.hypo_paid,
                  insurance_age=insurance_age, exited=self.exited, withdrawn_usd=self.withdrawn_usd, held_buck=held,
                  wallet_buck=wallet, wallet_bought=self.wallet_bought,
                  refusals=self.refusals,
                  active=self._started, role=self.p["role"], member=self.member, action=self.last_action,
                  issue_action=self.last_issue, surplus_action=self.last_surplus,
                  funding_pending=self.funding_pending,
                  pending_usd=pending, invested_usd=self.invested_usd, issued_invest=self.issued_invest,
                  external_interest=self.external_interest, hypo_interest=self.hypo_interest,
                  income_total=self.income_total, living_paid=self.living_paid, hypo_living=self.hypo_living,
                  decision=self._last_verdict, error=self.last_error, arrive_day=self.arrive_day)
        st.update(self.exit_state())
        return st

    def exit_state(self):
        """Cheap live lifecycle fields: unlike account marks, these must not be five days stale."""
        return dict(exit_started=self.exit_started, exit_stage=self.exit_stage, exit_retries=self.exit_retries,
                    exit_short=self.exit_short, released=self._released, exited=self.exited,
                    action=self.last_action, error=self.last_error)

    def basket_position(self, share_price):
        """Open receipt basis and TWAP share mark, in BUCK; this is not a realized return."""
        basis                   = self._basket_in
        mark                    = (self._basket_shares * share_price // 10**18
                                   if self._basket_shares and share_price is not None else
                                   0 if not basis else None)
        return dict(basket=basis, basket_at_risk=basis, basket_shares=self._basket_shares,
                    basket_receipts=len(self._basket_receipts), basket_mark=mark,
                    basket_roi=(mark - basis) / basis if basis and mark is not None else None)


@_register
class CreditSaverAgent(CreditHouseholdAgent):
    def setup(self, d, scenario, rng):
        self.settings           = getattr(self, "settings", {}) | {"role": "saver"}
        super().setup(d, scenario, rng)


@_register
class HouseholdInvestorAgent(CreditHouseholdAgent):
    def setup(self, d, scenario, rng):
        self.settings           = getattr(self, "settings", {}) | {"role": "investor"}
        super().setup(d, scenario, rng)


ROLE_CLASSES                    = {"debtor": CreditHouseholdAgent, "saver": CreditSaverAgent,
                                   "investor": HouseholdInvestorAgent}


@_register
class HouseholdPopulationAgent(Agent):
    """A population whose households arrive while the world runs.

    It stays in the cast. Each morning (day_start, called by the loop before any agent acts) it reads
    the basket's price in BUCKs, advances each role's arrival intensity (household_policy.arrivals_step)
    and adds the households that arrive to the world's own lists, where they act like any other agent.
    Members are named <population>/<role>/<k>; their draws are keyed by that name alone.
    """
    is_eoa                      = False

    def setup(self, d, scenario, rng):
        cls                     = type(self).__name__
        self.population         = str(spec(scenario, cls, "population", 0))
        self.pop                = policy.population({k: spec(scenario, cls, k, None)
                                                     for k in (*policy.POPULATION, *policy.LEGACY_POPULATION)})
        self.settings           = {k: spec(scenario, cls, k, v) for k, v in policy.DEFAULTS.items()}
        self.state              = policy.arrival_state(scenario.seed, self.population)
        self.members            = []
        self._opened            = False
        self._restore           = []                    # (day, member, income) a temporary income shock ends

    def disturb(self, iv, day):
        """The set_population intervention: rates, gain, external rates, incomes and investors' exits."""
        notes                   = []
        scale                   = float(iv.get("rate_scale", 1.))
        for role in policy.ROLES:
            key                 = f"{role}_rate"
            if key in iv or scale != 1.:
                self.pop[key]   = float(iv.get(key, self.pop[key])) * scale
                notes.append(f"{key} {self.pop[key]:g}")
        if "gain" in iv:
            self.pop["gain"]    = float(iv["gain"])
            notes.append(f"gain {self.pop['gain']:g}")
        if "apr_shift" in iv:
            shift               = float(iv["apr_shift"])
            apr                 = self.settings["apr"]
            self.settings["apr"] = [a + shift for a in apr] if isinstance(apr, (list, tuple)) else apr + shift
            for a in self.members:
                if not a.investor:
                    a.apr       = a.p["apr"] = max(0., a.apr + shift)
            notes.append(f"external rates {shift:+.2%}")
        if "income_scale" in iv:
            factor, days        = float(iv["income_scale"]), iv.get("days")
            for a in self.members:
                if days is not None:
                    self._restore.append((day + int(days), a, a.income_annual))
                a.income_annual = int(a.income_annual * factor)
            notes.append(f"incomes x{factor:g}" + (f" for {int(days)} days" if days is not None else ""))
        if "exit_now" in iv:
            present             = [a for a in self.members if a.investor and a._started and not a.exited]
            leaving             = present[:round(float(iv["exit_now"]) * len(present))]
            for a in leaving:
                a._exit_day     = day
            notes.append(f"{len(leaving)} of {len(present)} investors withdraw")
        return ", ".join(notes) or "no change"

    def act(self, d, scenario, day, tick, ctr):
        pass                    # members act for themselves; arrivals are decided in day_start

    def day_start(self, d, scenario, day, ctr, agents, arbs, rng):
        for end, member, income in [r for r in self._restore if r[0] <= day]:
            member.income_annual = income
        self._restore           = [r for r in self._restore if r[0] > day]
        if not self._opened:
            self._opened        = True
            for role in policy.ROLES:
                for k in range(self.pop[f"initial_{role}s"]):
                    self._arrive(d, scenario, rng, agents, arbs, role, k, day)
        bvib                    = None
        if day >= self.pop["settle_days"]:
            try:
                bvib            = d.basket.functions.basketValueInBuck().call() / 1e18
            except Exception:
                bvib            = None  # an unreadable price leaves the average where it was
        try:
            k_now               = d.kctrl.functions.buckK().call() / 1e18
        except Exception:
            k_now               = None  # an unreadable K leaves the issuers' pace at its reference
        for role, k in policy.arrivals_step(self.state, scenario.seed, self.population, day, scenario.days,
                                            bvib, k_now, self.pop):
            self._arrive(d, scenario, rng, agents, arbs, role, self.pop[f"initial_{role}s"] + k, day)
        ctr["pop"]              = self.summary()

    def _arrive(self, d, scenario, rng, agents, arbs, role, k, day):
        if len(self.members) >= self.pop["max_members"]:
            raise ValueError("household arrivals exceed max_members; raise the bound or lower the rates")
        agent                   = ROLE_CLASSES[role](max(a.idx for a in agents) + 1)
        agent.settings          = self.settings | {"role": role}
        agent.member            = f"{self.population}/{role}/{k}"
        agent.arrive_day        = day
        agent.setup(d, scenario, rng)
        agents.append(agent)
        arbs.append(agent)
        self.members.append(agent)

    def summary(self):
        s                       = self.state
        out                     = dict(average=s["average"], gap=s["gap"], k_factor=s["k_factor"],
                                       multiplier=dict(s["multiplier"]))
        for role in policy.ROLES:
            mine                = [a for a in self.members if a.p["role"] == role]
            out[role]           = dict(arrived=len(mine), active=sum(a._started and not a.exited for a in mine),
                                       exited=sum(a.exited for a in mine))
        return out
