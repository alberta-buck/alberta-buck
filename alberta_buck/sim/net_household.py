"""Checkpointed household refinancing on canonical Buck.

An assessed plan is an economic input, not authorization. The existing SDK
rechecks the selected facility, guards the sale and authenticates each receipt.
Only a confirmed bank payment changes the external mortgage. A lost reply
stops at that exact phase until its saved receipt can be reconciled. This
assessor chooses among bounded actual candidates using explicit forecasts;
cash-flow scheduling lives in the existing household calendar component.

The append-only public events use decimal strings for money, with stable
decision/sequence identifiers. They are suitable for saved JSON and live UI
consumers; recording an unavailable account observation never invents a zero
or reverses an already applied financial transition.
"""
from dataclasses import asdict, dataclass, replace
import math
from alberta_buck.sim import carry, household_policy as policy
from alberta_buck.sim.household_policy import external_premium_due

from buck_core import (IssuancePending, IssueCandidate, TransactionReceiptPending, TransactionReverted, NetBasketOps,
                       mark_net_basket_positions, observe_net_market)
from buck_core.session import _txhex

RAW = 10**6
PHASES = ("fund", "issue", "export", "mortgage")
SIZE_FRACTIONS = (20, 15, 10, 5, 2)


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


@dataclass(frozen=True)
class SurplusPlan:
    action: str                  # save or retire; never creates credit or pays a bank
    cash_buffer: int
    reserve_floor: int            # all prior Ordinary cash remains protected
    spend_usdc: int = 0
    minimum_net: int = 0
    target_principal: int = 0
    position_id: int = 0          # zero creates; positive saves into the same BUCK receipt
    redeem_shares: int = 0
    minimum_buck: int = 0
    slippage_bp: int = 50


@dataclass(frozen=True)
class WalletPlan:
    action: str                  # buy: dollars -> wallet BUCK; sell: wallet BUCK -> dollars
    wallet: str                   # the working balance's own Ordinary custody account
    cash_buffer: int              # dollars that stay home: bills and the current credit front
    target: int                   # net BUCK wanted in the wallet when assessed
    held: int                     # net BUCK actually there when assessed
    spend_usdc: int = 0
    minimum_net: int = 0
    sell_buck: int = 0
    minimum_usdc: int = 0
    slippage_bp: int = 50


def _basket_ops(d):
    return NetBasketOps(d.chain, d.basket, d.buck, d.net_market_ops.execute, getattr(d, "net_simulate", None))


def _reserve_for(candidate):
    return candidate.reserve_required + (candidate.reserve_buffer if candidate.premium else 0)


def _bank_terms(plan):
    principal = plan.mortgage_limit
    penalty = (principal * plan.penalty_numerator + plan.penalty_denominator - 1) // plan.penalty_denominator
    return dict(amount=principal + plan.switch_cost + penalty, principal=principal,
                switch=plan.switch_cost, penalty=penalty)


