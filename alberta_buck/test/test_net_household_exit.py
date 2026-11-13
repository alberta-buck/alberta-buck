"""A leaving household: asset sale, savings home, the whole lien retired, coverage released, and out."""
import json
from pathlib import Path

import pytest
from web3.logs import DISCARD
from buck_core import TransactionReceiptPending
from buck_core.session import _txhex

from alberta_buck.sim.credit_households import CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent
from alberta_buck.test.test_net_household_setup import RAW, _surplus_day, household_world  # noqa: F401 (fixture)
from alberta_buck.test.test_net_household_invest import _buy_buck_in_token_pools

EXIT = json.loads((Path(__file__).resolve().parents[2] / "test/fixtures/household-exit.json").read_text())
FORECASTS, FLOW = EXIT["forecasts"], EXIT["flow"]
ZERO = "0x0000000000000000000000000000000000000000"


def _household(d, make, index, cls, settings):
    a = make(index, cls, settings=settings)
    for contract, method, args in ((d.usdc, "approve", [d.swap_guard.address, 2**256 - 1]),
                                   (d.buck, "approve", [d.permit2.address, 2**256 - 1]),
                                   (d.permit2, "approve", [d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1])):
        a._proxy_exec(d, contract.address, contract.encode_abi(method, args=args))
    a.advance_net_calendar(d, 0, {})
    return a


def _saver(d, make, index, ctr):
    """Two days of a carry line, a working balance bought on the payday, a receipt and a lien."""
    spec = EXIT["saver"]
    a = _household(d, make, index, CreditSaverAgent, spec["settings"])
    for day in range(spec["exit_day"]):
        if day:
            _surplus_day(d, a, day)
        a.advance_net_investment(d, day, ctr, forecasts=FORECASTS)
        assert a.last_action == "invest", (day, a.last_error)
        a.advance_net_wallet(d, day, ctr)
    assert a._wallet is not None and a.wallet_bought > RAW and len(a._basket_receipts) == 1
    a._divest_day = spec["exit_day"]
    return a


def _leave(d, a, day, ctr, turns=8):
    """One simulated day of exit work: the stages reached, in order, until nothing more can be done today."""
    _surplus_day(d, a, day)
    stages = []
    for _ in range(turns):
        if not a.advance_net_exit(d, day, ctr):
            break
        stages.append(a.exit_stage)
    return stages


def _books(d, a):
    book = d.net_credit_ops.household(a.address, a._token_ids) if a._token_ids else None
    return dict(cash=d.usdc.functions.balanceOf(a.address).call(), buck=d.buck.functions.balanceOf(a.address).call(),
                wallet=d.buck.functions.balanceOf(a._wallet.address).call() if a._wallet is not None else 0,
                issued=book.issued if book else 0, premium=book.premium_nominal if book else 0,
                activated=sum(f.activated for f in book.facilities) if book else 0,
                supply=d.buck.functions.totalIssued().call(), receipts=tuple(a._basket_receipts))


def _events(m, kind, status="confirmed"):
    return [e for e in m.events if e.get("kind") == kind and e["status"] == status]


def _assert_gone(d, a, ctr, day):
    """Nothing is left on-chain or in the books, and every dispatcher leaves an exited household alone."""
    end = _books(d, a)
    assert a.exited and a.exit_stage == "complete" and a.last_action == "exit", a.last_error
    assert end["issued"] == end["premium"] == end["activated"] == 0 and end["receipts"] == ()
    assert end["buck"] < RAW and end["wallet"] < RAW + RAW // 100, "at most a BUCK of dust stays"
    assert not a._receipt_book and a._basket_in == a._basket_shares == 0
    assert a._net_repayment_target is None and a._net_repayment_reserve is None and not a.refinance_machine.active
    before, seen = d.w3.eth.block_number, len(a.refinance_machine.events)
    assert a.advance_net_exit(d, day, ctr) is False and a.advance_net_surplus(d, day, ctr) is False
    assert a.advance_net_wallet(d, day, ctr) is False
    assert (d.w3.eth.block_number, len(a.refinance_machine.events)) == (before, seen)
    return end


