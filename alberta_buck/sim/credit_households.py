"""Households, insured savers and investors with portable decisions and separate, real contract accounts."""

from dataclasses import replace
import math

from alberta_buck.sim import carry, household_policy as policy
from alberta_buck.sim.agents import Agent, _register
from alberta_buck.sim.equilibrium_agents import _HouseholdAccounts, PARITY, _impact_cap, mint_amount, mint_capacity
from alberta_buck.sim.experiment import spec
from alberta_buck.sim.gauge import active_reserves, buck_usd6
from alberta_buck.sim.net_household import (ExitPlan, HouseholdRefinance, SurplusPlan, WalletPlan, _basket_ops, _exportable,
                                            assess_investment, assess_investor, assess_refinance, assess_remainder,
                                            assess_surplus, assess_wallet, observe_refinance_forecasts)
from alberta_buck.sim.household_calendar import HouseholdCalendar
from buck_core import ReductionPlan, TransactionReceiptPending, TransactionReverted, observe_net_market
from buck_core.session import _txhex, custom_error_name

# The basket's price guard, by name or (as a bare revert reports it) by selector: keccak("Slippage()").
SLIPPAGE                        = ("Slippage", "0x7dd37f70")


@_register
class CreditHouseholdAgent(_HouseholdAccounts):
    """One household with its own decisions and shared contract/accounting operations."""
    OCTL_STRIDE                 = 5                     # its books are read every fifth day (and the last)

    def setup(self, d, scenario, rng):
        cls                     = type(self).__name__
        overrides               = {k: spec(scenario, cls, k, v) for k, v in policy.DEFAULTS.items()}
        overrides.update(getattr(self, "settings", {}))
        self.member             = getattr(self, "member", str(self.idx))
        self.p                  = policy.profile(scenario.seed, self.member, overrides)
        p                       = self.p
        self.arrive_day         = getattr(self, "arrive_day", int(spec(scenario, cls, "arrive_day", 0)))
        self._last_day          = self.arrive_day
        self._next_pay          = policy.first_payday(self.arrive_day, p["pay_day"])
        self._next_decision     = self.arrive_day + p["phase"]
        self._net_refinance_day = None                  # at most one fresh assessment/resumption per bank day
        self._net_surplus_day   = None
        self._net_invest_day    = None                  # at most one credit-investment assessment per day
        self._net_credit_reserve = 0                    # lasting BUCK issuance front; outlives each operation
        self._net_funding       = None                  # (block, day, forecasts) -> the credit dollar front read there
        self._trading_ready     = False                 # the main account's market approvals, made on first act
        self._next_wallet       = self._next_pay        # working money is tended once after each payday
        self._wallet_ready      = False
        self.wallet_sold        = 0
        self._next_surplus      = self._next_decision
        self._net_dispatching   = False
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
        # The lifecycle (household_policy.stage): an investor of an age works and saves until retire_age,
        # draws its savings down as income, and leaves when its drawdown ends, not at a horizon.
        self.lifecycle          = p["age"] > 0
        if self.lifecycle and self.investor:
            years_left          = p["retire_age"] + p["drawdown_years"] - p["age"]
            self._exit_day      = self.arrive_day + max(0, int(policy.YEAR_DAYS * years_left))
        self.usd_fund           = 0                     # the external USD fund (off chain), raw dollars
        self.usd_fund_paid      = 0                     # what it has paid out as retirement income
        self._next_drawdown     = None                  # the next day a retired investor draws its month
        self._drawdown_to_sell  = 0                     # BUCK a month's redemption paid, still to sell
        self._divest_day        = (None if self.investor
                                   else policy.divest_day(scenario.seed, self.member, self.arrive_day, p["divest_rate"]))
        self._wallet            = None                  # the BUCK working balance, apart from the lien
        self._net_repayment_reserve = None              # pre-buyback balance while acquired BUCK waits
        self._net_repayment_target = None               # remaining principal of the original buyback
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
        self.refinance_machine  = HouseholdRefinance(self)
        self.calendar_machine   = HouseholdCalendar(self)
        self.wallet_bought      = 0
        self._sold              = False                 # the insured asset has been sold
        self._released          = False                 # its coverage has been released
        self.exit_started       = None
        self.exit_stage         = ""
        self.exit_retries       = 0
        self.exit_short         = False
        self.exited             = False
        self._exit_wait_day     = None                  # the day a refused or empty exit step last waited
        self._exit_trade_day    = None                  # the day of the last capped market bite: one a day
        self.released_coverage  = 0                     # face coverage burn(0) released at exit
        self.released_refund    = 0                     # and the premium deposit it refunded
        self.withdrawn_usd      = 0
        self.refusals           = 0
        self._receipt_book      = {}                    # receipt -> (principal, shares), taken off at exit
        self.max_impact_bp      = p["max_impact_bp"]
        self.theta              = p["theta"]
        self.pattern            = "salary"
        self.surplus_policy     = p["surplus_policy"]
        self.joined             = False
        self.refinance_closed   = False                 # confirmed bank closing, separate from insurance activation
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
        self.last_wallet        = "waiting"
        self.last_error         = ""
        self._token_idx         = int(policy.uniform(scenario.seed, self.member, "token") * len(d.tokens))
        self._bind_proxy(d)
        if getattr(d, "buck_accounting", "") == "net":
            self._proxy_exec(d, d.buck.address, d.buck.encode_abi("approve", args=[d.basket.address, 2**256 - 1]))
        if self.investor:
            self._token_ids     = []
            return
        self._proxy_exec(d, d.credit.address, d.credit.encode_abi(
            "setCreditIssuer", args=[getattr(d.chain.deployer, "address", d.chain.deployer), True]))
        now                     = d.w3.eth.get_block("latest")["timestamp"]
        # One facility per insured asset. An explicit asset portfolio shares
        # this household's aggregate collateral; a single home needs one NFT.
        count                   = p["asset_count"]
        per                     = self._face0 // count
        if not per:
            raise ValueError("asset_count exceeds raw insured value")
        for face in [per] * (count - 1) + [self._face0 - (count - 1) * per]:
            d.chain.send(d.credit.functions.createCredit(self.proxy.address, 0, face, 0, 0, 0, now, self.premium_rate))
        self._token_ids         = [d.credit.functions.tokenOfOwnerByIndex(self.proxy.address, i).call() for i in range(count)]

    def bootstrap(self, d, scenario, ctr):
        pass  # arrivals use the live market; they are not an opening OTC placement

    def _external_premium_due(self, d, full_premium):
        if getattr(d, "buck_accounting", "") != "net":
            return 0 if self.joined else full_premium  # labelled legacy comparison
        block = d.w3.eth.block_number
        activated = sum(d.credit.functions.creditInfo(t).call(block_identifier=block)[1] for t in self._token_ids)
        return policy.external_premium_due(full_premium, self._face0, activated)

    def _monthly(self, d):
        premium                 = 0 if self._sold else self._face0 * self.premium_rate // 10_000 // 12
        actual_premium          = self._external_premium_due(d, premium)
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
        self.premium_arrears   += actual_premium
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
            state["closing_paid"] = self.refinance_closed
            state["activated_capacity"] = available.capacity / PARITY
            state["issuance_limit"] = (0 if available.maturities_pending else available.shared_limit / PARITY)
            state["maturities_pending"] = available.maturities_pending
            if available.maturities_pending:
                state["unactivated"] = 0  # no issuance can pass until the keeper advances the claim book
        return state

    def _buy_cash(self, d, budget, minimum_net=0, *, for_repayment=False, repayment_target=None):
        if getattr(d, "buck_accounting", "") == "net":
            cash                = d.chain.balance_of(d.usdc, self.proxy.address)
            spend               = min(cash, budget)
            if spend <= 0:
                return 0
            if for_repayment:
                if (type(repayment_target) is not int or repayment_target <= 0 or
                        getattr(self, "_net_repayment_reserve", None) is not None or
                        getattr(self, "_net_repayment_target", None) is not None):
                    raise ValueError("buyback requires a new positive repayment target")
                self._net_repayment_reserve = d.buck.functions.balanceOf(self.proxy.address).call() + 1
                self._net_repayment_target = repayment_target
                self._net_trade_uncertain = True
                self._net_pending_buy_hash = None
            try:
                purchase        = d.net_market_ops.buy_exact_input(
                    self.proxy.address, d.pool_ub, spend, minimum_net, self.proxy.address)
                if (purchase is None or type(getattr(purchase, "paid", None)) is not int or
                        type(getattr(purchase, "received", None)) is not int or
                        purchase.paid < 0 or purchase.paid > spend or purchase.received < minimum_net):
                    raise TransactionReceiptPending(None)
            except TransactionReverted as exc:
                if for_repayment:
                    if not exc.transaction_hash or exc.receipt.get("status") != 0:
                        self._net_pending_buy_hash = exc.transaction_hash or None
                        raise TransactionReceiptPending(exc.transaction_hash or None) from exc
                    self._net_trade_uncertain = False
                    self._net_repayment_reserve = None
                    self._net_repayment_target = None
                raise
            except TransactionReceiptPending as exc:
                if for_repayment:
                    self._net_pending_buy_hash = exc.transaction_hash
                raise
            if for_repayment:
                self._net_trade_uncertain = False
                if not purchase.received:
                    self._net_repayment_reserve = None
                    self._net_repayment_target = None
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

    def assess_net_refinance(self, d, day, **forecasts):
        keys = ("expected_relief_rate", "expected_refund_growth", "expected_wallet_growth")
        explicit = any(key in forecasts for key in keys)
        if explicit and not all(key in forecasts for key in keys):
            raise ValueError("explicit household forecasts must include all three annual rates")
        observed = None if explicit else observe_refinance_forecasts(d)
        result = assess_refinance(self, d, day, **((observed["values"] if observed else {}) | forecasts))
        if observed:
            if result["context"]["block"] != observed["observation"]["block_number"]:
                raise ValueError("household forecast block changed")
            result["context"]["forecasts"].update(source=observed["observation"], basis=observed["basis"])
        if set(forecasts) <= set(keys):
            # Later same-block saving/working-money choices reuse this front.
            self._net_funding = ((result["context"]["block"], day, tuple(sorted(forecasts.items()))),
                                 result["context"]["funding"])
        return result

    def start_net_refinance(self, d, plan, day, ctr, *, context=None):
        self._prepare_net_refinance(d, day)
        return self.refinance_machine.start(d, plan, day, ctr, context=context)

    def retry_net_refinance_payment(self, d, day, ctr):
        """Retry only a proved-refused bank leg; keep its confirmed issue/export."""
        self._prepare_net_refinance(d, day)
        return self.refinance_machine.retry_payment(d, day, ctr)

    def assess_net_refinance_remainder(self, d, day, **options):
        """Read current cash-only completion choices; never quote another issue."""
        return assess_remainder(self, d, day, **options)

    def resume_net_refinance(self, d, day, ctr, **options):
        self._prepare_net_refinance(d, day)
        return self.refinance_machine.resume(d, day, ctr, **options)

    def _prepare_net_refinance(self, d, day):
        """Serialize refinancing with calendar flows and the saved buyback intent."""
        if getattr(d, "buck_accounting", "") != "net":
            raise ValueError("canonical refinancing requires net accounting")
        if self.calendar_machine.bound:
            self.calendar_machine.prepare(d, day)
            if self.calendar_machine.active:
                raise ValueError("household calendar must finish before refinancing")
        if (self._pending_repayment(d) or self._net_repayment_reserve is not None or
                self._net_repayment_target is not None):
            raise ValueError("household buyback must finish before refinancing")

    def advance_net_calendar(self, d, day, ctr):
        """Explicit recurring-flow component; normal economic decisions remain gated."""
        return self.calendar_machine.advance(d, day, ctr)

    def _ensure_wallet(self, d):
        """The working balance's own bound Ordinary account, able to sell for dollars."""
        if self._wallet is None:
            self._wallet        = self._new_proxy(d)
            self._wallet_ready  = False
        if not self._wallet_ready:
            for contract, method, args in ((d.buck, "approve", [d.permit2.address, 2**256 - 1]),
                                           (d.permit2, "approve", [d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1])):
                self._proxy_exec(d, contract.address, contract.encode_abi(method, args=args), proxy=self._wallet)
            self._wallet_ready  = True
        return self._wallet

    def assess_net_wallet(self, d, day, *, forecasts=None):
        return assess_wallet(self, d, day, forecasts=forecasts)

    def start_net_wallet(self, d, plan, day, ctr, *, context=None):
        self._prepare_net_refinance(d, day)
        return self.refinance_machine.start_wallet(d, plan, day, ctr, context=context)

    def advance_net_wallet(self, d, day, ctr, *, forecasts=None):
        """Existing owner dispatcher: tend the BUCK working balance after a payday.

        PRE: normal keeper/control work, current integer day and prepared
        trading for the main account. POST: recover earlier evidence, calendar
        and buybacks first; then at most one working-balance trade per pay
        period, from dollars above the buffer and credit front. A refused or
        declined attempt waits for the next payday. The wallet's own account
        and approvals are created on first use, before any flow starts.
        """
        if type(day) is not int or not 0 <= day < 2**53 or getattr(d, "buck_accounting", "") != "net":
            raise ValueError("household wallet needs canonical accounting and an integer day")
        if day < self.arrive_day or self.exited:
            return False
        if self._net_dispatching:
            raise ValueError("household dispatcher is already running")
        self._net_dispatching = True
        try:
            if not self.calendar_machine.bound:
                self.calendar_machine.prepare(d, day)
            if self._recover_pending_operations(d, ctr, day, continue_after_calendar=True):
                return True
            m = self.refinance_machine
            if m.settlement_due or m.surplus_due:
                self.last_action = "settlement-due" if m.settlement_due else "surplus-due"
                return True
            if day < self._next_wallet:
                return False
            self._next_wallet  += ((day - self._next_wallet) // 30 + 1) * 30
            self.last_error     = ""
            try:
                assessed        = self.assess_net_wallet(d, day, forecasts=forecasts)
                if assessed["plan"] is None:
                    self.last_action = "wallet-hold"
                    m.decisions += 1
                    m._event(d, day, "declined", kind="wallet", phase="assessment", context=assessed["context"],
                             reason=assessed["context"]["reason"], market=m._market(d))
                    return True
                wallet          = self._ensure_wallet(d)
                return self.start_net_wallet(d, replace(assessed["plan"], wallet=wallet.address), day, ctr,
                                             context=assessed["context"])
            except Exception as exc:
                self.last_action, self.last_error = "wallet-unavailable", str(exc)[:200]
                ctr["household_err"] = self.last_error
                m._event(d, day, "unavailable", kind="wallet", phase="assessment", reason=self.last_error)
                return True
            finally:
                self.last_wallet = self.last_action
        finally:
            self._net_dispatching = False

    def assess_net_surplus(self, d, day, *, forecasts=None):
        return assess_surplus(self, d, day, forecasts=forecasts)

    def start_net_surplus(self, d, plan, day, ctr, *, context=None):
        self._prepare_net_refinance(d, day)
        result = self.refinance_machine.start_surplus(d, plan, day, ctr, context=context)
        if not self.refinance_machine.active and self._net_repayment_target is not None:
            self._resume_net_repayment(d, ctr)
        return result

    def resume_net_surplus(self, d, day, ctr):
        self._prepare_net_refinance(d, day)
        return self.refinance_machine.resume_surplus(d, day, ctr)

    def advance_net_surplus(self, d, day, ctr, *, forecasts=None):
        """Existing owner dispatcher, with reserve-safe discretionary choices.

        PRE: normal keeper/control work, current integer day and prepared
        trading. POST: recover earlier evidence/calendar/buybacks first; never
        spend a credit funding target. Saving/retirement decisions retain the
        household's keyed interval, with daily retry of a known deposit refusal.
        Full saver issuance/working money/exits still gate the normal act loop.
        """
        if type(day) is not int or not 0 <= day < 2**53 or getattr(d, "buck_accounting", "") != "net":
            raise ValueError("household surplus needs canonical accounting and an integer day")
        if day < self.arrive_day or self.exited:
            return False
        if self._net_dispatching:
            raise ValueError("household dispatcher is already running")
        self._net_dispatching = True
        try:
            if not self.calendar_machine.bound:
                self.calendar_machine.prepare(d, day)
            if self._recover_pending_operations(d, ctr, day, continue_after_calendar=True):
                return True
            if self.exit_due(day):
                return False                            # a leaving household's work belongs to advance_net_exit
            m = self.refinance_machine
            if m.settlement_due:
                self.last_action = "settlement-due"
                return True
            if self._net_surplus_day == day or day < self._next_surplus and not m.surplus_due:
                return False
            self._net_surplus_day = day
            period = self.p["decision_days"]
            self._next_surplus += ((day - self._next_surplus) // period + 1) * period
            self.last_error = ""
            try:
                if m.surplus_due:
                    return self.resume_net_surplus(d, day, ctr)
                assessed = self.assess_net_surplus(d, day, forecasts=forecasts)
                if assessed["plan"] is None:
                    self.last_action = "surplus-wait"
                    m.decisions += 1
                    # The same pinned market record a planned operation carries:
                    # a wait is inspectable against K, the basket and the pool.
                    m._event(d, day, "declined", kind="surplus", phase="assessment", context=assessed["context"],
                             reason=assessed["context"]["reason"], market=m._market(d))
                    return True
                return self.start_net_surplus(d, assessed["plan"], day, ctr, context=assessed["context"])
            except Exception as exc:
                self.last_action, self.last_error = "surplus-unavailable", str(exc)[:200]
                ctr["household_err"] = self.last_error
                m._event(d, day, "unavailable", kind="surplus", phase="assessment", reason=self.last_error)
                return True
            finally:
                self.last_surplus = self.last_action
        finally:
            self._net_dispatching = False

    def _drawdown_due(self, day):
        """A retired lifecycle investor's month of income is due, or what it redeemed waits to be sold."""
        if not (self.lifecycle and self.investor) or policy.stage(self.p, self.arrive_day, day) != "retired":
            return False
        return self._drawdown_to_sell > 0 or self._next_drawdown is None or day >= self._next_drawdown

    def _drawdown_step(self, d, day, ctr):
        """One step of a retired household's monthly income (household_policy.drawdown_share).

        On the month's first step it takes that share of its external fund (paid off chain) and redeems
        that share of a basket receipt home, paid in BUCK (the basket's exits); on the following days it
        sells what the redemption paid for dollars, one capped market bite a day, as an exit sells.
        Neither issues nor retires principal. Returns True when it sent a transaction.
        """
        m                       = self.refinance_machine
        if self._drawdown_to_sell <= 0:
            share               = policy.drawdown_share(self.p, self.arrive_day, day)
            paid                = int(self.usd_fund * share)
            self.usd_fund      -= paid
            self.usd_fund_paid += paid
            self._next_drawdown = day + policy.MONTH_DAYS
            self.last_action    = "drawdown"
            for rid in list(self._basket_receipts):
                shares          = self._receipt_book[rid][1]
                amount          = min(shares, max(1, int(shares * share)))
                try:
                    quote       = _basket_ops(d).preview_redemption(self.address, rid, amount)
                except Exception as exc:
                    m._event(d, day, "waiting", kind="drawdown", phase="redeem", reason=str(exc)[:200])
                    return False
                if quote["paid"] < PARITY:
                    continue
                before          = d.buck.functions.balanceOf(self.address).call()
                m.start_exit(d, ExitPlan("redeem", position_id=rid, shares=amount,
                                         minimum_buck=quote["paid"] * 9950 // 10_000),
                             day, ctr, context=dict(reason="drawdown", share=share, fund_paid=paid))
                # What the redemption actually paid is the month's income to sell (0 if it is not settled).
                self._drawdown_to_sell = max(0, d.buck.functions.balanceOf(self.address).call() - before)
                ctr["hhDrawdowns"] = ctr.get("hhDrawdowns", 0) + 1
                self.last_action = "drawdown"           # the exit step names itself; this is the month's income
                return True
            return False
        if self._exit_trade_day == day:
            return False                                # one market bite a day
        block                   = d.w3.eth.block_number
        held                    = d.buck.functions.balanceOf(self.address).call(block_identifier=block)
        market                  = observe_net_market(d.chain, d.buck, d.usdc, d.pool_ub, d.fee_ub, block_number=block)
        cap                     = market.reserve_buck * int(self.max_impact_bp * 50) // 10**6
        bite                    = min(_exportable(held, 0), cap, self._drawdown_to_sell)
        minimum                 = market.sell_expected(bite) * 9950 // 10_000
        if bite < PARITY or minimum < PARITY:
            self._drawdown_to_sell = 0                  # nothing worth a sale is left of this month's
            return False
        m.start_exit(d, ExitPlan("sell", sell_buck=bite, minimum_usdc=minimum), day, ctr,
                     context=dict(reason="drawdown"))
        self._drawdown_to_sell -= bite
        self._exit_trade_day = day
        self.last_action        = "drawdown-sell"
        return True

    def assess_net_investor(self, d, day):
        return assess_investor(self, d, day)

    def advance_net_investor(self, d, day, ctr):
        """Existing owner dispatcher: a cash investor's decision on its keyed cadence.

        PRE: normal keeper/control work, current integer day and prepared
        trading. POST: recover earlier evidence and recurring income first; a
        known refused deposit is retried alone; then at most one decision a
        day: buy BUCK with spare dollars and hold them, or, when they are
        dear, deposit what is held in the basket. Its withdrawal at the
        horizon belongs to advance_net_exit.
        """
        if type(day) is not int or not 0 <= day < 2**53 or getattr(d, "buck_accounting", "") != "net":
            raise ValueError("investor decision needs canonical accounting and an integer day")
        if not self.investor:
            raise ValueError("investor decision requires a cash investor")
        if day < self.arrive_day or self.exited:
            return False
        if self._net_dispatching:
            raise ValueError("household dispatcher is already running")
        self._net_dispatching = True
        try:
            if not self.calendar_machine.bound:
                self.calendar_machine.prepare(d, day)
            if self._recover_pending_operations(d, ctr, day, continue_after_calendar=True):
                return True
            m = self.refinance_machine
            if self.exit_due(day):
                return False
            if self._drawdown_due(day):
                return self._drawdown_step(d, day, ctr)
            if self._net_surplus_day == day or day < self._next_decision and not m.surplus_due:
                return False
            self._net_surplus_day = day
            if day >= self._next_decision:
                period = self.p["decision_days"]
                self._next_decision += ((day - self._next_decision) // period + 1) * period
            self.last_error = ""
            try:
                if m.surplus_due:
                    return self.resume_net_surplus(d, day, ctr)
                assessed = self.assess_net_investor(d, day)
                if assessed["plan"] is None:
                    self.last_action = "investor-wait"
                    m.decisions += 1
                    m._event(d, day, "declined", kind="investor", phase="assessment", context=assessed["context"],
                             reason=assessed["context"]["reason"], market=m._market(d))
                    return True
                return self.start_net_surplus(d, assessed["plan"], day, ctr, context=assessed["context"])
            except Exception as exc:
                self.last_action, self.last_error = "investor-unavailable", str(exc)[:200]
                ctr["household_err"] = self.last_error
                m._event(d, day, "unavailable", kind="investor", phase="assessment", reason=self.last_error)
                return True
            finally:
                self.last_surplus = self.last_action
        finally:
            self._net_dispatching = False

    def exit_due(self, day):
        """Whether this household's keyed leaving day has come: its asset sale, or an investor's horizon."""
        due = self._exit_day if self.investor else self._divest_day
        return due is not None and day >= due

    def advance_net_exit(self, d, day, ctr):
        """Existing owner dispatcher: a leaving household's staged, resumable unwinding.

        PRE: normal keeper/control work, current integer day and prepared
        trading. POST: recover earlier evidence, calendar and buybacks first;
        then at most ONE step, chosen from what the chain and books show now:
        sell the insured asset (its price pays arrears and the whole
        mortgage), sell the working balance, redeem each basket receipt,
        retire the whole lien with owned BUCK and then with bought BUCK a
        capped bite at a time, release coverage no principal needs, sell the
        BUCK left, and leave. An investor only redeems, sells and leaves.
        A refused or empty step waits for the next day; an unknown outcome
        stops everything until reconciled. Returns True while work remains.
        """
        if type(day) is not int or not 0 <= day < 2**53 or getattr(d, "buck_accounting", "") != "net":
            raise ValueError("household exit needs canonical accounting and an integer day")
        if day < self.arrive_day or self.exited or not self.exit_due(day):
            return False
        if self._net_dispatching:
            raise ValueError("household dispatcher is already running")
        self._net_dispatching = True
        try:
            if not self.calendar_machine.bound:
                self.calendar_machine.prepare(d, day)
            if self._recover_pending_operations(d, ctr, day, continue_after_calendar=True):
                return True
            self.last_error     = ""
            try:
                return self._exit_step(d, day, ctr)
            except Exception as exc:
                self.last_action, self.last_error = "exit-unavailable", f"{self.exit_stage}: {exc}"[:200]
                ctr["household_err"] = self.last_error
                self._exit_wait_day = day
                self.refinance_machine._event(d, day, "unavailable", kind="exit", phase=self.exit_stage or "exit",
                                              reason=self.last_error)
                return False
        finally:
            self._net_dispatching = False

    def _exit_step(self, d, day, ctr):
        m, cal              = self.refinance_machine, self.calendar_machine
        if self.exit_started is None:
            self.exit_started = day
            self.funding_pending = False
            self.cash_buffer = 0                        # a leaving household protects no dollar buffer
            m.decisions    += 1
            m._event(d, day, "started", kind="exit", phase="exit",
                     reason="investor horizon" if self.investor else "insured asset sale", market=m._market(d))
        if m.settlement_due or m.surplus_due:
            # A known refused remainder's intent ended with the household's stay; its cash is simply owned.
            m.abandon(d, day, "household is leaving; the unfinished remainder is not resumed")
        if self._net_repayment_target is not None or self._net_repayment_reserve is not None:
            return self._resume_net_repayment(d, ctr)
        waited              = self._exit_wait_day == day
        traded              = self._exit_trade_day == day  # one capped market bite a day, as any household
        block               = d.w3.eth.block_number
        read                = lambda fn: fn.call(block_identifier=block)
        cash                = read(d.usdc.functions.balanceOf(self.address))
        market              = observe_net_market(d.chain, d.buck, d.usdc, d.pool_ub, d.fee_ub, block_number=block)
        impact              = int(self.max_impact_bp * 50)
        buck_cap, cash_cap  = market.reserve_buck * impact // 10**6, market.reserve_usdc * impact // 10**6
        def wait(stage, reason):
            # A step that cannot run today is not a failure: record it once, try again tomorrow.
            self.exit_stage, self.last_action = stage, "exit-wait"
            if not waited:
                self._exit_wait_day = day
                self.exit_retries += 1
                m._event(d, day, "waiting", kind="exit", phase=stage, reason=str(reason)[:200])
            return False
        def pace(stage):
            # Today's market bite is taken; the next is tomorrow's. The normal pace, not a retry.
            self.exit_stage, self.last_action = stage, "exit-pace"
            return False
        if not self.investor:
            if not self._sold:
                self.exit_stage = "sell-asset"
                cal.schedule_sale(d, day)
                ctr["hhDivestments"] = ctr.get("hhDivestments", 0) + 1
                cal.advance(d, day, ctr)
                return True
            if cash and cal.schedule_settlement(d, day):
                self.exit_stage = "settle-mortgage"
                cal.advance(d, day, ctr)
                return True
        if self._wallet is not None:
            held            = read(d.buck.functions.balanceOf(self._wallet.address))
            bite            = min(_exportable(held, 0), buck_cap)
            minimum         = market.sell_expected(bite) * 9950 // 10_000
            if bite >= PARITY and minimum >= PARITY:
                if waited or traded:
                    return pace("collect-savings")
                self.exit_stage = "collect-savings"
                self._ensure_wallet(d)
                m.start_wallet(d, WalletPlan("sell", self._wallet.address, 0, 0, held, sell_buck=bite, minimum_usdc=minimum),
                               day, ctr, context=dict(reason="exit"))
                return self._exit_sent(day, market=True)
        if self._basket_receipts:
            self.exit_stage = "collect-savings"
            if waited:
                return pace("collect-savings")
            rid             = self._basket_receipts[0]
            shares          = self._receipt_book[rid][1]
            found, why      = None, "no BUCK-paying exit"
            for part in (1, 2, 4, 8):                   # the whole receipt, else a smaller bite the basket can pay
                amount      = shares // part
                if not amount:
                    break
                try:
                    quote   = _basket_ops(d).preview_redemption(self.address, rid, amount)
                except Exception as exc:
                    why     = exc
                    continue
                if quote["paid"] >= PARITY:
                    found   = ExitPlan("redeem", position_id=rid, shares=amount, minimum_buck=quote["paid"] * 9950 // 10_000)
                    break
            if found is None:
                return wait("collect-savings", f"basket exit unavailable: {why}")
            m.start_exit(d, found, day, ctr, context=dict(reason="exit"))
            return self._exit_sent(day)
        book                = d.net_credit_ops.household(self.address, self._token_ids, block_number=block) if self._token_ids else None
        held                = read(d.buck.functions.balanceOf(self.address))
        if book is not None and book.issued:
            self.exit_stage = "repay-lien"
            if held > 1:
                if waited:
                    return pace("repay-lien")
                # Every owned BUCK above one raw unit goes to the lien; the engine keeps its own evidence.
                self._net_repayment_reserve, self._net_repayment_target = 1, book.issued
                before      = self.unwound
                self._resume_net_repayment(d, ctr)
                if self.unwound == before and not self._pending_repayment(d):
                    self._net_repayment_reserve = self._net_repayment_target = None
                    self._net_repayment_interrupted = False
                    return wait("repay-lien", self.last_error or "nothing could be retired today")
                return True
            cost            = market.buy_cost(book.issued)
            spend           = min(cash, cash_cap, cash_cap if cost is None else (cost * 10_050 + 9_999) // 10_000)
            minimum         = market.buy_expected(spend) * 9950 // 10_000 if spend else 0
            if spend < PARITY or minimum < PARITY:
                if not self.exit_short:
                    ctr["hhDivestShort"] = ctr.get("hhDivestShort", 0) + 1
                self.exit_short = True                  # insolvency is not a completed exit
                return wait("cash-shortfall", "no dollars left to buy back the remaining lien")
            if waited or traded:
                return pace("repay-lien")
            self.start_net_surplus(d, SurplusPlan("retire", 0, held, spend, minimum, book.issued), day, ctr,
                                   context=dict(reason="exit"))
            return self._exit_sent(day, market=True)
        if book is not None and not self._released:
            self.exit_stage = "release-coverage"
            covered         = tuple(f.token_id for f in book.facilities if f.activated)
            if not covered:
                self._released = True
            elif waited:
                return pace("release-coverage")
            else:
                m.start_exit(d, ExitPlan("release", token_ids=covered), day, ctr, context=dict(reason="exit"))
                if not m.active and m.flow["status"] == "complete":
                    left    = d.net_credit_ops.household(self.address, self._token_ids)
                    self._released = not any(f.activated for f in left.facilities)
                    if not self._released:
                        return wait("release-coverage", "a facility's pool cannot refund its deposit yet")
                return self._exit_sent(day)
        bite                = min(_exportable(held, 0), buck_cap)
        minimum             = market.sell_expected(bite) * 9950 // 10_000
        if bite >= PARITY and minimum >= PARITY:
            self.exit_stage = "sell-owned-buck"
            if waited or traded:
                return pace("sell-owned-buck")
            m.start_exit(d, ExitPlan("sell", sell_buck=bite, minimum_usdc=minimum), day, ctr, context=dict(reason="exit"))
            return self._exit_sent(day, market=True)
        for i, token in enumerate(d.tokens):            # TOKEN an in-kind basket exit paid: a capped bite a day
            amount          = d.chain.balance_of(token, self.address)
            if amount:
                ru, rt      = active_reserves(d.chain, d.pool_usdc[i], d.usdc, token)
                if amount * ru // rt <= PARITY:
                    continue                            # at most one dollar's dust per constituent
                self.exit_stage = "sell-tokens"
                if waited or traded:
                    return pace("sell-tokens")
                self._swap_via_simlp(d, d.pool_usdc[i], token, min(amount, max(1, _impact_cap(rt, self.max_impact_bp))),
                                     self.address)
                self.withdrawn_usd += d.chain.balance_of(d.usdc, self.address) - cash
                return self._exit_sent(day, market=True)
        self.exited, self.exit_stage, self.last_action = True, "complete", "exit"
        key                 = "invExits" if self.investor else "hhDivested"
        ctr[key]            = ctr.get(key, 0) + 1
        m._event(d, day, "complete", kind="exit", phase="complete", outcome="exited",
                 amounts=dict(withdrawn_usdc=self.withdrawn_usd, released_coverage=self.released_coverage,
                              premium_refund_nominal=self.released_refund, retired_principal=self.unwound))
        return True

    def _exit_sent(self, day, *, market=False):
        """After one exit transaction: a market bite is the day's only one; a proved refusal waits too."""
        m                   = self.refinance_machine
        if market:
            self._exit_trade_day = day
        if not m.active and m.flow is not None and m.flow["status"] == "refused":
            self._exit_wait_day = day                   # retried tomorrow, never today
            self.exit_retries += 1
        return True

    def assess_net_investment(self, d, day, **forecasts):
        """Read one mortgage-free household's quoted credit investment; observed forecasts by default."""
        keys = ("expected_relief_rate", "expected_refund_growth", "expected_wallet_growth")
        explicit = any(key in forecasts for key in keys)
        if explicit and not all(key in forecasts for key in keys):
            raise ValueError("explicit household forecasts must include all three annual rates")
        observed = None if explicit else observe_refinance_forecasts(d)
        result = assess_investment(self, d, day, **((observed["values"] if observed else {}) | forecasts))
        if observed:
            if result["context"]["block"] != observed["observation"]["block_number"]:
                raise ValueError("household forecast block changed")
            result["context"]["forecasts"].update(source=observed["observation"], basis=observed["basis"])
        return result

    def start_net_investment(self, d, plan, day, ctr, *, context=None):
        self._prepare_net_refinance(d, day)
        return self.refinance_machine.start_investment(d, plan, day, ctr, context=context)

    def advance_net_investment(self, d, day, ctr, *, forecasts=None):
        """Existing owner dispatcher: a mortgage-free household's credit investment.

        PRE: normal keeper/control work, current integer day and prepared
        trading. POST: recover earlier evidence, calendar and buybacks first;
        a known refused deposit is retried alone before any new credit; then
        at most one fresh assessment on the household's keyed decision cadence.
        A debtor is served by refinancing until its mortgage is paid. A
        declined or refused attempt waits for the next decision day.
        """
        if type(day) is not int or not 0 <= day < 2**53 or getattr(d, "buck_accounting", "") != "net":
            raise ValueError("household investment needs canonical accounting and an integer day")
        if self.investor:
            raise ValueError("credit investment requires an insured household")
        if day < self.arrive_day or self.exited:
            return False
        if self._net_dispatching:
            raise ValueError("household dispatcher is already running")
        self._net_dispatching = True
        try:
            if not self.calendar_machine.bound:
                self.calendar_machine.prepare(d, day)
            if self._recover_pending_operations(d, ctr, day, continue_after_calendar=True):
                return True
            if self.exit_due(day):
                return False                            # a leaving household's work belongs to advance_net_exit
            m = self.refinance_machine
            if self.mortgage or m.settlement_due:
                return False
            if self._net_invest_day == day or day < self._next_decision and not m.surplus_due:
                return False
            self._net_invest_day = day
            due = day >= self._next_decision
            if due:
                period = self.p["decision_days"]
                self._next_decision += ((day - self._next_decision) // period + 1) * period
            self.last_error = ""
            try:
                if m.surplus_due:
                    # The issued or saved BUCK is deposited before any new credit is drawn.
                    self._prepare_net_refinance(d, day)
                    return m.resume_surplus(d, day, ctr)
                assessed = self.assess_net_investment(d, day, **(forecasts or {}))
                self._last_verdict = assessed["context"].get("selected")
                if assessed["plan"] is None:
                    self.last_action = "invest-hold"
                    m.decisions += 1
                    m._event(d, day, "declined", kind="invest", phase="assessment", context=assessed["context"],
                             reason=assessed["context"]["reason"], market=m._market(d))
                    return True
                return self.start_net_investment(d, assessed["plan"], day, ctr, context=assessed["context"])
            except Exception as exc:
                self.last_action, self.last_error = "invest-unavailable", str(exc)[:200]
                ctr["household_err"] = self.last_error
                m._event(d, day, "unavailable", kind="invest", phase="assessment", reason=self.last_error)
                return True
            finally:
                self.last_issue = self.last_action
        finally:
            self._net_dispatching = False

    def advance_net_refinancing(self, d, day, ctr, *, forecasts=None):
        """Automatic debtor component; normal saving/exits remain gated.

        PRE: one owner dispatcher, a canonical world at this integer day and
        normal external keeper/control maintenance. POST: recover earlier
        evidence/calendar/buybacks before new work; resume committed cash
        before borrowing again. One fresh assessment or known-refusal resume
        per day. Adoption uses the existing keyed phase/cadence; after actual
        issuance, retry fresh bounded tranches daily. No forecast is funding.
        A recovery consumes this call and does not reset the daily limit on
        fresh assessments. Healthy due calendar work can precede a choice.
        """
        if type(day) is not int or day < 0 or getattr(d, "buck_accounting", "") != "net":
            raise ValueError("household refinancing needs canonical accounting and an integer day")
        if self.p["role"] != "debtor":
            raise ValueError("automatic refinancing requires a debtor household")
        if day < self.arrive_day or self.exited:
            return False
        if self._net_dispatching:
            raise ValueError("household dispatcher is already running")
        self._net_dispatching = True
        try:
            if not self.calendar_machine.bound:
                self.calendar_machine.prepare(d, day)
            if self._recover_pending_operations(d, ctr, day, continue_after_calendar=True):
                return True
            if self.exit_due(day):
                return False                            # a leaving household's work belongs to advance_net_exit
            machine = self.refinance_machine
            if self._net_refinance_day == day:
                return False
            if not machine.settlement_due and not self.mortgage:
                self.last_action = "mortgage-paid"
                return False
            if not (self.joined or machine.settlement_due) and day < self._next_decision:
                return False
            self._net_refinance_day = day
            period = self.p["decision_days"]
            self._next_decision = (day + 1 if self.joined or machine.settlement_due else
                                   self._next_decision + ((day - self._next_decision) // period + 1) * period)
            self.last_error = ""
            try:
                if machine.settlement_due:
                    return self.resume_net_refinance(d, day, ctr)
                assessed = self.assess_net_refinance(d, day, **(forecasts or {}))
                self._last_verdict = assessed["context"].get("selected")
                if assessed["plan"] is None:
                    self.last_action = "declined"
                    machine.decisions += 1
                    machine._event(d, day, "declined", kind="decision", phase="assessment",
                                   reason=assessed["context"]["reason"], context=assessed["context"], limits=self.p)
                    return True
                return self.start_net_refinance(d, assessed["plan"], day, ctr, context=assessed["context"])
            except Exception as exc:
                self.last_action, self.last_error = "refinance-unavailable", str(exc)[:200]
                ctr["household_err"] = self.last_error
                machine._event(d, day, "unavailable", kind="decision", phase="assessment", reason=self.last_error)
                return True
            finally:
                if self.joined:
                    self._next_decision = day + 1
                self.last_issue = self.last_action
        finally:
            self._net_dispatching = False

    def _recover_pending_operations(self, d, ctr, day=None, *, continue_after_calendar=False):
        """Reconcile one known receipt before this household submits more work.

        A recovered outcome consumes this turn. Unknown hashes, an untracked
        authorization remains stopped for external reconciliation. A confirmed
        authorization or reverted settlement can be canceled by this dispatcher.
        This simulation assumes a single dispatcher and immediate finality.
        """
        if getattr(d, "buck_accounting", "") != "net":
            return False
        if self.calendar_machine.bound:
            self.calendar_machine.prepare(d, self._last_day if day is None else day)
        try:
            machine = getattr(self, "refinance_machine", None)
            if machine is not None and machine.active:
                return machine.advance(d, self._last_day if day is None else day, ctr)
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
            if self._pending_repayment(d):
                return True
            if self.calendar_machine.active:
                recovering = self.calendar_machine.flow is not None and self.calendar_machine.flow["status"] == "pending"
                self.calendar_machine.advance(d, self._last_day if day is None else day, ctr)
                if self.calendar_machine.active or recovering or not continue_after_calendar:
                    return True  # recovered/uncertain recurring work always consumes this turn
            return self._resume_net_repayment(d, ctr)
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
        self._net_retirement_event(d, "canceled", operation="cancel", token_id=token_id, principal=expected[0],
                                   transaction_hash=tx_hash, reason="unsettled authorization withdrawn; target retained")

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
        principal               = getattr(self, "_net_pending_principal", 0)
        self._clear_pending_repayment()
        self.last_error         = ""
        self.last_action        = "repay-canceled"
        self._net_retirement_event(d, "canceled", operation="cancel", token_id=token_id, principal=principal, recovered=True,
                                   transaction_hash=tx_hash, reason="unsettled authorization withdrawn; target retained")

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
            self._net_repayment_target = None
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
            self._net_repayment_target = None
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
                self._net_retirement_event(d, "refused", operation="authorize", token_id=token_id, principal=principal,
                                           transaction_hash=tx_hash, reason="authorization reverted; nothing was retired")
                return 0
            plan                = ReductionPlan(token_id, principal, 0, expected[1], 0, "ready")
            d.net_credit_ops.authorization_from_receipt(self.proxy.address, plan, expected[2], receipt,
                                                        expected_hash=tx_hash)
            if actual != expected:
                raise ValueError("authorization changed; reconcile before canceling")
            self._net_authorization_confirmed = True
            self.last_error     = ""
            self.last_action    = "authorize-reconciled"
            self._net_retirement_event(d, "recovered", operation="authorize", token_id=token_id, principal=principal,
                                       transaction_hash=tx_hash, reason="authorization confirmed; no principal retired")
            return 0
        if receipt.get("status") == 0:
            self._net_pending_reverted = True
            self.last_error     = ""
            self.last_action    = "repay-reverted"
            self._net_retirement_event(d, "refused", operation="reduce", token_id=token_id, principal=principal,
                                       transaction_hash=tx_hash,
                                       reason="settlement reverted; authorization awaits cancellation")
            return 0
        settled                 = d.net_credit_ops.reduction_from_receipt(
            self.proxy.address, token_id, principal, receipt, expected_hash=tx_hash)
        self._record_net_repayment(settled.principal)
        self.unwound           += settled.principal
        ctr["bcdRetired"]       = ctr.get("bcdRetired", 0) + settled.principal
        if self.p["surplus_policy"] == "adaptive":
            ctr["hhPaidDown"]   = ctr.get("hhPaidDown", 0) + settled.principal
        self._clear_pending_repayment()
        self.last_error         = ""
        self.last_action        = "repay-reconciled"
        self._net_reduction_event(d, settled, recovered=True)
        self._net_retirement_done(d)
        return settled.principal

    def _net_retirement_event(self, d, status, **detail):
        """Selected-facility work on the household's existing public history.

        PRE: any confirmed principal was already applied to the saved target.
        POST: one append-only record with the retained target/reserve; never a
        financial transition. Funding a target is not a reduction: only an
        authenticated FacilityReduced receipt is recorded as "confirmed".
        """
        machine                 = getattr(self, "refinance_machine", None)
        if machine is None:
            return              # isolated recovery fixtures have no household history
        calendar                = getattr(self, "calendar_machine", None)
        day                     = calendar.day if calendar is not None and calendar.bound else self._last_day
        try:
            machine._event(d, day, status, kind="retirement", phase="reduction", **detail,
                           retirement=dict(target=self._net_repayment_target, reserve=self._net_repayment_reserve))
        except Exception as exc:
            # An observation never blocks or reverses a settled transition.
            machine._event(d, day, status, kind="retirement", phase="reduction", unavailable=str(exc)[:200])

    def _net_reduction_event(self, d, settled, *, recovered=False):
        self._net_retirement_event(d, "confirmed", operation="reduce", recovered=recovered, amounts=dict(
            token_id=settled.token_id, principal=settled.principal, relief_scaled=settled.relief_scaled,
            owner_cost_scaled=settled.owner_cost_scaled, premium_refund_nominal=settled.premium_nominal),
            transaction_hash=_txhex(settled.receipt["transactionHash"]), receipt_block=settled.receipt.get("blockNumber"))

    def _net_retirement_done(self, d, reason="target retired"):
        """Close the public record once no target, reserve or uncertainty remains."""
        if (self._net_repayment_target is None and self._net_repayment_reserve is None and
                getattr(self, "_net_pending_token", None) is None):
            self._net_retirement_event(d, "complete", reason=reason)

    def _record_net_repayment(self, principal):
        """Apply a confirmed principal once to the retained buyback target."""
        target                  = getattr(self, "_net_repayment_target", None)
        if target is None:
            return              # old diagnostic callers supplied no saved target
        if principal > target:
            raise ValueError("confirmed repayment exceeds the saved target")
        self._net_repayment_target = target - principal
        if not self._net_repayment_target:
            self._net_repayment_target = self._net_repayment_reserve = None

    def _resume_net_repayment(self, d, ctr):
        """Finish the prior purchase's target before considering a new action."""
        if getattr(d, "buck_accounting", "") != "net":
            return False
        reserve                 = getattr(self, "_net_repayment_reserve", None)
        target                  = getattr(self, "_net_repayment_target", None)
        if reserve is None and target is None:
            return False
        if reserve is None or type(target) is not int or target <= 0:
            self.last_error     = "buyback target or reserve unavailable; reconcile before continuing"
            self.last_action    = "repay-pending"
            return True
        issued                  = d.net_credit_ops.household(self.proxy.address, self._token_ids).issued
        if type(issued) is not int or issued < 0:
            raise ValueError("invalid current household principal")
        target                  = min(target, issued)
        if not target:
            self._net_repayment_target = self._net_repayment_reserve = None
            self.last_action    = "pay-down"
            self._net_retirement_done(d, "no principal remains")
            return True
        self._net_repayment_target = target
        retired                 = self._retire_acquired(d, target, 0)
        self.unwound           += retired
        ctr["bcdRetired"]       = ctr.get("bcdRetired", 0) + retired
        if self.p["surplus_policy"] == "adaptive":
            ctr["hhPaidDown"]   = ctr.get("hhPaidDown", 0) + retired
        if not self._pending_repayment(d):
            self.last_action    = "pay-down-wait" if self._net_repayment_reserve is not None else "pay-down"
        return True

    def _retire_acquired(self, d, target, acquired):
        """Legacy purchases repay implicitly; BuckNet settles selected NFTs explicitly."""
        if getattr(d, "buck_accounting", "") != "net":
            return acquired
        if target <= 0:
            return 0
        buffer                  = getattr(self, "_net_repayment_reserve", None)
        saved_target            = getattr(self, "_net_repayment_target", None)
        if saved_target is None:
            if buffer is not None:
                raise ValueError("buyback target unavailable; reconcile before continuing")
            if acquired <= 0:
                return 0
            self._net_repayment_target = target
        else:
            if type(saved_target) is not int or saved_target <= 0:
                raise ValueError("invalid saved repayment target")
            if buffer is None:
                raise ValueError("buyback reserve unavailable; reconcile before continuing")
            target              = min(target, saved_target)
        if buffer is None:
            held                = d.buck.functions.balanceOf(self.proxy.address).call()
            # Keep at least the full balance held before the first purchase;
            # this conservative floor also protects the issuance reserve.
            buffer              = max(0, held - acquired) + 1
            self._net_repayment_reserve = buffer
        deadline                = d.w3.eth.get_block("latest")["timestamp"] + 3600
        result                  = d.net_credit_ops.retire_household(
            self.proxy.address, self._token_ids, target, deadline, buffer=buffer)
        self._record_net_repayment(result.retired)
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
        # History follows the saved books and evidence; it cannot undo them.
        for settled in getattr(result, "settlements", ()):
            self._net_reduction_event(d, settled)
        if result.status == "authorization-pending":
            self._net_retirement_event(d, "pending",
                                       operation="authorize" if result.pending_phase == "authorization" else "reduce",
                                       token_id=result.blocked_token_id, principal=result.pending_principal,
                                       transaction_hash=result.pending_transaction_hash, reverted=result.pending_reverted,
                                       reason=self.last_error)
            return result.retired
        if self._net_repayment_reserve is not None:
            try:
                held            = d.buck.functions.balanceOf(self.proxy.address).call()
                if held <= buffer:
                    self._net_repayment_reserve = self._net_repayment_target = None
            except Exception as exc:
                # Preserve the reserve conservatively and report completed
                # settlements even if the follow-up balance is unavailable.
                self._net_repayment_interrupted = True
                self.last_error = f"repayment balance unavailable: {exc!r}"[:200]
        if result.status not in ("complete", "partial"):
            self._net_repayment_interrupted = True
            detail              = f": {result.error}" if result.error else ""
            self.last_error     = f"repayment {result.status} at facility {result.blocked_token_id}{detail}"[:200]
            # A fund/owner/quote refusal sent nothing. A retained target and
            # its protected reserve stay saved for a later turn.
            if self._net_repayment_target is not None:
                self._net_retirement_event(d, "waiting", operation="reduce", token_id=result.blocked_token_id,
                                           reason=self.last_error)
        if self._net_repayment_target is None or self._net_repayment_reserve is None:
            self._net_retirement_done(d, "target retired" if result.retired >= target else
                                      "owned cash above the reserve is spent; the remaining target is released")
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
        if getattr(d, "buck_accounting", "") == "net":
            if self._pending_repayment(d) or self._resume_net_repayment(d, ctr):
                return
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
            ru, rb, fee          = state["ru"], state["rb"], state["fee"]
            budget              = min(spare / PARITY, ru * self.max_impact_bp / 20_000)
            want                = (min(state["drawn"], carry.usdc_out(budget, rb, ru, fee))
                                   if p["surplus_policy"] == "retire" else carry.discounted_buyback(
                                       budget, state["drawn"], ru, rb, fee, state["basket_usd"], p["retire_disc"]))
            if want > 1:
                cost            = int(want * ru / ((rb - want) * (1 - fee)) * PARITY)
                spend           = min(int(budget * PARITY), cost)
                ceiling         = state["basket_usd"] * (1 - p["retire_disc"])
                min_net         = (math.floor(spend / ceiling) + 1 if p["surplus_policy"] == "discount"
                                   and getattr(d, "buck_accounting", "") == "net" and ceiling > 0 else 0)
                target          = int(want * PARITY)
                got             = self._buy_cash(d, spend, min_net, for_repayment=True, repayment_target=target)
                retired         = self._retire_acquired(d, target, got)
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
                if custom_error_name(d.buck.abi, exc) != "InsufficientCoverage":
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
        if self._resume_net_repayment(d, ctr):
            return
        adaptive                = self.p["surplus_policy"] == "adaptive"
        share                   = (policy.paydown(policy.discount(state["ru"] / state["rb"], state["basket_usd"],
                                                                 state["fee"]), self.p) if adaptive else 0.)
        drawn                   = self._drawn(d)
        if share > 0 and drawn > PARITY:
            target              = int(drawn * share)
            repaid              = 0
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
                pending_target  = target - repaid
                got             = self._buy_cash(d, spend, min_net, for_repayment=True, repayment_target=pending_target)
                retired         = self._retire_acquired(d, pending_target, got)
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

    ACT_TURNS                   = 6                     # known progress a day; an unknown outcome stops at once

    def _ensure_trading(self, d):
        """The main account's one-time approvals for guarded purchases and routed sales."""
        if self._trading_ready:
            return
        for contract, method, args in ((d.usdc, "approve", [d.swap_guard.address, 2**256 - 1]),
                                       (d.buck, "approve", [d.permit2.address, 2**256 - 1]),
                                       (d.permit2, "approve", [d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1])):
            self._proxy_exec(d, contract.address, contract.encode_abi(method, args=args))
        self._trading_ready     = True

    def _net_uncertain(self):
        """An outcome that must be reconciled before this household sends anything more."""
        m, cal                  = self.refinance_machine, self.calendar_machine
        return bool(self._net_trade_uncertain or self._net_cancel_uncertain or self._net_pending_token is not None or
                    m.flow is not None and m.flow["status"] == "pending" or
                    cal.flow is not None and cal.flow["status"] == "pending")

    def _act_net(self, d, day, ctr):
        """One day of a canonical household, on the one owner dispatcher and receipt cursor.

        Order: the calendar's bills; then a leaving household's exit, which
        preempts the rest; otherwise a known refused deposit before any new
        credit, the day's credit decision (a debtor's refinancing, a
        mortgage-free household's credit investment, or the investor's own
        choice), the working balance after a payday, and surplus. Each
        dispatcher recovers earlier evidence first and makes at most one fresh
        decision a day. Known multi-step progress then gets a small bounded
        number of turns; an unknown outcome stops everything at once. A day
        with nothing due costs one clock read.
        """
        # Earlier evidence first: a recovered or uncertain outcome consumes the turn, as it always has.
        if self._recover_pending_operations(d, ctr, day):
            return
        m, cal                  = self.refinance_machine, self.calendar_machine
        cal.prepare(d, day)                             # bind on arrival; accrue; schedule a payday's bills
        if (not cal.active and not m.active and not m.settlement_due and not m.surplus_due and
                self._net_repayment_target is None and self._net_repayment_reserve is None and
                not self._net_trade_uncertain and not self._net_cancel_uncertain and self._net_pending_token is None and
                not self._net_repayment_interrupted and not self.exit_due(day) and
                day < min(self._next_decision, self._next_surplus, self._next_wallet)):
            return
        try:
            self._ensure_trading(d)
            if self.exit_due(day):
                for _ in range(self.ACT_TURNS):
                    if not self.advance_net_exit(d, day, ctr) or self._net_uncertain():
                        break
                return
            if self.investor:
                steps           = [self.advance_net_investor]
            else:
                credit          = (self.advance_net_refinancing if self.p["role"] == "debtor" and
                                   (self.mortgage or m.settlement_due) else self.advance_net_investment)
                # Issued or saved BUCK still waiting for a refused deposit goes in before any new credit.
                steps           = ([self.advance_net_surplus] if m.surplus_due else []) + [
                                   credit, self.advance_net_wallet, self.advance_net_surplus]
            for step in steps:
                step(d, day, ctr)
                if self._net_uncertain():
                    return
            for _ in range(self.ACT_TURNS):
                if not (m.active or cal.active or self._net_repayment_target is not None) or self._net_uncertain():
                    break
                if not self._recover_pending_operations(d, ctr, day, continue_after_calendar=True):
                    break
        except NotImplementedError:
            raise                                       # a migration gate is fatal to the run
        except Exception as exc:
            # Each component saved its own evidence before sending; an unexpected fault here is recorded
            # and counted (a campaign fails on any), and this household tries again tomorrow.
            self._net_dispatching = False
            self.last_action, self.last_error = "act-unavailable", f"{type(exc).__name__}: {exc}"[:200]
            ctr["household_err"] = self.last_error
            ctr["household_faults"] = ctr.get("household_faults", 0) + 1
            m._event(d, day, "unavailable", kind="act", phase="dispatch", reason=self.last_error)

    def act(self, d, scenario, day, tick, ctr):
        if tick or day < self.arrive_day or self.exited:
            return
        if getattr(d, "buck_accounting", "") == "net":
            return self._act_net(d, day, ctr)
        if self._recover_pending_operations(d, ctr, day):
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
        arrears                 = self.living_arrears + self.premium_arrears
        hypo_arrears            = self.hypo_living_arrears + self.hypo_premium_arrears
        if getattr(d, "buck_accounting", "") == "net":
            from alberta_buck.sim.net_snapshot import household_mark
            st                  = self._household_fields() | household_mark(d, self)
            pending, held       = st["pending_usd"], st["held_buck"]
            wallet, insurance_age = st["wallet_buck"], st["insurance_age"]
            financial           = st["nw"]
        else:
            st                  = super().octl_state(d)
            if st is None:
                return None
            spot                = buck_usd6(d.chain, d.pool_ub, d.buck) / PARITY
            pending             = 0
            if self._savings_proxy is not None:
                token           = d.tokens[self._token_idx]
                held            = d.chain.balance_of(token, self._savings_proxy.address)
                ru, rt          = active_reserves(d.chain, d.pool_usdc[self._token_idx], d.usdc, token)
                pending         = int(held * ru / rt) if rt else 0
            basket              = st["basket_mark"] if st["basket_mark"] is not None else st["basket"]
            insurance_age       = 0
            if st["deposit"]:
                raw             = d.buck.functions.rawBalanceOf(d.pool_acct).call()
                fee             = d.buck.functions.feeOwing(d.pool_acct).call()
                insurance_age   = min(st["deposit"], fee * st["deposit"] // raw) if raw else st["deposit"]
            held                = max(0, d.buck.functions.signedBalanceOf(self.proxy.address).call())
            wallet              = (d.buck.functions.balanceOf(self._wallet.address).call() if self._wallet is not None else 0)
            financial           = st["cash"] + pending - self.mortgage - arrears + int(
                (basket + st["deposit"] - insurance_age - st["drawn"] + st["jub"] + st["savings_held"] + held + wallet) * spot)
        st.update(nw=financial, hypo=self.hypo_cash - self.hypo_mortgage - hypo_arrears,
                  hypo_cash=self.hypo_cash, hypo_mortgage=self.hypo_mortgage, arrears=arrears,
                  hypo_arrears=hypo_arrears, external_paid=self.external_paid, hypo_paid=self.hypo_paid,
                  insurance_age=insurance_age, exited=self.exited, withdrawn_usd=self.withdrawn_usd, held_buck=held,
                  wallet_buck=wallet, wallet_bought=self.wallet_bought,
                  refusals=self.refusals,
                  active=self._started, role=self.p["role"], member=self.member, action=self.last_action,
                  issue_action=self.last_issue, surplus_action=self.last_surplus,
                  funding_pending=self.funding_pending,
                  refinance_closed=self.refinance_closed,
                  pending_usd=pending, invested_usd=self.invested_usd, issued_invest=self.issued_invest,
                  external_interest=self.external_interest, hypo_interest=self.hypo_interest,
                  income_total=self.income_total, living_paid=self.living_paid, hypo_living=self.hypo_living,
                  decision=self._last_verdict, error=self.last_error, arrive_day=self.arrive_day)
        st.update(self.exit_state())
        if self.lifecycle:
            st.update(age=self.p["age"], usd_fund=self.usd_fund, usd_fund_paid=self.usd_fund_paid)
        if getattr(d, "buck_accounting", "") == "net":
            st["machine"] = self.refinance_machine.status()
            st["calendar"] = self.calendar_machine.status()
        return st

    def exit_state(self):
        """Cheap live lifecycle fields: unlike account marks, these must not be five days stale."""
        return dict(exit_started=self.exit_started, exit_stage=self.exit_stage, exit_retries=self.exit_retries,
                    exit_short=self.exit_short, released=self._released, exited=self.exited,
                    action=self.last_action, error=self.last_error)

    def basket_position(self, share_price):
        """Legacy cached share-price view; canonical octl_state reads exact receipts."""
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


# The decision settings an operator may change on a present household (the household_settings
# intervention): each is read when the household decides, or mirrored on the agent below. Bounds:
# (lo, hi) inclusive. Home, mortgage, income, role and payday are fixed at arrival.
LIVE_SETTINGS                   = dict(utilization=(0., 1.), carry_utilization=(0., 1.), theta=(0., 50.),
                                       risk=(0., .2), issue_premium=(0., .2), retire_disc=(0., .5),
                                       bvib_tolerance=(0., .2), decision_days=(1., 365.),
                                       money_months=(0., 24.), max_impact_bp=(0., 10000.))
LIVE_MIRRORS                    = dict(theta="theta", max_impact_bp="max_impact_bp", surplus_policy="surplus_policy")


def apply_live_settings(agent, settings: dict) -> str:
    """Change a present household's decision settings, all or none. Returns what changed."""
    checked                     = {}
    for key, value in settings.items():
        if key == "surplus_policy":
            if value not in policy.POLICIES:
                raise ValueError(f"surplus_policy must be one of {policy.POLICIES}")
            checked[key]        = value
            continue
        if key not in LIVE_SETTINGS:
            raise ValueError(f"{key} is not a live setting; live: {sorted([*LIVE_SETTINGS, 'surplus_policy'])}")
        lo, hi                  = LIVE_SETTINGS[key]
        v                       = float(value)
        if not (math.isfinite(v) and lo <= v <= hi):
            raise ValueError(f"{key} must be within [{lo:g}, {hi:g}]")
        checked[key]            = v
    for key, v in checked.items():
        agent.p[key]            = v
        if key in LIVE_MIRRORS:
            setattr(agent, LIVE_MIRRORS[key], v)
    return ", ".join(f"{k} {v:g}" if isinstance(v, float) else f"{k} {v}" for k, v in checked.items()) or "no change"


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

    def add_member(self, d, scenario, rng, agents, arbs, role, day, overrides=None):
        """One household arrives now, by an operator's hand (the add_household intervention): the
        population's settings with =overrides= over them, named <population>/<role>/op<k> so its keyed
        draws never meet an arrival's. Returns its member name."""
        if role not in policy.ROLES:
            raise ValueError(f"role must be one of {policy.ROLES}")
        unknown                 = set(overrides or {}) - set(policy.DEFAULTS) - {"role"}
        if unknown:
            raise ValueError(f"unknown household settings: {sorted(unknown)}")
        if len(self.members) >= self.pop["max_members"]:
            raise ValueError("household arrivals exceed max_members; raise the bound or lower the rates")
        k                       = sum(1 for a in self.members if a.member.startswith(f"{self.population}/{role}/op"))
        agent                   = ROLE_CLASSES[role](max(a.idx for a in agents) + 1)
        agent.settings          = self.settings | dict(overrides or {}) | {"role": role}
        agent.member            = f"{self.population}/{role}/op{k}"
        agent.arrive_day        = day
        agent.setup(d, scenario, rng)                   # draws and validates its settings; may refuse
        agents.append(agent)
        arbs.append(agent)
        self.members.append(agent)
        return agent.member

    def summary(self):
        s                       = self.state
        out                     = dict(average=s["average"], gap=s["gap"], k_factor=s["k_factor"],
                                       multiplier=dict(s["multiplier"]))
        for role in policy.ROLES:
            mine                = [a for a in self.members if a.p["role"] == role]
            out[role]           = dict(arrived=len(mine), active=sum(a._started and not a.exited for a in mine),
                                       exited=sum(a.exited for a in mine))
        return out