def _exportable(held, reserve):
    """Leave 0.1% of projected Ordinary cash owned for execution-time drift.

    This is the existing trade sizing cushion, not a demurrage calculator.
    Actual balanceOf and the original reserve guard still decide execution.
    """
    return max(0, held - reserve - (held + 999) // 1000)


def observe_refinance_forecasts(d):
    """Current fund/age ratio and the explicitly modeled idle-cash baseline.

    This estimates annual opportunity/carry, never actual token balances.
    Main cash and the simulation's uninvested insurer pool decline at 2%
    compounded continuously. Active insurer strategies need another explicit
    forecast. At zero claim age use a conservative zero relief forecast,
    recording that there was no observed rate; never invent a fund mark.
    """
    observed = d.net_credit_ops.quote_global_relief_rate()
    idle_growth = math.expm1(-.02)
    return dict(values=dict(expected_relief_rate=(observed.annual_rate_wad / 10**18
                           if observed.annual_rate_wad is not None else 0.),
        expected_refund_growth=idle_growth, expected_wallet_growth=idle_growth),
        observation=asdict(observed), basis="marked-fund/idle-cash" if observed.annual_rate_wad is not None
        else "no-claim-age/zero-relief/idle-cash")


def assess_refinance(agent, d, day, *, expected_relief_rate, expected_refund_growth,
                     expected_wallet_growth, reserve_floor=None, slippage_bp=50):
    """Read a bounded menu of actual NFT quotes; select a conservative forecast.

    Pre: canonical ordinary household; explicit annual forecasts, serialized
    owner, completed normal keeper/controller work and stable observation
    block. Post: no transaction or ledger change.
    Wallet-only, new issuance and mixed alternatives retain the reserve and
    cash buffer. The default reserve is the household's lasting credit front
    (its last refinance plan's floor), whatever cash work ran since.
    A sale's minimum is the principal planned for the bank;
    switching costs/penalty are funded from old USDC, so never counted twice
    as execution loss. Quote estimates are advisory, and execution rechecks.
    """
    if (getattr(d, "buck_accounting", "") != "net" or agent.refinance_machine.active or
            agent.refinance_machine.settlement_due or
            agent.calendar_machine.active):
        raise ValueError("household refinance assessment needs idle canonical accounting")
    if reserve_floor is None:
        # The lasting issuance front, not the last operation's cursor: a
        # surplus flow temporarily protects ALL prior wallet BUCK, and that
        # must not freeze owned cash out of a later mortgage conversion.
        reserve_floor = agent._net_credit_reserve
    if (type(reserve_floor) is not int or not 0 <= reserve_floor < 2**256 or
            type(slippage_bp) is not int or not 0 <= slippage_bp <= 1000):
        raise ValueError("invalid household assessment reserve or slippage")
    block = d.w3.eth.block_number
    market = observe_net_market(d.chain, d.buck, d.usdc, d.pool_ub, d.fee_ub, block_number=block)
    book = d.net_credit_ops.household(agent.address, agent._token_ids, block_number=block)
    retained = min(reserve_floor, book.spendable)  # an idle ordinary reserve also experiences demurrage
    available = d.net_credit_ops.quote_household_activated_capacity(agent.address, agent._token_ids)
    if available.block_number != block:
        raise ValueError("household assessment block changed")
    cash = d.usdc.functions.balanceOf(agent.address).call(block_identifier=block)
    k = d.kctrl.functions.currentBuckK().call(block_identifier=block)
    bvib = d.basket.functions.basketValueInBuck().call(block_identifier=block)
    month = (day - agent.arrive_day) // 30
    # Reuse the pinned books/market rather than re-reading the entire legacy
    # household state. Financial feasibility remains integer arithmetic.
    state = dict(accounting="net", mortgage=agent.mortgage / RAW, face=agent._face0 / RAW,
        joined=agent.joined, closing_paid=agent.refinance_closed, k=k / 10**18, spendable=book.spendable / RAW,
        unactivated=0 if available.maturities_pending else available.unactivated_present / RAW,
        activated_capacity=available.capacity / RAW,
        issuance_limit=0 if available.maturities_pending else available.shared_limit / RAW,
        at_renewal=(month - agent.p["renew_in"]) % agent.p["term_months"] == 0,
        basket_usd=(bvib / 10**18) * (float(market.price_num) / float(market.price_den)),
        expected_relief_rate=expected_relief_rate, expected_refund_growth=expected_refund_growth,
        expected_wallet_growth=expected_wallet_growth, premium_refund_fraction=1.)
    t = policy.terms(state, agent.p)
    utilization = agent._face0 * k * int(agent.p["utilization"] * 10**9) // (10**18 * 10**9)
    # Half the configured price-impact allowance in active virtual reserves.
    impact_ppm = int(agent.max_impact_bp * 50)
    buck_cap = market.reserve_buck * impact_ppm // 10**6
    cash_cap = market.reserve_usdc * impact_ppm // 10**6
    if agent.mortgage < market.reserve_usdc:
        buck_cap = min(buck_cap, market.reserve_buck * agent.mortgage * 10**6 //
                       ((market.reserve_usdc - agent.mortgage) * (10**6 - market.fee_ppm)))
    fractions = SIZE_FRACTIONS
    context = dict(block=block, considered=0, issuance_quotes=0, rejections={}, feasible=0,
                   forecasts=dict(relief_rate=expected_relief_rate, refund_growth=expected_refund_growth,
                                  wallet_growth=expected_wallet_growth), protected_buck=reserve_floor)
    plans, values, refunds, programmes, unfunded, funding = [], [], {}, [], [], []
    active = sum(f.activated for f in book.facilities)
    annual = agent._face0 * agent.premium_rate // 10_000

    def reject(reason):
        context["rejections"][reason] = context["rejections"].get(reason, 0) + 1

    def option(candidate, wallet, *, programme=False):
        context["considered"] += 1
        new, premium = (candidate.requested_spendable, candidate.premium) if candidate else (0, 0)
        floor = max(retained, _reserve_for(candidate)) if candidate else retained
        buy = max(0, floor - book.spendable) if candidate else 0
        cost = market.buy_cost(buy)
        if cost is None:
            reject("reserve-liquidity")
            return
        maximum = (cost * (10_000 + slippage_bp) + 9999) // 10_000
        # Issuance/funding and export are separate blocks. Do not plan to
        # spend exactly every last net unit above the protected reserve.
        projected = book.spendable + new + buy
        export = min(new + wallet, _exportable(projected, floor))
        if (export < RAW or (not programme and export > buck_cap) or maximum > cash_cap or
                buy * market.gross_scaled > buck_cap * market.net_scaled):
            reject("trade-size")
            return
        # A programme is a forecast of bounded tranches after route repair,
        # not a single large swap or authorization to issue its full amount.
        leg = min(export, buck_cap)
        if leg == 0:
            reject("trade-size")
            return
        minimum = (market.sell_expected(leg) * (10_000 - slippage_bp) // 10_000) * export // leg
        if not 0 < minimum <= agent.mortgage:
            reject("mortgage-size")
            return
        fixed = int(t.fixed_cost * RAW) if candidate else 0
        pn = 0 if state["at_renewal"] else math.ceil(agent.apr * agent.p["penalty_months"] / 12 * 10**12)
        penalty = (minimum * pn + 10**12 - 1) // 10**12
        cash_front = agent.cash_buffer + maximum + fixed + penalty
        coverage = candidate.after_coverage - candidate.before_coverage if candidate else 0
        fraction = 1.
        saving = 0
        if candidate and premium:
            key = candidate.token_id, candidate.coverage
            if key not in refunds:
                refunds[key] = d.net_credit_ops.quote_premium_refund(*key, block_number=block)
            q = refunds[key]
            if q.refund_fraction_wad is None:
                reject("insurer-refund-unavailable")
                return
            fraction = q.refund_fraction_wad / 10**18
        if candidate and candidate.coverage:
            saving = (external_premium_due(annual, agent._face0, active) -
                      external_premium_due(annual, agent._face0, active + candidate.coverage))
        economics = dict(usd=minimum / RAW, wallet_used=max(0, export - new) / RAW, new_spendable=new / RAW,
            present_coverage=coverage / RAW, premium=premium / RAW, premium_refund_fraction=fraction,
            external_premium_saving=saving / RAW, reserve_cost=(maximum - buy * t.buck_value) / RAW,
            reserve_carry=buy / RAW * t.buck_value * (agent.apr - expected_wallet_growth))
        if programme:
            programmes.append((candidate, economics, export, cash_front))
        else:
            plan = RefinancePlan(candidate, export, minimum, minimum, agent.cash_buffer, floor,
                                 fixed, pn, 10**12, buy, maximum)
            if cash < cash_front:
                reject("cash-buffer")
                unfunded.append((plan, economics, cash_front))
                if carry.refinance_quoted(t, **economics).go:
                    funding.append((cash_front, "worthwhile-leg"))
            else:
                plans.append(plan)
                values.append(economics)

    wallet_limit = min(max(0, book.spendable - retained), buck_cap)
    for wallet in sorted({wallet_limit * f // 20 for f in fractions}, reverse=True):
        option(None, wallet)
    credit_cap = min(max(0, utilization - book.issued), available.shared_limit)
    maximum_new = min(credit_cap, buck_cap)
    activated = dict(available.facilities)

    def issuance_options(cap, *, programme=False):
        for facility in book.facilities:
            sizes = {cap * f // 20 for f in fractions} | {min(cap, activated.get(facility.token_id, 0))}
            for amount in sorted((v for v in sizes if v >= RAW), reverse=True):
                menu = d.net_credit_ops.issue_coverage_menu(agent.address, facility.token_id, amount,
                    facility.face * facility.premium_rate * 10 // 10_000, reserve_buffer=RAW)
                context["issuance_quotes"] += len(menu)
                for candidate in menu:
                    if candidate.block_number != block:
                        raise ValueError("household assessment block changed")
                    if any(r != "reserve-shortfall" for r in candidate.reasons):
                        for reason in candidate.reasons:
                            reject(reason)
                        continue
                    if book.issued + amount + candidate.premium > utilization:
                        reject("utilization")
                        continue
                    floor = max(retained, _reserve_for(candidate))
                    wallet = min(max(0, book.spendable - floor), max(0, buck_cap - amount))
                    for spent in (0,) if programme else sorted({0, wallet}):
                        option(candidate, spent, programme=programme)

    issuance_options(maximum_new)
    index, verdict = carry.best_quoted_refinance(t, values)
    # A first small trade should not have to repay the entire closing fee
    # alone. Require a same-asset programme affordable from current capital
    # and actual credit, scored at today's conservative per-tranche price.
    if index is None and (plans or unfunded) and t.fixed_cost > 0 and buck_cap:
        minimum_leg = market.sell_expected(buck_cap) * (10_000 - slippage_bp) // 10_000
        programme_cap = min(credit_cap, agent.mortgage * buck_cap // minimum_leg) if minimum_leg else 0
        if programme_cap > maximum_new:
            issuance_options(programme_cap, programme=True)
        context["programme_quotes"] = len(programmes)
        context["programme_feasible"] = sum(front <= cash for _, _, _, front in programmes)
        selected_key = None
        legs = [(i, p, v, p.cash_buffer + p.reserve_buy_max_usdc + p.switch_cost + _bank_terms(p)["penalty"])
                for i, (p, v) in enumerate(zip(plans, values))]
        legs += [(None, p, v, front) for p, v, front in unfunded]
        for i, plan, economics, front in legs:
            if plan.candidate is None:
                continue
            for candidate, whole_economics, exported, whole_front in programmes:
                if candidate.token_id != plan.candidate.token_id:
                    continue
                marginal, whole = carry.staged_refinance(t, economics, whole_economics)
                if marginal.go and max(front, whole_front) > cash:
                    funding.append((max(front, whole_front), "worthwhile-programme"))
                if i is None or whole_front > cash:
                    continue  # a savings target never makes an unfunded programme executable
                key = (plan.mortgage_limit, marginal.value, -plan.candidate.premium)
                if marginal.go and (selected_key is None or key > selected_key):
                    index, verdict, selected_key = i, marginal, key
                    context["programme"] = dict(token_id=candidate.token_id, economics=whole_economics,
                        score=asdict(whole), tranches=(exported + buck_cap - 1) // buck_cap,
                        basis="current-credit/capital/per-tranche-price", fixed_cost_paid_now=plan.switch_cost,
                        fixed_cost_allocated=-marginal.parts["fixed"])
    if d.w3.eth.block_number != block:
        raise ValueError("household assessment block changed")
    context["feasible"] = len(plans)
    context["selected"] = None if index is None else dict(economics=values[index], score=asdict(verdict))
    if index is not None:
        p = plans[index]
        retained_cash = book.spendable + (p.candidate.requested_spendable if p.candidate else 0) + p.reserve_buy_net - p.export_buck
        context["selected"].update(post_export_buck_quote=retained_cash,
                                   export_cushion_buck=retained_cash - p.reserve_floor)
    # Savings must not spend the cash needed to front a worthwhile current
    # opportunity. An unfunded target is only a saving obligation, never an
    # executable loan or a promise of future wages/K/prices.
    front = (plans[index].cash_buffer + plans[index].reserve_buy_max_usdc + plans[index].switch_cost +
             _bank_terms(plans[index])["penalty"]) if index is not None else None
    hold = (front, "selected-leg") if front is not None else min(funding) if funding else (agent.cash_buffer, "no-worthwhile-credit")
    context["funding"] = dict(cash_floor=hold[0], basis=hold[1], unfunded=hold[0] > cash,
                              refinance_ready=index is not None)
    context["reason"] = ("worthwhile-staged-refinance" if "programme" in context else
                         "worthwhile-refinance" if index is not None else "no-worthwhile-refinance")
    return dict(plan=plans[index] if index is not None else None, verdict=verdict, context=context)


def assess_remainder(agent, d, day, *, expected_wallet_growth=None, slippage_bp=50):
    """Read-only reassessment of a proved-refused export/bank remainder.

    PRE: serialized ordinary household, prepared/idle calendar and no unknown
    transaction. POST: only unfinished cash operations are proposed, within
    the original export/principal caps, reserve and cash buffer. No credit,
    insurer or fund quote is needed. Prior activation, premium and relief are
    existing holdings, not benefits earned a second time. The entire unpaid
    closing fee remains in the bank cash budget, independent of its sunk-cost
    exclusion from the marginal wallet-export comparison.
    """
    machine = agent.refinance_machine
    if (type(day) is not int or day < 0 or getattr(d, "buck_accounting", "") != "net" or not machine.settlement_due or
            agent.calendar_machine.active):
        raise ValueError("household remainder needs a refused settlement and idle calendar")
    if agent.calendar_machine.bound and agent.calendar_machine.day != day:
        raise ValueError("household calendar books must be prepared before remainder assessment")
    if type(slippage_bp) is not int or not 0 <= slippage_bp <= 1000:
        raise ValueError("invalid household remainder slippage")
    growth = math.expm1(-.02) if expected_wallet_growth is None else expected_wallet_growth
    if not carry._finite_forecast(growth) or growth < -1:
        raise ValueError("invalid expected wallet growth")
    flow, block = machine.flow, d.w3.eth.block_number
    phase, revision = flow["phase"], flow.get("revision", 0)
    origin = flow.get("origin", flow["plan"])
    principal = min(origin.mortgage_limit, agent.mortgage)
    cash = d.usdc.functions.balanceOf(agent.address).call(block_identifier=block)
    closing = 0 if agent.refinance_closed else origin.switch_cost
    base = replace(flow["plan"], candidate=None, mortgage_limit=principal, switch_cost=closing,
                   cash_buffer=max(origin.cash_buffer, agent.cash_buffer), reserve_floor=origin.reserve_floor,
                   penalty_numerator=origin.penalty_numerator, penalty_denominator=origin.penalty_denominator,
                   reserve_buy_net=0, reserve_buy_max_usdc=0)
    context = dict(block=block, phase=phase, revision=revision, usdc=cash, mortgage=agent.mortgage,
        export_cap=origin.export_buck, principal_cap=origin.mortgage_limit, reserve_floor=base.reserve_floor,
        cash_buffer=base.cash_buffer, committed_closing_usdc=closing,
        premium_already_paid=origin.candidate.premium if origin.candidate else 0,
        new_credit=False)
    plan, next_phase = None, phase
    if phase == "mortgage" or not principal:
        # Existing USDC can settle even if BUCK credit/fund/markets are unavailable.
        next_phase = "mortgage"
        terms = _bank_terms(base)
        context.update(basis="existing-exported-usdc", bank=terms,
                       reason="ready" if not terms["amount"] or cash >= base.cash_buffer + terms["amount"] else "cash-buffer")
        if context["reason"] == "ready":
            plan = base
    else:
        read = lambda fn: fn.call(block_identifier=block)
        if not read(d.reg.functions.isVerified(agent.address)) or read(d.reg.functions.isCarrying(agent.address)):
            raise ValueError("household remainder needs a verified ordinary wallet")
        held = read(d.buck.functions.balanceOf(agent.address))
        market = observe_net_market(d.chain, d.buck, d.usdc, d.pool_ub, d.fee_ub, block_number=block)
        fair = read(d.basket.functions.basketValueInBuck()) / 10**18 * (market.price_num / market.price_den)
        t = carry.RefiTerms(debt=agent.mortgage / RAW, rate=agent.apr, face=agent._face0 / RAW,
            premium_bp=agent.premium_rate, joined=True, payback_y=agent.p["theta"], risk=agent.p["risk"],
            headroom=held / RAW, buck_value=fair, penalty_months=0, fixed_cost=0,
            expected_relief_rate=0, expected_refund_growth=0, expected_wallet_growth=growth,
            wallet_already_issued=True, issuance_limit=0)
        # Same 0.1% sizing cushion used by the existing trade policies. It stays
        # owned cash; it is not another demurrage calculation or trading fee.
        available = _exportable(held, base.reserve_floor)
        cap = min(origin.export_buck, available, market.reserve_buck * int(agent.max_impact_bp * 50) // 10**6)
        target = (principal * 10_000 + 9999 - slippage_bp) // (10_000 - slippage_bp)
        needed = market.sell_input(target) if target < 2**256 else None
        if needed is not None:
            cap = min(cap, needed)
        context.update(basis="existing-buck/marginal", held_buck=held, available_buck=available,
                       export_budget=cap, expected_wallet_growth=growth, rejections={},
                       reason="no-worthwhile-remainder" if cap else "export-budget")
        selected = None
        for amount in sorted({cap * f // 20 for f in SIZE_FRACTIONS}, reverse=True):
            minimum = market.sell_expected(amount) * (10_000 - slippage_bp) // 10_000
            retired = min(principal, minimum)
            if not amount or not retired:
                continue
            candidate = replace(base, export_buck=amount, minimum_usdc=minimum, mortgage_limit=retired)
            bank = _bank_terms(candidate)
            if cash + minimum < candidate.cash_buffer + bank["amount"]:
                context["rejections"]["cash-buffer"] = context["rejections"].get("cash-buffer", 0) + 1
                continue
            # Original penalty terms are retained exactly, including rounding.
            v = carry.refinance_quoted(t, retired / RAW, amount / RAW, 0, 0, 0)
            penalty = bank["penalty"] / RAW
            v.parts["penalty"] = -penalty
            v.one_time += penalty
            v.value -= penalty
            v.go = v.go and v.value > 0
            if not v.go:
                context["rejections"]["economics"] = context["rejections"].get("economics", 0) + 1
            key = retired, v.value, -amount
            if v.go and (selected is None or key > selected):
                plan, selected = candidate, key
                context.update(reason="ready", score=asdict(v), bank=bank)
    if (d.w3.eth.block_number != block or machine.flow is not flow or not machine.settlement_due or agent.calendar_machine.active or
            flow["phase"] != phase or flow.get("revision", 0) != revision):
        raise ValueError("household remainder observation changed")
    return dict(plan=plan, phase=next_phase, context=context)


def assess_surplus(agent, d, day, *, forecasts=None):
    """Read-only, same-block saving/paydown choice after recurring work.

    Keep the current credit cash target, every pre-existing wallet BUCK and
    actual pending intents. New retirement money comes from an authenticated
    net purchase or the real basket exit; existing liabilities/reserves are
    never confused with spendable credit. No new fund/oracle/exit policy.

    Fair value is the basket's price in BUCK times the SAME dollar pool's
    spot, so that pool's price cancels: the measured discount is basket
    purchasing power, net of the pool's carried fees and trading fee. One
    dislocated dollar route is therefore not a retirement signal (route
    arbitrage owns that repair); BUCK cheaper against the basket itself is.
    """
    m, cal = agent.refinance_machine, agent.calendar_machine
    if (type(day) is not int or day < 0 or getattr(d, "buck_accounting", "") != "net" or m.active or
            m.settlement_due or cal.active or not cal.bound or cal.day != day):
        raise ValueError("household surplus needs prepared, idle canonical accounting")
    if (agent._savings_proxy is not None or agent._net_repayment_target is not None or
            agent._net_trade_uncertain or agent._net_cancel_uncertain or agent._net_pending_token is not None or
            agent._net_repayment_interrupted or m.surplus_due):
        raise ValueError("household custody/repayment must be reconciled before surplus")
    block = d.w3.eth.block_number
    read = lambda fn: fn.call(block_identifier=block)
    if any(read(d.buck.functions.reductionAuthorization(t))[0] for t in agent._token_ids):
        raise ValueError("household repayment authorization remains pending")
    cash = read(d.usdc.functions.balanceOf(agent.address))
    held = read(d.buck.functions.balanceOf(agent.address))
    book = d.net_credit_ops.household(agent.address, agent._token_ids, block_number=block)
    q = observe_net_market(d.chain, d.buck, d.usdc, d.pool_ub, d.fee_ub, block_number=block)
    fair_num, fair_den = read(d.basket.functions.basketValueInBuck()) * q.price_num, 10**18 * q.price_den
    credit = credit_cash_floor(agent, d, day, block, forecasts)
    floor = max(agent.cash_buffer, credit["cash_floor"])
    choice = policy.net_surplus_purchase(q, cash, book.issued, fair_num, fair_den, floor, agent.p)
    context = dict(block=block, cash=cash, protected_buck=held, credit_funding=credit, choice=choice,
                   fair_num=fair_num, fair_den=fair_den, fair_basis="basket-in-buck x dollar-pool spot",
                   reason=choice["reason"])
    plan = None
    if choice["action"] == "retire" and agent._basket_receipts:
        positions = mark_net_basket_positions(d.chain, d.basket, d.buck, agent.address, agent._basket_receipts, block_number=block)
        context["basket_exits"] = []
        for p in positions.positions:
            if not p.mark or not p.shares:
                continue
            amount = min(p.shares, (choice["target_principal"] * p.shares + p.mark - 1) // p.mark)
            if not amount:
                continue
            try:
                exit_ = _basket_ops(d).preview_redemption(agent.address, p.token_id, amount)
                minimum = exit_["paid"] * 9950 // 10_000
                if exit_["block_number"] != block:
                    raise ValueError("basket exit observation changed")
                if minimum >= RAW:
                    plan = SurplusPlan("retire", floor, held, target_principal=choice["target_principal"],
                                       position_id=p.token_id, redeem_shares=amount, minimum_buck=minimum)
                    context.update(reason="redeem-to-retire", exit=exit_)
                    break
            except Exception as exc:
                context["basket_exits"].append(dict(token_id=p.token_id, unavailable=str(exc)[:200]))
    if plan is None and choice["spend_usdc"]:
        tid = 0
        if choice["action"] == "save":
            for existing in agent._basket_receipts:
                if read(d.basket.functions.deposits(existing))[2].lower() == d.buck.address.lower():
                    tid = existing
                    break
        plan = SurplusPlan(choice["action"], floor, held, choice["spend_usdc"], choice["minimum_net"],
                           choice["target_principal"], tid)
        context["reason"] = "purchase-to-" + choice["action"]
    if d.w3.eth.block_number != block:
        raise ValueError("household surplus observation changed")
    return dict(plan=plan, context=context)


def credit_cash_floor(agent, d, day, block, forecasts=None):
    """Dollars a discretionary operation must leave: buffer and credit front.

    A debtor's front comes from the whole refinance assessor. That read is
    reused when the same day's assessment was made at this same block (a
    declined decision sends nothing), so saving and working money after it
    do not assess twice. A mortgage-free household keeps only its buffer.
    """
    if not agent.mortgage:
        return dict(cash_floor=agent.cash_buffer, basis="no-external-mortgage", unfunded=False, refinance_ready=False)
    key = (block, day, tuple(sorted((forecasts or {}).items())))
    cached = getattr(agent, "_net_funding", None)
    if cached is None or cached[0] != key:
        funding = agent.assess_net_refinance(d, day, **(forecasts or {}))["context"]["funding"]
        agent._net_funding = cached = (key, funding)
    return cached[1]


def assess_wallet(agent, d, day, *, forecasts=None):
    """Read-only payday choice for the household's BUCK working balance.

    The wallet is ordinary money held apart from the credit account, sized
    by household_policy.money_target and bought or trimmed in one capped
    bite. It spends only dollars above the buffer and current credit front,
    never the main account's BUCK, and neither issues nor retires principal.
    The same basket-relative discount as saving enlarges the target.
    """
    m, cal = agent.refinance_machine, agent.calendar_machine
    if (type(day) is not int or day < 0 or getattr(d, "buck_accounting", "") != "net" or m.active or
            m.settlement_due or m.surplus_due or cal.active or not cal.bound or cal.day != day):
        raise ValueError("household wallet needs prepared, idle canonical accounting")
    if (agent._net_repayment_target is not None or agent._net_trade_uncertain or agent._net_cancel_uncertain or
            agent._net_pending_token is not None or agent._net_repayment_interrupted):
        raise ValueError("household repayment must be reconciled before working money")
    spending = agent.income_annual * agent.p["living_frac"] / RAW
    if agent.investor or agent._sold or spending <= 0 or agent.p["money_months"] <= 0:
        return dict(plan=None, context=dict(reason="no-working-balance"))
    block = d.w3.eth.block_number
    read = lambda fn: fn.call(block_identifier=block)
    wallet = agent._wallet.address if agent._wallet is not None else ""
    cash = read(d.usdc.functions.balanceOf(agent.address))
    held = read(d.buck.functions.balanceOf(wallet)) if wallet else 0
    q = observe_net_market(d.chain, d.buck, d.usdc, d.pool_ub, d.fee_ub, block_number=block)
    bvib = read(d.basket.functions.basketValueInBuck())
    # cost/fair of a marginal net BUCK; the dollar pool's own price cancels.
    ratio = (q.gross_scaled * 10**6 * 10**36 // (q.net_scaled * (10**6 - q.fee_ppm) * bvib)) if q.net_scaled and bvib else None
    target = policy.net_wallet_target(ratio, day - agent.arrive_day, spending, agent.p)
    credit = credit_cash_floor(agent, d, day, block, forecasts)
    floor = max(agent.cash_buffer, credit["cash_floor"])
    choice = policy.net_wallet_adjustment(q, cash, held, target, floor, agent.p)
    context = dict(block=block, cash=cash, wallet=wallet, held=held, target=target, credit_funding=credit, choice=choice,
                   marginal_ratio_wad=ratio, fair_basis="basket-in-buck x dollar-pool spot", reason=choice["reason"])
    plan = None
    if choice["action"] in ("buy", "sell"):
        plan = WalletPlan(choice["action"], wallet, floor, target, held, choice["spend_usdc"], choice["minimum_net"],
                          choice["sell_buck"], choice["minimum_usdc"])
    if d.w3.eth.block_number != block:
        raise ValueError("household wallet observation changed")
    return dict(plan=plan, context=context)


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

    @property
    def settlement_due(self):
        return self.flow is not None and self.flow["status"] == "refused" and self.flow["phase"] in ("export", "mortgage")

    @property
    def payment_due(self):
        """A failed bank leg still owns the confirmed issue/export proceeds."""
        return self.settlement_due and self.flow["phase"] == "mortgage"

    @property
    def surplus_due(self):
        return bool(self.flow and self.flow.get("kind") == "surplus" and
                    self.flow["status"] == "refused" and self.flow["phase"] == "deposit")

    def _label(self):
        kind = self.flow.get("kind") if self.flow else None
        return kind if kind in ("surplus", "wallet") else "refinance"

    def retirement(self):
        """The selected-facility engine's retained intent; None when idle.

        A funded target is an intention with protected prior cash, not a
        burn. ``uncertain`` means an outcome must be reconciled before any
        further send; ``waiting`` is a known refusal to be re-read later.
        """
        a = self.agent
        pending = None
        if a._net_pending_token is not None:
            pending = {"token_id": a._net_pending_token, "principal": a._net_pending_principal,
                       "phase": a._net_pending_phase, "hash": a._net_pending_hash, "reverted": a._net_pending_reverted,
                       "authorization_confirmed": a._net_authorization_confirmed}
        uncertain = bool(a._net_trade_uncertain or a._net_cancel_uncertain or pending is not None)
        if a._net_repayment_target is None and a._net_repayment_reserve is None and not uncertain:
            return None
        return {"target": a._net_repayment_target, "reserve": a._net_repayment_reserve, "uncertain": uncertain,
                "waiting": bool(a._net_repayment_interrupted), "pending": pending,
                "cancel_hash": a._net_cancel_hash if a._net_cancel_uncertain else None}

    def status(self):
        f = self.flow
        return {"schema": 1, "decision": self.decisions, "status": f["status"] if f else "idle",
                "phase": f["phase"] if f else None, "hash": f.get("hash") if f else None,
                "payment_due": self.payment_due, "settlement_due": self.settlement_due,
                "kind": f.get("kind", "refinance") if f else None, "surplus_due": self.surplus_due,
                "last_surplus_day": self.agent._net_surplus_day, "next_surplus_day": self.agent._next_surplus,
                "next_wallet_day": self.agent._next_wallet,
                "last_attempt_day": self.agent._net_refinance_day, "next_decision_day": self.agent._next_decision,
                "credit_reserve": self.agent._net_credit_reserve, "retirement": self.retirement(),
                "revision": f.get("revision", 0) if f else 0, "events": len(self.events)}

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
            kind = self.flow.get("kind") if self.flow else None
            surplus = kind == "surplus"
            buffer = self.flow["plan"].cash_buffer if self.flow and kind not in ("surplus", "wallet") else a.cash_buffer
            reserve = a._net_credit_reserve
            # One surplus operation's own floors; never the lasting policy.
            protection = ({"usdc": self.flow["plan"].cash_buffer, "buck": self.flow["plan"].reserve_floor}
                          if surplus else None)
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
                          "external_interest": a.external_interest, "income_total": a.income_total,
                          "living_paid": a.living_paid, "external_premium_paid": a.ext_premium,
                          "living_arrears": a.living_arrears, "premium_arrears": a.premium_arrears,
                          "counterfactual_cash": a.hypo_cash, "counterfactual_mortgage": a.hypo_mortgage,
                          "counterfactual_interest": a.hypo_interest, "counterfactual_paid": a.hypo_paid,
                          "calendar": a.calendar_machine.status(),
                          "switch_paid": a.switch_paid, "penalty_paid": a.penalty_paid,
                          "refinance_closed": a.refinance_closed,
                          "external_premium_monthly": external_premium,
                          "counterfactual_premium_monthly": monthly_premium,
                          "cash_buffer": buffer, "reserve_floor": reserve, "surplus_protection": protection,
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
        if f and f.get("kind") in ("surplus", "wallet"):
            detail.setdefault("kind", f["kind"])
        self.events.append({"schema": 1, "seq": len(self.events) + 1, "member": self.agent.member,
                            "decision": self.decisions, "day": day, "block": block,
                            "phase": f["phase"] if f else "idle", "status": status, "accounts": self._accounts(d),
                            **_wire(detail)})

    def start_wallet(self, d, plan, day, ctr, *, context=None):
        """Start one quoted working-balance trade on the existing receipt cursor.

        PRE: prepared owner/calendar, no earlier outcome or due cash work, and
        an existing bound wallet account with its own trading approvals.
        POST: dollars buy net BUCK delivered to the wallet, or the wallet sells
        BUCK for dollars delivered home. Credit and the main BUCK are untouched.
        """
        if self.active or self.settlement_due or self.surplus_due:
            raise ValueError("household has unfinished cash work")
        if not isinstance(plan, WalletPlan) or plan.action not in ("buy", "sell"):
            raise ValueError("invalid household wallet plan")
        amounts = (plan.cash_buffer, plan.target, plan.held, plan.spend_usdc, plan.minimum_net, plan.sell_buck, plan.minimum_usdc)
        buying = plan.action == "buy"
        a = self.agent
        if (any(type(v) is not int or not 0 <= v < 2**256 for v in amounts) or
                type(plan.slippage_bp) is not int or not 0 <= plan.slippage_bp <= 1000 or
                plan.cash_buffer < a.cash_buffer or a._wallet is None or
                not isinstance(plan.wallet, str) or plan.wallet.lower() != a._wallet.address.lower() or
                buying != bool(plan.spend_usdc and plan.minimum_net) or
                buying == bool(plan.sell_buck and plan.minimum_usdc) or
                buying and (plan.sell_buck or plan.minimum_usdc) or not buying and (plan.spend_usdc or plan.minimum_net)):
            raise ValueError("invalid household wallet bounds")
        self.decisions += 1
        self.flow = dict(kind="wallet", plan=plan, phase=plan.action, status="running", hash=None, terms=None, revision=0)
        self._event(d, day, "planned", plan=asdict(plan), context=context or {}, market=self._market(d))
        return self.advance(d, day, ctr)

    def _prepare_wallet(self, d, day):
        a, p = self.agent, self.flow["plan"]
        if p.action == "buy":
            if d.usdc.functions.balanceOf(a.address).call() < p.cash_buffer + p.spend_usdc:
                self._refuse(d, day, "wallet USDC funding reserve changed")
                return False
        elif d.buck.functions.balanceOf(p.wallet).call() < p.sell_buck:
            self._refuse(d, day, "wallet BUCK balance changed")
            return False
        return True

    def _apply_wallet(self, d, day, result, ctr):
        a, f = self.agent, self.flow
        p, receipt = f["plan"], result.receipt
        if p.action == "buy":
            if not 0 < result.paid <= p.spend_usdc or result.received < p.minimum_net:
                raise ValueError("wallet purchase differs from bounds")
            a.wallet_bought += result.received
            moved = dict(usdc=result.paid, net_buck_scaled=result.net_scaled, nominal_buck=result.nominal_received)
        else:
            if not 0 < result.paid <= p.sell_buck or result.received < p.minimum_usdc:
                raise ValueError("wallet sale differs from bounds")
            a.wallet_sold += result.paid
            moved = dict(buck=result.paid, nominal_buck=result.nominal_paid, usdc=result.received)
        # Dollars against BUCK at one-for-one, the households' existing trade-cost convention.
        a.trade_loss += result.paid - result.received
        f["hash"], f["terms"], f["status"], f["phase"] = None, None, "running", "complete"
        self._event(d, day, "confirmed", operation=p.action, amounts=moved,
                    transaction_hash=_txhex(receipt["transactionHash"]), receipt_block=receipt.get("blockNumber"))

    def start_surplus(self, d, plan, day, ctr, *, context=None):
        """Start one quoted cash-only intention on the existing receipt cursor.

        PRE: prepared owner/calendar, no earlier outcome/credit settlement.
        POST: only owned cash moves; debt changes later through the existing
        selected-facility engine. A save's receipt belongs to this same owner.
        """
        if self.active or self.settlement_due or self.surplus_due:
            raise ValueError("household has unfinished cash work")
        if not isinstance(plan, SurplusPlan) or plan.action not in ("save", "retire"):
            raise ValueError("invalid household surplus plan")
        amounts = (plan.cash_buffer, plan.reserve_floor, plan.spend_usdc, plan.minimum_net,
                   plan.target_principal, plan.position_id, plan.redeem_shares, plan.minimum_buck)
        if (any(type(v) is not int or not 0 <= v < 2**256 for v in amounts) or
                type(plan.slippage_bp) is not int or not 0 <= plan.slippage_bp <= 1000 or
                plan.cash_buffer < self.agent.cash_buffer or
                bool(plan.spend_usdc) != bool(plan.minimum_net) or
                bool(plan.redeem_shares) != bool(plan.minimum_buck) or
                bool(plan.redeem_shares) == bool(plan.spend_usdc) or
                plan.action == "save" and (plan.redeem_shares or plan.target_principal) or
                plan.action == "retire" and not plan.target_principal or
                plan.redeem_shares and not plan.position_id):
            raise ValueError("invalid household surplus bounds")
        a = self.agent
        cash = d.usdc.functions.balanceOf(a.address).call()
        if plan.spend_usdc and cash < plan.cash_buffer + plan.spend_usdc:
            raise ValueError("surplus would spend protected credit capital")
        if d.buck.functions.balanceOf(a.address).call() < plan.reserve_floor:
            raise ValueError("household surplus reserve changed")
        if plan.target_principal > d.net_credit_ops.household(a.address, a._token_ids).issued:
            raise ValueError("surplus retirement exceeds actual principal")
        if plan.position_id and plan.position_id not in a._basket_receipts:
            raise ValueError("household surplus names an untracked receipt")
        self.decisions += 1
        self.flow = dict(kind="surplus", plan=plan, phase="redeem" if plan.redeem_shares else "buy",
                         status="running", hash=None, terms=None, revision=0, acquired=0)
        self._event(d, day, "planned", plan=asdict(plan), context=context or {}, market=self._market(d), basket=self._basket(d))
        return self.advance(d, day, ctr)

    def resume_surplus(self, d, day, ctr):
        if not self.surplus_due:
            raise ValueError("household has no known refused savings deposit")
        self.flow.update(status="running", hash=None, terms=None, revision=self.flow["revision"] + 1)
        self._event(d, day, "resumed", reason="retain purchased cash; re-quote only the deposit")
        return self.advance(d, day, ctr)

    def _prepare_surplus(self, d, day):
        a, f = self.agent, self.flow
        p, phase = f["plan"], f["phase"]
        held = d.buck.functions.balanceOf(a.address).call()
        if phase == "buy":
            if d.usdc.functions.balanceOf(a.address).call() < p.cash_buffer + p.spend_usdc:
                self._refuse(d, day, "surplus USDC funding reserve changed")
                return False
            f["terms"] = dict(preserved=max(held, p.reserve_floor) + 1)
        elif phase == "deposit":
            amount = min(f["acquired"], _exportable(held, p.reserve_floor))
            price = d.basket.functions.sharePrice().call()
            minimum = amount * 10**18 // price * (10_000 - p.slippage_bp) // 10_000 if price else 0
            if not minimum:
                f["phase"], f["retained"] = "complete", True
                self._event(d, day, "retained", reason="owned cash below deposit grain/reserve")
                return False
            f["terms"] = _basket_ops(d).prepare_deposit(a.address, amount, minimum, token_id=p.position_id)
            self._event(d, day, "quoted", basket_terms=asdict(f["terms"]))
        else:
            f["terms"] = _basket_ops(d).prepare_redemption(a.address, p.position_id, p.redeem_shares, p.minimum_buck)
            f["preserved"] = max(held, p.reserve_floor) + 1
            self._event(d, day, "quoted", basket_terms=asdict(f["terms"]))
        return True

    def _apply_surplus(self, d, day, result, ctr):
        a, f = self.agent, self.flow
        p, phase, receipt = f["plan"], f["phase"], result.receipt
        if phase == "buy":
            if not 0 < result.paid <= p.spend_usdc or result.received < p.minimum_net:
                raise ValueError("surplus purchase differs from bounds")
            a.trade_loss += result.paid - result.received
            if p.action == "save":
                a.invested_usd += result.paid
            f["acquired"] = result.received
            moved = dict(usdc=result.paid, net_buck_scaled=result.net_scaled, nominal_buck=result.nominal_received)
            preserved = f["terms"]["preserved"]
            f["phase"] = "deposit" if p.action == "save" else "complete"
        else:
            tid = result.token_id
            old_basis, old_shares = a._receipt_book.get(tid, (0, 0))
            if old_basis != f["terms"].prior_basis or old_shares != f["terms"].prior_shares:
                raise ValueError("household basket books differ from prepared receipt")
            if phase == "deposit":
                shares, basis = result.shares, result.basis
                if tid not in a._basket_receipts:
                    a._basket_receipts.append(tid)
                moved = dict(buck=result.received, token_id=tid, shares_added=result.shares_added)
                ctr["bcdInvested"] = ctr.get("bcdInvested", 0) + result.received
                ctr["bcdInvestments"] = ctr.get("bcdInvestments", 0) + 1
                preserved = None
            else:
                shares, basis = result.shares_remaining, result.basis_remaining
                moved = dict(buck=result.paid, token_id=tid, shares_retired=result.shares_retired, in_kind=result.in_kind)
                preserved = f["preserved"]
            a._basket_in += basis - old_basis
            a._basket_shares += shares - old_shares
            if shares:
                a._receipt_book[tid] = (basis, shares)
            else:
                a._receipt_book.pop(tid, None)
                a._basket_receipts.remove(tid)
            f["phase"] = "complete"
        if p.action == "retire":
            a._net_repayment_reserve, a._net_repayment_target = preserved, p.target_principal
        f["hash"], f["terms"], f["status"] = None, None, "running"
        self._event(d, day, "confirmed", operation=phase, amounts=moved,
                    transaction_hash=_txhex(receipt["transactionHash"]), receipt_block=receipt.get("blockNumber"))

    def start(self, d, plan, day, ctr, *, context=None):
        if self.active:
            raise ValueError("household refinancing already active")
        if self.payment_due:
            raise ValueError("household bank payment due; retry before a new refinance")
        if self.settlement_due:
            raise ValueError("household has an unsettled export; resolve before a new refinance")
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
        if c is not None and plan.reserve_floor < _reserve_for(c):
            raise ValueError("refinance must retain the quoted BUCK reserve")
        if c is None and plan.switch_cost:
            raise ValueError("wallet prepayment cannot switch insurance")
        if self.agent.refinance_closed and plan.switch_cost:
            raise ValueError("household refinance closing was already paid")
        book = d.net_credit_ops.household(self.agent.address, self.agent._token_ids)
        if any(d.buck.functions.reductionAuthorization(t).call()[0] for t in self.agent._token_ids):
            raise ValueError("household reduction authorization pending")
        fresh = c.requested_spendable if c else 0
        if book.spendable + fresh + plan.reserve_buy_net < plan.export_buck + plan.reserve_floor:
            raise ValueError("refinance would consume the BUCK reserve")
        bank = _bank_terms(plan)
        if bank["amount"] >= 2**256:
            raise ValueError("household bank allocation overflows uint256")
        cash = d.usdc.functions.balanceOf(self.agent.address).call()
        if funding and cash < plan.cash_buffer + plan.reserve_buy_max_usdc:
            raise ValueError("reserve purchase would consume the USDC buffer")
        if cash + plan.minimum_usdc < plan.cash_buffer + plan.reserve_buy_max_usdc + bank["amount"]:
            raise ValueError("refinance would consume the USDC buffer")
        context = _wire(context or {})
        self.decisions += 1
        # Policy state outlives this cursor; later saving/retirement neither
        # replaces it with its own protection nor releases it.
        self.agent._net_credit_reserve = plan.reserve_floor
        self.flow = {"plan": plan, "origin": plan, "revision": 0,
                     "phase": "fund" if funding else "issue" if c else "export",
                     "status": "running", "hash": None, "issue_candidate": None,
                     "initial_closed": self.agent.refinance_closed, "terms": None}
        self.agent.funding_pending = True
        limits = {key: self.agent.p[key] for key in
                  ("surplus_policy", "theta", "issue_premium", "retire_disc", "utilization", "max_impact_bp")}
        self._event(d, day, "planned", plan=asdict(plan), context=context, limits=limits,
                    market=self._market(d), basket=self._basket(d))
        return self.advance(d, day, ctr)

    def retry_payment(self, d, day, ctr):
        """Compatibility bank-only entry; use the shared remainder assessment."""
        if not self.payment_due:
            raise ValueError("household has no refused bank payment to retry")
        return self.resume(d, day, ctr)

    def resume(self, d, day, ctr, **options):
        """PRE: proved refusal, original intent and an idle owner/calendar.
        POST: only reassessed unfinished cash phases can run. Their latest
        bounds authenticate recovery; original caps/confirmed steps survive.
        A declined quote retains the intent and sends nothing. Unknown receipts
        cannot be repriced or retried through this entry point.
        """
        source = self.flow
        assessment = assess_remainder(self.agent, d, day, **options)
        if (self.flow is not source or not self.settlement_due or self.agent.calendar_machine.active or
                source["phase"] != assessment["context"]["phase"] or
                source.get("revision", 0) != assessment["context"]["revision"]):
            raise ValueError("household remainder observation changed")
        self.agent.funding_pending = True
        if assessment["plan"] is None:
            self.agent.last_action = "refinance-wait"
            self.agent.last_error = assessment["context"]["reason"]
            self._event(d, day, "waiting", context=assessment["context"])
            return True
        previous = self.flow["plan"]
        self.flow.update(plan=assessment["plan"], phase=assessment["phase"], status="running", hash=None,
                         terms=None, revision=self.flow.get("revision", 0) + 1)
        self._event(d, day, "resumed", context=assessment["context"], prior_plan=asdict(previous),
                    plan=asdict(assessment["plan"]), revision=self.flow["revision"])
        return self.advance(d, day, ctr)

    def _refuse(self, d, day, reason):
        self.flow["status"] = "refused"
        self.agent.funding_pending = False
        self.agent.last_error = reason[:200]
        self.agent.last_action = f"{self._label()}-refused"
        self._event(d, day, "refused", reason=reason[:200], transaction_hash=self.flow.get("hash"))

    def _apply(self, d, day, result, ctr):
        if self.flow.get("kind") == "surplus":
            return self._apply_surplus(d, day, result, ctr)
        if self.flow.get("kind") == "wallet":
            return self._apply_wallet(d, day, result, ctr)
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
            if a.calendar_machine.bound and terms["principal"]:
                a.calendar_machine.record_mortgage(d, receipt, terms["principal"])
            elif terms["principal"]:
                a.mortgage -= terms["principal"]
                a.external_paid += terms["principal"]
            a.switch_paid += terms["switch"]
            a.penalty_paid += terms["penalty"]
            if f.get("origin", f["plan"]).candidate is not None:
                a.refinance_closed = True
            if terms["principal"]:
                a.refis += 1
                a.deploys += 1
                key = "rtrTopUps" if f["initial_closed"] else "rtrRefis"
                ctr[key] = ctr.get(key, 0) + 1
                ctr["rtrUsdRetired"] = ctr.get("rtrUsdRetired", 0) + terms["principal"] // RAW
            moved = {"usdc": result.amount, "principal_usdc": terms["principal"],
                     "switch_usdc": terms["switch"], "penalty_usdc": terms["penalty"]}
        # Commit the financial transition and cursor before optional observations.
        f["hash"], f["terms"], f["status"] = None, None, "running"
        f["phase"] = PHASES[PHASES.index(phase) + 1] if phase != "mortgage" else "complete"
        self._event(d, day, "confirmed", operation=phase, amounts=moved,
                    transaction_hash=_txhex(receipt["transactionHash"]), receipt_block=receipt.get("blockNumber"))

    def _apply_known(self, d, day, result, ctr):
        """Retain evidence if the required receipt-date lookup is unavailable."""
        f, phase = self.flow, self.flow["phase"]
        f["hash"] = _txhex(result.receipt["transactionHash"])
        try:
            self._apply(d, day, result, ctr)
            return True
        except Exception as exc:
            if f["phase"] != phase:
                raise
            f["status"] = "pending"
            self.agent.last_action = f"{self._label()}-pending"
            self.agent.last_error = f"{self._label()} accounting unavailable: {exc}"[:200]
            self._event(d, day, "pending", reason=self.agent.last_error, transaction_hash=f["hash"])
            return False

    def _recover(self, d):
        f, a = self.flow, self.agent
        receipt = d.w3.eth.get_transaction_receipt(f["hash"])
        if _txhex(receipt.get("transactionHash")).lower() != f["hash"].lower():
            raise ValueError("household receipt hash mismatch")
        if receipt.get("status") == 0:
            raise TransactionReverted(f["phase"], "household operation reverted", receipt)
        p = f["plan"]
        if f.get("kind") == "wallet":
            if f["phase"] == "buy":
                return d.net_market_ops.purchase_from_receipt(a.address, d.pool_ub, p.wallet, receipt, expected_hash=f["hash"])
            return d.net_market_ops.sale_from_receipt(p.wallet, d.pool_ub, a.address, p.sell_buck, p.minimum_usdc, receipt,
                                                      expected_hash=f["hash"])
        if f.get("kind") == "surplus":
            if f["phase"] == "buy":
                return d.net_market_ops.purchase_from_receipt(a.address, d.pool_ub, a.address, receipt, expected_hash=f["hash"])
            ops = _basket_ops(d)
            reader = ops.deposit_from_receipt if f["phase"] == "deposit" else ops.redemption_from_receipt
            return reader(f["terms"], receipt, expected_hash=f["hash"])
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
                a.last_error = f"{self._label()} hash unavailable; reconcile before continuing"
                return True
            try:
                result = self._recover(d)
            except TransactionReverted as exc:
                self._refuse(d, day, str(exc))
                return True
            except Exception as exc:
                a.last_error = f"{self._label()} receipt unavailable: {exc}"[:200]
                return True
            if not self._apply_known(d, day, result, ctr):
                return True
        while f["phase"] != "complete":
            p, phase = f["plan"], f["phase"]
            if f.get("kind") == "surplus":
                try:
                    prepared = self._prepare_surplus(d, day)
                except Exception as exc:
                    self._refuse(d, day, f"surplus observation unavailable: {exc}")
                    return True
                if not prepared:
                    if f["phase"] == "complete":
                        continue
                    return True
            elif f.get("kind") == "wallet":
                try:
                    prepared = self._prepare_wallet(d, day)
                except Exception as exc:
                    self._refuse(d, day, f"wallet observation unavailable: {exc}")
                    return True
                if not prepared:
                    return True
            elif phase == "issue":
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
                        _reserve_for(fresh) > p.reserve_floor):
                    self._refuse(d, day, "refinance issuance terms changed; reassess retained holdings")
                    return True
                f["issue_candidate"] = fresh
            elif phase == "fund":
                if d.usdc.functions.balanceOf(a.address).call() < p.reserve_buy_max_usdc + p.cash_buffer:
                    self._refuse(d, day, "refinance reserve USDC buffer changed")
                    return True
            elif phase == "mortgage":
                terms = _bank_terms(p)
                if not terms["amount"]:
                    f["phase"], f["satisfied"] = "complete", True
                    self._event(d, day, "satisfied", reason="mortgage already retired; no bank amount due")
                    continue
                if d.usdc.functions.balanceOf(a.address).call() < terms["amount"] + p.cash_buffer:
                    self._refuse(d, day, "refinance USDC buffer changed")
                    return True
                f["terms"] = terms
            elif phase == "export" and d.buck.functions.balanceOf(a.address).call() < p.export_buck + p.reserve_floor:
                self._refuse(d, day, "refinance BUCK reserve changed")
                return True
            # Save the phase before dispatch. Exceptions after dispatch can
            # never turn an unknown outcome into permission to send again.
            f["status"], f["hash"] = "pending", None
            try:
                if f.get("kind") == "wallet":
                    if phase == "buy":
                        result = d.net_market_ops.buy_exact_input(a.address, d.pool_ub, p.spend_usdc, p.minimum_net, p.wallet)
                    else:
                        result = d.net_market_ops.sell_exact_input(p.wallet, d.pool_ub, p.sell_buck, p.minimum_usdc,
                                                                    a.address, d.fee_ub)
                elif f.get("kind") == "surplus":
                    if phase == "buy":
                        result = d.net_market_ops.buy_exact_input(a.address, d.pool_ub, p.spend_usdc, p.minimum_net, a.address)
                    elif phase == "deposit":
                        result = _basket_ops(d).deposit(f["terms"])
                    else:
                        result = _basket_ops(d).redeem(f["terms"])
                elif phase == "fund":
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
                    a.last_action = f"{self._label()}-pending"
                    a.last_error = str(exc)[:200]
                    self._event(d, day, "pending", reason=a.last_error, transaction_hash=f["hash"])
                return True
            except Exception as exc:
                a.last_action, a.last_error = f"{self._label()}-pending", str(exc)[:200]
                self._event(d, day, "pending", reason=a.last_error, transaction_hash=None)
                return True
            if not self._apply_known(d, day, result, ctr):
                return True
        f["status"] = "complete"
        kind = f.get("kind")
        action = (f"wallet-{f['plan'].action}" if kind == "wallet" else
                  "save-retained" if f.get("retained") else "save" if f["plan"].action == "save" else "retirement-funded"
                  ) if kind in ("surplus", "wallet") else ("refinance-satisfied" if f.get("satisfied") else "refinance")
        a.funding_pending, a.last_action, a.last_error = False, action, ""
        if kind == "surplus":
            # Completion of a retire plan funds a target; principal changes
            # only in the selected-facility engine's own confirmed records.
            self._event(d, day, "complete", outcome=action, retirement=self.retirement())
        elif kind == "wallet":
            self._event(d, day, "complete", outcome=action)
        else:
            self._event(d, day, "complete")
        return True