def test_household_exit_sells_the_asset_retires_the_whole_lien_and_leaves(household_world):
    d, _, make = household_world
    ctr, spec = {}, EXIT["saver"]
    a = _saver(d, make, 95, ctr)
    m, start, hypo = a.refinance_machine, _books(d, a), a.hypo_cash
    assert start["issued"] > 0 and start["premium"] > 0 and start["wallet"] > RAW and not a.exit_due(spec["exit_day"] - 1)
    assert a.advance_net_exit(d, spec["exit_day"] - 1, ctr) is False, "not before its day"
    stages, market_days = [], {}
    for day in range(spec["exit_day"], spec["last_day"]):
        seen = len(m.events)
        stages += _leave(d, a, day, ctr)
        trades = [e for e in m.events[seen:] if e["status"] == "confirmed" and e.get("operation") in ("sell", "buy")]
        market_days[day] = len(trades)
        if a.exited:
            break
    assert [s for i, s in enumerate(stages) if i == 0 or s != stages[i - 1]] == spec["stages"], stages
    assert max(market_days.values()) == 1 and sum(market_days.values()) > 2, "one capped market bite a day"
    end = _assert_gone(d, a, ctr, day)
    assert a.exit_started == spec["exit_day"] and a._sold and a.exit_retries == 0 and not a.exit_short
    # The asset fetched its insured value in dollars; the counterfactual sold the same asset the same day.
    sale = [e for e in _events(m, "calendar") if e["phase"].startswith("sale") and e["operation"] == "income"]
    assert len(sale) == 1 and int(sale[0]["amounts"]["usdc"]) == a._face0 and a.hypo_cash == hypo + a._face0
    assert a.income_total == a.income_annual // 12, "sale proceeds are not wages"
    # The whole lien was retired by the selected-facility engine alone, and its whole deposit came back.
    reductions = _events(m, "retirement")
    assert sum(int(e["amounts"]["principal"]) for e in reductions) == start["issued"] == a.unwound
    assert sum(int(e["amounts"]["premium_refund_nominal"]) for e in reductions) == start["premium"]
    assert end["supply"] == start["supply"] - start["issued"], "supply fell by exactly the principal retired"
    redeemed = [e for e in _events(m, "exit") if e["operation"] == "redeem"]
    assert len(redeemed) == 1 and int(redeemed[0]["amounts"]["buck"]) > 0 and redeemed[0]["amounts"]["in_kind"] == []
    # Full retirement released every facility's coverage itself: burn(0) had nothing left to release.
    assert a._released and a.released_coverage == 0 and not [e for e in _events(m, "exit") if e["operation"] == "release"]
    sold = sum(int(e["amounts"]["usdc"]) for e in _events(m, "exit") if e["operation"] == "sell")
    wallet = sum(int(e["amounts"]["usdc"]) for e in _events(m, "wallet") if e["operation"] == "sell")
    assert a.withdrawn_usd == sold > 0 and wallet > 0 and end["cash"] > a._face0
    final = m.events[-1]
    assert (final["kind"], final["status"], final["outcome"]) == ("exit", "complete", "exited")
    assert final["amounts"]["retired_principal"] == str(start["issued"]) and ctr["hhDivested"] == ctr["hhDivestments"] == 1
    assert [e["seq"] for e in m.events] == list(range(1, len(m.events) + 1)) and not d.chain.mismatches


