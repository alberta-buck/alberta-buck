"""Households and insured savers with portable decisions and separate, real contract accounts."""

from alberta_buck.sim import carry, household_policy as policy
from alberta_buck.sim.agents import Agent, _register
from alberta_buck.sim.equilibrium_agents import BuckCreditDebtorAgent, PARITY, mint_amount, mint_capacity
from alberta_buck.sim.experiment import spec
from alberta_buck.sim.gauge import active_reserves, buck_usd6


@_register
class CreditHouseholdAgent(BuckCreditDebtorAgent):
    """One insured household. Legacy debtor methods supply only contract plumbing, never its decisions."""

    def setup(self, d, scenario, rng):
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
        self._face0             = int(p["home_k"] * 1000 * PARITY)
        self._face              = self._face0
        self.mortgage           = int(self._face0 * p["ltv"]) if p["role"] == "debtor" else 0
        self._mortgage0         = self.mortgage
        self.hypo_mortgage      = self.mortgage
        self.hypo_cash          = 0
        self.income_annual      = int(self._face0 * p["income_frac"])
        self.apr                = p["apr"]
        rate                    = self.apr / 12
        self.payment            = (int(self.mortgage * rate / (1 - (1 + rate) ** -300))
                                   if rate else self.mortgage // 300)
        self.premium_rate       = p["premium_bp"]
        self.cash_buffer        = int(p["buffer_k"] * 1000 * PARITY)
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
        self._last_verdict      = None
        self.last_action        = "waiting"
        self.last_issue         = "waiting"
        self.last_surplus       = "waiting"
        self.last_error         = ""
        self._token_idx         = int(policy.uniform(scenario.seed, self.member, "token") * len(d.tokens))
        self._bind_proxy(d)
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
        living                  = int(income * self.p["living_frac"])
        self.living_arrears    += living
        paid                    = self._pay_bank(d, self.living_arrears)
        self.living_arrears    -= paid
        self.living_paid       += paid
        self.hypo_living_arrears += living
        paid                    = min(self.hypo_living_arrears, self.hypo_cash)
        self.hypo_living_arrears -= paid
        self.hypo_cash         -= paid
        self.hypo_living       += paid
        premium                 = self._face0 * self.premium_rate // 10_000 // 12
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
        signed                  = d.buck.functions.signedBalanceOf(self.proxy.address).call()
        unactivated             = sum(max(0, f - a) for f, a, _ in (
            d.credit.functions.creditInfo(t).call() for t in self._token_ids))
        ru, rb                  = active_reserves(d.chain, d.pool_ub, d.usdc, d.buck)
        month                   = (day - self.arrive_day) // 30
        return dict(mortgage=self.mortgage / PARITY, face=self._face0 / PARITY, joined=self.joined,
                    k=d.kctrl.functions.buckK().call() / 1e18, drawn=max(0, -signed) / PARITY,
                    spendable=d.buck.functions.balanceOf(self.proxy.address).call() / PARITY,
                    unactivated=unactivated / PARITY, ru=ru / PARITY, rb=rb / PARITY, fee=d.fee_ub / 1e6,
                    basket_usd=self._basket_usd(d), at_renewal=(month - self.p["renew_in"]) % self.p["term_months"] == 0)

    def _buy_cash(self, d, budget):
        before                  = d.buck.functions.signedBalanceOf(self.proxy.address).call()
        cash                    = d.chain.balance_of(d.usdc, self.proxy.address)
        self._swap_via_simlp(d, d.pool_ub, d.usdc, min(cash, budget), self.proxy.address)
        got                     = d.buck.functions.signedBalanceOf(self.proxy.address).call() - before
        spent                   = cash - d.chain.balance_of(d.usdc, self.proxy.address)
        self.trade_loss         += spent - got
        return got

    def _issue(self, d, state, kind, v, ctr):
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
        before                  = d.chain.balance_of(d.usdc, self.proxy.address)
        signed                  = d.buck.functions.signedBalanceOf(self.proxy.address).call()
        self._sell_capped(d, d.pool_ub, want)
        sold                    = signed - d.buck.functions.signedBalanceOf(self.proxy.address).call()
        got                     = d.chain.balance_of(d.usdc, self.proxy.address) - before
        if sold <= 0:
            return
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
            self.last_error     = repr(exc)[:200]
            ctr["household_err"] = self.last_error

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
        for log in receipt["logs"]:
            if log["topics"][0] == d.deposited_topic:
                rid             = int.from_bytes(log["topics"][2], "big")
                principal       = d.basket.functions.deposits(rid).call()[0]
                self._basket_receipts.append(rid)
                self._basket_in += principal
                if str(d.basket_impl).startswith("equity"):
                    self._basket_shares += d.basket.functions.holdings(rid).call()[0]
                ctr["bcdInvested"] = ctr.get("bcdInvested", 0) + principal
                ctr["bcdInvestments"] = ctr.get("bcdInvestments", 0) + 1

    def _surplus(self, d, state, ctr):
        p                       = self.p
        cash                    = d.chain.balance_of(d.usdc, self.proxy.address)
        spare                   = max(0, cash - self.cash_buffer)
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
                got             = self._buy_cash(d, min(int(budget * PARITY), cost))
                self.unwound   += got
                ctr["bcdRetired"] = ctr.get("bcdRetired", 0) + got
        self.last_action        = action

    def act(self, d, scenario, day, tick, ctr):
        if tick or day < self.arrive_day:
            return
        if not self._started:
            cash                = int(self.p["cash_k"] * 1000 * PARITY)
            d.chain.send(d.usdc.functions.mint(self.proxy.address, cash))
            self.hypo_cash      = cash
            self._started      = self._growth_arrived = True
        self._advance(d, day)
        if day < self._next_decision:
            return
        period                  = self.p["decision_days"]
        self._next_decision    += ((day - self._next_decision) // period + 1) * period
        self.last_error         = ""
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
        financial               = st["cash"] + pending - self.mortgage + int(
            (basket + st["deposit"] - insurance_age - st["drawn"] + st["jub"] + st["savings_held"] + held) * spot)
        arrears                 = self.living_arrears + self.premium_arrears
        hypo_arrears            = self.hypo_living_arrears + self.hypo_premium_arrears
        st.update(nw=financial - arrears, hypo=self.hypo_cash - self.hypo_mortgage - hypo_arrears,
                  hypo_cash=self.hypo_cash, hypo_mortgage=self.hypo_mortgage, arrears=arrears,
                  hypo_arrears=hypo_arrears, external_paid=self.external_paid, hypo_paid=self.hypo_paid,
                  insurance_age=insurance_age,
                  active=self._started, role=self.p["role"], member=self.member, action=self.last_action,
                  issue_action=self.last_issue, surplus_action=self.last_surplus,
                  pending_usd=pending, invested_usd=self.invested_usd, issued_invest=self.issued_invest,
                  external_interest=self.external_interest, hypo_interest=self.hypo_interest,
                  income_total=self.income_total, living_paid=self.living_paid, hypo_living=self.hypo_living,
                  decision=self._last_verdict, error=self.last_error, arrive_day=self.arrive_day)
        return st


@_register
class CreditSaverAgent(CreditHouseholdAgent):
    def setup(self, d, scenario, rng):
        self.settings           = getattr(self, "settings", {}) | {"role": "saver"}
        super().setup(d, scenario, rng)


@_register
class HouseholdPopulationAgent(Agent):
    """Expand into separate accounts once, keeping ordinary loop, telemetry and accounting semantics."""

    @classmethod
    def expand(cls, scenario, population, first_idx):
        name                    = cls.__name__
        get                     = lambda key, default: spec(scenario, name, key, default)
        schedule                = policy.arrivals(scenario.seed, population, scenario.days,
                                                  get("initial", 10), get("arrival_rate", 6.),
                                                  get("max_members", 256))
        settings                = {k: get(k, v) for k, v in policy.DEFAULTS.items()}
        members                 = []
        share                   = float(get("saver_share", .5))
        if not 0 <= share <= 1:
            raise ValueError("saver_share must be between zero and one")
        for i, day in enumerate(schedule):
            member              = f"{population}/{i}"
            saver               = policy.uniform(scenario.seed, member, "role") < share
            agent               = (CreditSaverAgent if saver else CreditHouseholdAgent)(first_idx + i)
            agent.settings      = settings | {"role": "saver" if saver else "debtor"}
            agent.arrive_day     = day
            agent.member        = member
            members.append(agent)
        return members