def test_debtor_exit_pays_the_whole_mortgage_from_the_sale_and_buys_back_its_lien(household_world):
    d, _, make = household_world
    ctr, spec = {}, EXIT["debtor"]
    a = _household(d, make, 96, CreditHouseholdAgent, spec["settings"])
    m = a.refinance_machine
    for day in range(spec["exit_day"]):
        if day:
            _surplus_day(d, a, day)
        a.advance_net_refinancing(d, day, ctr, forecasts=FORECASTS)
    start = _books(d, a)
    assert a.refis >= 1 and start["issued"] > 0 and a.mortgage > 0, a.last_error
    mortgage, shadow, paid = a.mortgage, a.hypo_mortgage, a.external_paid
    a._divest_day = spec["exit_day"]
    for day in range(spec["exit_day"], spec["last_day"]):
        _leave(d, a, day, ctr)
        if day == spec["exit_day"]:
            # The same day: arrears and the WHOLE mortgage from the price, actual and counterfactual alike.
            assert a._sold and a.mortgage == a.hypo_mortgage == 0
            assert a.external_paid - paid >= mortgage and a.hypo_paid >= shadow
        if a.exited:
            break
    end = _assert_gone(d, a, ctr, day)
    settle = [e for e in _events(m, "calendar") if e["phase"].startswith("sale") and e["operation"] == "mortgage"]
    assert len(settle) == 1 and int(settle[0]["amounts"]["usdc"]) >= mortgage
    # It had sold its issued BUCK to the bank, so the lien is bought back: guarded purchases, one a day.
    bought = [e for e in _events(m, "surplus") if e["operation"] == "buy"]
    assert bought and len({e["day"] for e in bought}) == len(bought)
    assert sum(int(e["amounts"]["principal"]) for e in _events(m, "retirement")) == start["issued"] == a.unwound
    assert end["supply"] == start["supply"] - start["issued"] and a.penalty_paid == 0
    assert not d.chain.mismatches


def test_investor_withdraws_at_its_horizon_by_redeeming_and_selling(household_world):
    d, _, make = household_world
    ctr, spec = {}, EXIT["investor"]
    a = _household(d, make, 97, HouseholdInvestorAgent, spec["settings"])
    m = a.refinance_machine
    opening = _books(d, a)
    # Not dear: it buys BUCK with its dollars, one capped bite, and HOLDS them as money.
    assert a.advance_net_investor(d, 0, ctr) is True and a.last_action == "buy", a.last_error
    held = _books(d, a)
    assert held["buck"] > RAW and held["receipts"] == () and a.invested_usd == opening["cash"] - held["cash"] > 0
    assert a.advance_net_investor(d, 0, ctr) is False, "one decision a day"
    with pytest.raises(ValueError, match="cash investor"):
        make(197, CreditSaverAgent).advance_net_investor(d, 0, ctr)
    # Dear against the basket by more than its premium: it converts what it holds into basket shares.
    _buy_buck_in_token_pools(d, spec["token_pool_share"])
    _surplus_day(d, a, 1)
    quote = a.assess_net_investor(d, 1)
    assert quote["context"]["action"] == "convert" and quote["plan"].deposit_buck <= held["buck"]
    assert a.advance_net_investor(d, 1, ctr) is True and a.last_action == "convert", a.last_error
    start = _books(d, a)
    assert len(a._basket_receipts) == 1 and a._basket_in == quote["plan"].deposit_buck and start["buck"] < held["buck"] // 100
    assert start["cash"] == held["cash"] and start["supply"] == opening["supply"] and not a._token_ids
    assert [e["operation"] for e in _events(m, "surplus")] == ["buy", "deposit"] and not a.exit_due(spec["exit_day"])
    a._exit_day = spec["exit_day"]
    for day in range(spec["exit_day"], spec["last_day"]):
        _leave(d, a, day, ctr)
        if a.exited:
            break
    end = _assert_gone(d, a, ctr, day)
    assert not a._sold and end["supply"] == start["supply"], "an investor has no asset, lien or coverage"
    assert a.withdrawn_usd == end["cash"] - start["cash"] > 0 and ctr["invExits"] == 1 and "hhDivested" not in ctr
    assert not [e for e in m.events if e.get("kind") in ("retirement", "calendar") and e["day"] >= spec["exit_day"]
                and e["status"] == "confirmed"]
    assert not d.chain.mismatches


def test_exit_releases_coverage_that_no_principal_backs(household_world):
    d, _, make = household_world
    ctr, spec = {}, EXIT["release"]
    a = _household(d, make, 98, CreditSaverAgent, spec["settings"])
    m, coverage = a.refinance_machine, int(spec["coverage"])
    token = a._token_ids[0]
    d.net_credit_ops.issue(a.address, token, coverage, 0, 0, ZERO)   # backing only: no cash, principal or premium
    start = _books(d, a)
    assert start["activated"] == coverage and start["issued"] == start["premium"] == start["buck"] == 0
    a._divest_day = 0
    stages = _leave(d, a, 0, ctr)
    assert stages == ["sell-asset", "release-coverage", "complete"], (stages, a.last_error)
    end = _assert_gone(d, a, ctr, 0)
    released = [e for e in _events(m, "exit") if e["operation"] == "release"]
    assert len(released) == 1 and released[0]["amounts"]["coverage"] == str(coverage)
    assert released[0]["amounts"]["facilities"] == [dict(token_id=str(token), coverage=str(coverage), premium_refund_nominal="0")]
    logs = d.buck.events.FacilityReleased().process_receipt(
        d.w3.eth.get_transaction_receipt(released[0]["transaction_hash"]), errors=DISCARD)
    assert [(x["args"]["tokenId"], x["args"]["coverage"]) for x in logs] == [(token, coverage)]
    assert a._released and a.released_coverage == coverage and a.released_refund == 0 and end["supply"] == start["supply"]
    assert m.events[-1]["amounts"]["released_coverage"] == str(coverage) and not d.chain.mismatches


@pytest.mark.parametrize("case", FLOW["cases"], ids=lambda c: c["name"])
def test_household_exit_applies_known_outcomes_once_and_stops_on_unknown(household_world, monkeypatch, case):
    d, _, make = household_world
    ctr, spec = {}, EXIT["saver"]
    a = _saver(d, make, 99, ctr)
    m, start, day = a.refinance_machine, _books(d, a), spec["exit_day"]
    step = case.get("lost") or case.get("unknown") or case.get("refused")
    sends, fault, index = [], [True], FLOW["refuse_argument"]

    def wrap(dispatch):
        def send(owner, target, method, args):
            current = m.flow["plan"].step if m.flow and m.flow.get("kind") == "exit" and m.active else None
            sends.append((current, method))
            hit = fault[0] and current is not None and current == step
            if hit and case.get("unknown"):
                fault[0] = False
                raise RuntimeError("submission outcome unknown")
            if hit and case.get("refused"):
                fault[0] = False
                args = list(args)
                args[index[method]] = 2**200  # the real contract refuses its own unreachable bound
            receipt = dispatch(owner, target, method, args)
            if hit and case.get("lost"):
                fault[0] = False
                raise TransactionReceiptPending(_txhex(receipt["transactionHash"]))
            return receipt
        return send
    for ops in (d.net_market_ops, d.net_credit_ops, d.net_settlement_ops):
        monkeypatch.setattr(ops, "execute", wrap(ops.execute))
    sent = lambda name: [method for at, method in sends if at == name]
    if case.get("preview"):
        simulate, previews = d.net_simulate, []
        def closed(owner, target, method, args, block_number=None):
            if not previews or previews[-1] == day:
                previews.append(day)
                raise RuntimeError("execution reverted: CreditShortfallIncreased")
            return simulate(owner, target, method, args, block_number)
        monkeypatch.setattr(d, "net_simulate", closed)
    if case.get("shortfall"):
        # After the sale the dollars are gone (a test transfer); the wallet and receipt are too small for the lien.
        _leave(d, a, day, ctr, turns=1)
        assert a._sold and a.exit_stage == "sell-asset"
        a._proxy_exec(d, d.usdc.address, d.usdc.encode_abi(
            "transfer", args=[d.simlp.address, d.usdc.functions.balanceOf(a.address).call()]))
        a._proxy_exec(d, d.buck.address, d.buck.encode_abi(
            "transfer", args=[d.simlp.address, d.buck.functions.balanceOf(a.address).call()]))
        a._basket_receipts.clear()                      # the receipt is left out of this household's reach
        book = dict(a._receipt_book)
        a._receipt_book.clear()
        for later in range(day, day + 8):
            _leave(d, a, later, ctr)
            if a.exit_short:
                break
        assert a.exit_short and a.exit_stage == "cash-shortfall" and not a.exited and ctr["hhDivestShort"] == 1
        assert d.net_credit_ops.household(a.address, a._token_ids).issued > 0, "insolvency is not a completed exit"
        waits = [e for e in _events(m, "exit", "waiting") if e["phase"] == "cash-shortfall"]
        assert len(waits) == 1 and "no dollars" in waits[0]["reason"]
        # Dollars and the receipt return: the same exit resumes where it stood and completes.
        d.chain.send(d.usdc.functions.mint(a.address, 100_000 * RAW))
        a._basket_receipts[:] = list(book)
        a._receipt_book.update(book)
        for later in range(later + 1, later + 12):
            _leave(d, a, later, ctr)
            if a.exited:
                break
        _assert_gone(d, a, ctr, later)
        assert ctr["hhDivestShort"] == 1 and ctr["hhDivested"] == 1
        return

    def until(done):
        """Advance one turn at a time, a day at a time, until the case's moment; the day reached."""
        at = day
        for _ in range(60):
            _surplus_day(d, a, at)
            while a.advance_net_exit(d, at, ctr):
                if done():
                    return at
            if done():
                return at
            at += 1
        raise AssertionError("the case's moment never came")

    if case.get("preview"):
        # The basket's own K gate refused the exit preview: nothing is sent, and the wait is counted once a day.
        day = until(lambda: bool(_events(m, "exit", "waiting")))
        waits = _events(m, "exit", "waiting")
        assert not sent("redeem") and a.exit_retries == 1 and a.exit_stage == "collect-savings"
        assert len(waits) == 1 and "CreditShortfallIncreased" in waits[0]["reason"] and a.last_action == "exit-wait"
        assert a.advance_net_exit(d, day, ctr) is False and len(_events(m, "exit", "waiting")) == 1
    elif case.get("unknown"):
        day = until(lambda: not fault[0])
        assert m.active and m.flow["status"] == "pending" and m.flow["hash"] is None and a.last_action == "exit-pending"
        count, books = len(sends), _books(d, a)
        for later in (day, day + 1, day + 2):
            d.anvil.warp_to(a.calendar_machine.epoch + later * 86400)
            assert a.advance_net_exit(d, later, ctr) is True
            assert "exit hash unavailable" in a.last_error and len(sends) == count
        assert not a.exited and _books(d, a)["receipts"] == books["receipts"]
        return
    elif case.get("lost"):
        day = until(lambda: not fault[0])
        assert m.active and m.flow["status"] == "pending" and m.flow["hash"] and len(sent(step)) == 1
        lost_hash = m.flow["hash"]
        assert a.advance_net_exit(d, day, ctr) is True
        applied = [e for e in _events(m, "exit") if e["operation"] == step]
        assert len(applied) == 1 and applied[0]["transaction_hash"] == lost_hash and len(sent(step)) == 1
    else:
        day = until(lambda: not fault[0])
        refusal = [e for e in m.events if e["status"] == "refused"][-1]
        assert refusal["kind"] == "exit" and d.w3.eth.get_transaction_receipt(refusal["transaction_hash"])["status"] == 0
        assert a.last_action == "exit-refused" and a.exit_retries == 1 and not m.active
        assert a.advance_net_exit(d, day, ctr) is False, "a proved refusal is retried tomorrow, never today"
        assert _books(d, a)["receipts"] == start["receipts"]

    later = day
    for later in range(day, day + 14):
        _leave(d, a, later, ctr)
        if a.exited:
            break
    end = _assert_gone(d, a, ctr, later)
    assert sum(int(e["amounts"]["principal"]) for e in _events(m, "retirement")) == start["issued"] == a.unwound
    assert end["supply"] == start["supply"] - start["issued"]
    hashes = [e["transaction_hash"] for e in m.events if e["status"] == "confirmed"]
    assert len(set(hashes)) == len(hashes), "no receipt is applied twice"
    assert len(sent("redeem")) == (2 if case.get("refused") == "redeem" else 1)
    assert [e["seq"] for e in m.events] == list(range(1, len(m.events) + 1)) and not d.chain.mismatches
