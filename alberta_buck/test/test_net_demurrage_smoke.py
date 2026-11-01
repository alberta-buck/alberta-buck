"""A linked BuckNet deployment against the independent demurrage reference.

This is a cash-only, zero-premium accounting smoke test. It is not a household
economy, market, insurer model, leveraged basket, or five-year validation.
"""

import json
import math
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from alberta_buck.sim.anvil import Anvil
from alberta_buck.sim import carry
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.demurrage_reference import DemurrageLedger
from buck_core import (bound_activated_capacity, deploy_net_stack, issue_coverage_candidates, net_transfer_value, IssuancePending,
                       NetCreditOps, ReductionPending, TransactionReceiptPending)
from buck_core.session import Expect, _txhex

YEAR = 365 * 24 * 60 * 60 + 6 * 60 * 60
RAW = 10**6
Q = 10**36
ZERO = "0x" + "00" * 20
ROOT = Path(__file__).resolve().parents[2]
VECTOR = json.loads((ROOT / "test/fixtures/bucknet-credit-operations.json").read_text())
REQUIRED = ("BuckNet", "BuckNetMaturity", "BuckNetTransfers", "BuckNetQuoter", "JubileeFund",
            "IdentityRegistryHarness", "BuckCredit", "BuckKControllerStatic", "UniswapV3Factory",
            "BuckKControllerDirect", "BuckBasketEquity", "SimLP")


def _exec(chain, actor, target, method, args):
    data = target.encode_abi(method, args=args)
    return chain.send(actor.functions.exec(target.address, bytes.fromhex(data[2:])))


def test_activated_capacity_shared_integer_limits():
    max_balance, max_uint = 2**79 - 1, 2**256 - 1
    held = (max_balance - 10) * Q
    assert bound_activated_capacity([4, 4], held, 5 * Q, 0) == (5, 5)
    assert bound_activated_capacity([4, 4], held, 5 * Q + 1, 0) == (4, 4)
    assert bound_activated_capacity([4, 4], held, 0, max_uint // Q - 3) == (3, 3)
    with pytest.raises(ValueError, match="invalid"):
        bound_activated_capacity([4], max_balance * Q, 1, 0)
    with pytest.raises(ValueError, match="invalid"):
        bound_activated_capacity([4], 0, 0, max_uint // Q + 1)


def test_coverage_candidates_are_bounded_and_include_repair_estimates():
    choices = issue_coverage_candidates(100, 40, 30, 14, 75 * 10**16, 100, 1)
    assert choices == (0, 1, 7, 15, 22, 26, 30, 34, 37, 38, 42, 45, 52, 60)
    assert issue_coverage_candidates(100, 40, 0, 14, 10**18, 100, 1) == (0,)
    slow = issue_coverage_candidates(100, 40, 30, 14, 10**17, 100, 1)
    assert len(slow) <= 15 and slow[0] == 0 and slow[-1] == 60
    for bad in ((100, 101, 30, 14, 10**18, 100, 1), (100, 40, 101, 14, 10**18, 100, 1),
                (100, 40, 30, 14, 10**18, 1000, 1), (100, 40, 30, 14, 10**18, 100, 0)):
        with pytest.raises(ValueError, match="coverage"):
            issue_coverage_candidates(*bad)


@pytest.mark.skipif(
    not shutil.which("anvil") or not all((ROOT / "out" / f"{name}.sol" / f"{name}.json").is_file()
                                         for name in REQUIRED),
    reason="requires anvil and built net/simulation artifacts; run make nix-sim-build",
)
def test_linked_net_issuance_and_reduction_match_reference():
    with Anvil(timestamp=1_900_000_000) as anvil:
        chain = Chain(anvil.w3, anvil.w3.eth.accounts[0])
        gov = chain.deployer
        registry = chain.deploy("IdentityRegistryHarness", gov)
        credit = chain.deploy("BuckCredit")
        controller = chain.deploy("BuckKControllerStatic", 10**18, gov)
        stack = deploy_net_stack(chain, registry, credit, controller, gov)
        fund, buck = stack.fund, stack.token
        factory = chain.deploy("UniswapV3Factory")
        basket = chain.deploy("BuckBasketEquity", buck.address, controller.address, factory.address,
                              gov, 3000, 600, 64, 500, 1000)
        actor = chain.deploy("SimLP")  # Public test-only contract identity, not an agent model.
        for target, carrying in ((fund.address, True), (basket.address, False), (actor.address, False)):
            chain.send(registry.functions.bindContract(target, (1, 2), ((1, 2), (1, 2)), True, carrying))
        chain.send(basket.functions.enableNetAccounting())
        chain.send(fund.functions.configure(buck.address, basket.address))
        _exec(chain, actor, credit, "setCreditIssuer", [gov, True])
        chain.send(credit.functions.createCredit(actor.address, 0, 100 * RAW, 100 * RAW, 0, 0, 0, 0))
        assert credit.functions.ownerOf(0).call() == actor.address
        issued = _exec(chain, actor, buck, "issue", [0, 100 * RAW, 100 * RAW, 0, ZERO])
        issued_at = anvil.w3.eth.get_block(issued["blockNumber"])["timestamp"]
        ops = NetCreditOps(chain, stack)
        fresh = ops.quote_relief_rate(0, RAW)
        assert fresh.selected_seconds == 0 and fresh.annual_rate_wad is None
        assert ops.quote_premium_refund(0, 0).reason == "no-premium"
        model = DemurrageLedger()
        model.issue_unspent("facility", 100, "owner", credit_limit=100)
        assert buck.functions.balanceOf(actor.address).call() == 100 * RAW
        assert buck.functions.totalIssued().call() == 100 * RAW
        runtime = anvil.w3.eth.get_code(buck.address).hex().lower()
        assert stack.maturity_library.address[2:].lower() in runtime
        assert stack.transfers_library.address[2:].lower() in runtime
        chain.send(controller.functions.setBuckK(5 * 10**17))
        shocked = ops.assess_issue_candidate(actor.address, 0, 0, RAW, 0)
        assert shocked.current_k == 5 * 10**17 and shocked.shortfall_before == 50 * RAW
        assert "insufficient-capacity" in shocked.reasons
        chain.send(controller.functions.setBuckK(10**18))

        anvil.warp_to(issued_at + YEAR)
        model.advance(1)
        _, cash = buck.functions.fundBalancesScaled().call()
        assert cash / Q / RAW == pytest.approx(model.jubilee, abs=1e-6)
        assert buck.functions.balanceOf(actor.address).call() / RAW == pytest.approx(model.balance_of("owner"), abs=1e-6)
        quote, _, exceeds = fund.functions.previewRelief(0, RAW).call()
        assert not exceeds
        assert quote / Q / RAW == pytest.approx(model.quote_marked_retirement("facility", 1).relief, abs=1e-6)
        observed = ops.quote_relief_rate(0, RAW)
        # Without investment returns the observed net fund value after one
        # year is 1 - exp(-.02), not the full linear 2% target.
        assert observed.annual_rate_wad / 10**18 == pytest.approx(1 - math.exp(-.02), abs=1e-8)
        assert not observed.capped and not observed.exceeds_liquidity_limit
        too_large = ops.quote_relief_rate(0, 100 * RAW)
        assert too_large.exceeds_liquidity_limit and not too_large.capped

        deadline = issued_at + YEAR + 3600
        _exec(chain, actor, buck, "authorizeReduction", [0, RAW, 0, deadline])
        pending = ops.assess_issue_candidate(actor.address, 0, 0, RAW, 0)
        assert "authorization-pending" in pending.reasons
        with pytest.raises(ValueError, match="authorization pending"):
            ops.issue_candidate(actor.address, replace(pending, reasons=()))
        retired = _exec(chain, actor, fund, "reduce", [0, RAW])
        retired_at = anvil.w3.eth.get_block(retired["blockNumber"])["timestamp"]
        model.advance((retired_at - issued_at - YEAR) / YEAR)
        model.retire_marked("facility", 1, "owner")
        assert buck.functions.totalIssued().call() == 99 * RAW
        assert credit.functions.issuanceOf(0).call()[0] == 99 * RAW
        assert buck.functions.balanceOf(actor.address).call() / RAW == pytest.approx(model.balance_of("owner"), abs=2e-6)
        assert buck.functions.fundBalancesScaled().call()[1] / Q / RAW == pytest.approx(model.jubilee, abs=2e-6)

        # An old claim can be fully capped after new liquidity is contributed;
        # the implied annual rate is then only a lower bound on the fund mark.
        anvil.warp_to(issued_at + 49 * YEAR)
        chain.send(credit.functions.createCredit(actor.address, 0, 100 * RAW, 100 * RAW, 0, 0, 0, 0))
        _exec(chain, actor, buck, "issue", [1, 100 * RAW, 50 * RAW, 0, ZERO])
        _exec(chain, actor, buck, "transfer", [fund.address, 40 * RAW])
        capped = ops.quote_relief_rate(0, RAW)
        assert capped.capped and capped.relief_scaled == RAW * Q and capped.annual_rate_wad is not None
        anvil.warp_to(issued_at + 51 * YEAR)
        assert buck.functions.maturitiesPending().call()
        capacity = ops.quote_household_activated_capacity(actor.address, [0, 1])
        assert capacity.maturities_pending and capacity.potential_capacity > 0 and capacity.capacity == 0
        pending = ops.assess_issue_candidate(actor.address, 1, 0, RAW, 0)
        assert "maturities-pending" in pending.reasons
        assert ops.issue_coverage_menu(actor.address, 1, RAW, 0) == (pending,)
        with pytest.raises(Exception):
            ops.quote_relief_rate(0, RAW)  # stale claim denominator must not become a forecast
        chain.send(buck.functions.processMaturities(10))
        assert not buck.functions.maturitiesPending().call()
        assert credit.functions.depreciatedFaceValue(0).call() > credit.functions.currentValue(0).call()
        matured = ops.quote_household_activated_capacity(actor.address, [0])
        assert matured.capacity == matured.unactivated_present == 0  # a matured NFT cannot issue again
        assert "facility-matured" in ops.assess_issue_candidate(actor.address, 0, 0, RAW, 0).reasons
        assert not chain.mismatches


@pytest.mark.skipif(
    not shutil.which("anvil") or not all((ROOT / "out" / f"{name}.sol" / f"{name}.json").is_file()
                                         for name in REQUIRED),
    reason="requires anvil and built net/simulation artifacts; run make nix-sim-build",
)
def test_net_credit_ops_premium_reserve_and_explicit_reduction():
    """Real contract fixture: dynamic controller at a neutral, unbound reference.

    No market or household behavior is modeled here. SimLP identities and a
    no-strategy insurer pool are test infrastructure, not production actors.
    """
    with Anvil(timestamp=1_900_000_000) as anvil:
        chain = Chain(anvil.w3, anvil.w3.eth.accounts[0])
        gov = chain.deployer
        registry = chain.deploy("IdentityRegistryHarness", gov)
        credit = chain.deploy("BuckCredit")
        controller = chain.deploy("BuckKControllerDirect", 100_000_000_000, 643_004, 0,
                                  1800, 0, 950_000_000_000_000_000, 750_000_000_000_000_000, gov)
        stack = deploy_net_stack(chain, registry, credit, controller, gov)
        buck, fund = stack.token, stack.fund
        factory = chain.deploy("UniswapV3Factory")
        basket = chain.deploy("BuckBasketEquity", buck.address, controller.address, factory.address,
                              gov, 3000, 600, 64, 500, 1000)
        insurer, insurer2, provider, debtor, pool, pool2 = (chain.deploy("SimLP") for _ in range(6))
        g = (1, 2)
        e = (g, g)
        for target, carrying in ((fund.address, True), (basket.address, False),
                                 (insurer.address, False), (insurer2.address, False),
                                 (provider.address, False), (debtor.address, False)):
            chain.send(registry.functions.bindContract(target, g, e, True, carrying))
        _exec(chain, insurer, registry, "bindContract", [pool.address, g, e, True, True])
        _exec(chain, insurer2, registry, "bindContract", [pool2.address, g, e, True, True])
        _exec(chain, insurer, buck, "setInsurerPool", [pool.address])
        _exec(chain, insurer2, buck, "setInsurerPool", [pool2.address])
        _exec(chain, pool, buck, "approve", [buck.address, 2**256 - 1])
        _exec(chain, pool2, buck, "approve", [buck.address, 2**256 - 1])
        chain.send(basket.functions.enableNetAccounting())
        chain.send(fund.functions.configure(buck.address, basket.address))

        actors = {a.address.lower(): a for a in (provider, debtor)}
        ops = NetCreditOps(chain, stack, lambda owner, contract, method, args:
                           _exec(chain, actors[owner.lower()], contract, method, args))
        face, premium = VECTOR["face"], VECTOR["premium"]
        reserve, buffer = VECTOR["reserve"], VECTOR["reserve_buffer"]
        spendable, issued, reduction = VECTOR["requested_spendable"], VECTOR["issued"], VECTOR["reduction"]
        for actor, rate in ((provider, 0), (debtor, VECTOR["premium_rate_bp"])):
            _exec(chain, actor, credit, "setCreditIssuer", [insurer.address, True])
            _exec(chain, insurer, credit, "createCredit", [actor.address, 0, face, face, 0, 0, 0, rate])
        provider_id = credit.functions.tokenOfOwnerByIndex(provider.address, 0).call()
        debtor_id = credit.functions.tokenOfOwnerByIndex(debtor.address, 0).call()
        # A lost receipt cannot trigger another issue. Recover the actual
        # event by hash and reject mismatched or duplicated evidence.
        provider_plan = ops.assess_issue_candidate(provider.address, provider_id, face, VECTOR["provider_issuance"], 0)
        assert provider_plan.reasons == ()
        def unknown_issue(_owner, _contract, _method, _args):
            raise RuntimeError("dispatch outcome unavailable")
        with pytest.raises(IssuancePending) as unknown:
            NetCreditOps(chain, stack, unknown_issue).issue_candidate(provider.address, provider_plan)
        assert unknown.value.transaction_hash is None and not unknown.value.reverted
        assert ops.facility(provider_id).issued == 0
        def lost_issue(owner, contract, method, args):
            receipt = _exec(chain, actors[owner.lower()], contract, method, args)
            raise TransactionReceiptPending(receipt["transactionHash"])
        lost_ops = NetCreditOps(chain, stack, lost_issue)
        with pytest.raises(IssuancePending) as interrupted:
            lost_ops.issue_candidate(provider.address, provider_plan)
        assert interrupted.value.transaction_hash and not interrupted.value.reverted
        receipt = anvil.w3.eth.get_transaction_receipt(interrupted.value.transaction_hash)
        evidence = ops.issuance_from_receipt(provider.address, provider_plan, receipt,
                                             expected_hash=interrupted.value.transaction_hash)
        assert (evidence.principal, evidence.premium, evidence.spendable, evidence.pool.lower()) == (
            VECTOR["provider_issuance"], 0, VECTOR["provider_issuance"], ZERO)
        with pytest.raises(ValueError, match="hash"):
            ops.issuance_from_receipt(provider.address, provider_plan, receipt, expected_hash="0x" + "00" * 32)
        with pytest.raises(ValueError, match="candidate"):
            ops.issuance_from_receipt(provider.address, replace(provider_plan, coverage=face - 1), receipt,
                                      expected_hash=_txhex(receipt["transactionHash"]))
        duplicate = dict(receipt)
        duplicate["logs"] = list(receipt["logs"]) * 2
        with pytest.raises(ValueError, match="ambiguous"):
            ops.issuance_from_receipt(provider.address, provider_plan, duplicate,
                                      expected_hash=_txhex(receipt["transactionHash"]))
        assert ops.facility(provider_id).issued == VECTOR["provider_issuance"]
        quote = ops.quote_issue(debtor_id, face)
        assert (quote.premium, quote.reserve_required, quote.max_spendable, quote.reserve_shortfall) == (
            premium, reserve, spendable, reserve)
        candidate = ops.assess_issue_candidate(debtor.address, debtor_id, face, spendable, premium,
                                               reserve_buffer=buffer)
        assert candidate.block_number == chain.w3.eth.block_number
        assert candidate.premium_pool.lower() == pool.address.lower()
        assert (candidate.premium, candidate.reserve_required, candidate.max_spendable) == (premium, reserve, spendable)
        assert candidate.reserve_shortfall == reserve + buffer and candidate.reasons == ("reserve-shortfall",)
        with pytest.raises(ValueError, match="not available"):
            ops.issue_candidate(debtor.address, candidate)
        assert ops.assess_issue_candidate(debtor.address, debtor_id, face, spendable, premium, reserve_buffer=buffer,
                                          block_number=candidate.block_number) == candidate
        assert "premium-limit" in ops.assess_issue_candidate(debtor.address, debtor_id, face, spendable,
                                                             premium - 1).reasons
        with pytest.raises(ValueError, match="foreign"):
            ops.assess_issue_candidate(debtor.address, provider_id, face, spendable, 0)
        with pytest.raises(ValueError, match="unactivated"):
            ops.assess_issue_candidate(debtor.address, debtor_id, face + 1, spendable, premium)
        with pytest.raises(ValueError, match="candidate"):
            ops.assess_issue_candidate(debtor.address, debtor_id, face, 0, premium)
        with pytest.raises(ValueError, match="candidate"):
            ops.assess_issue_candidate(debtor.address, debtor_id, face, spendable, premium, block_number=-1)
        with pytest.raises(ValueError, match="maximum premium"):
            ops.issue(debtor.address, debtor_id, face, spendable, -1, pool.address)
        with pytest.raises(TypeError):
            ops.issue(debtor.address, debtor_id, face, spendable)
        with pytest.raises(TypeError):
            ops.issue(debtor.address, debtor_id, face, spendable, premium)
        with pytest.raises(ValueError, match="insurer pool"):
            ops.issue(debtor.address, debtor_id, face, spendable, premium, "invalid")
        data = buck.encode_abi("issue", args=[debtor_id, face, spendable, premium, pool.address])
        refused = chain.send(debtor.functions.exec(buck.address, bytes.fromhex(data[2:])), expect=Expect.REVERT)
        assert refused["status"] == 0
        assert ops.facility(debtor_id).issued == 0

        # A reserve equal at quote time can decay below the gate in the next
        # block, even when the current quote shows no shortfall.
        _exec(chain, provider, buck, "transfer", [debtor.address, reserve])
        assert ops.quote_issue(debtor_id, face).reserve_shortfall == 0
        now = anvil.w3.eth.get_block("latest")["timestamp"]
        anvil.warp_to(now + 1)
        assert ops.quote_issue(debtor_id, face).reserve_shortfall > 0
        refused = chain.send(debtor.functions.exec(buck.address, bytes.fromhex(data[2:])), expect=Expect.REVERT)
        assert refused["status"] == 0
        _exec(chain, provider, buck, "transfer", [debtor.address, buffer])
        assert ops.quote_issue(debtor_id, face).reserve_shortfall == 0
        before_supply = buck.functions.totalIssued().call()
        before_balance = buck.functions.balanceOf(debtor.address).call()
        rate = VECTOR["premium_rate_bp"]
        _exec(chain, insurer, credit, "updateCredit", [debtor_id, face, face, 0, 0, 0, rate + 1])
        assert ops.quote_issue(debtor_id, face).premium > premium
        stale = chain.send(debtor.functions.exec(buck.address, bytes.fromhex(data[2:])), expect=Expect.REVERT)
        assert stale["status"] == 0 and buck.functions.totalIssued().call() == before_supply
        assert 0 <= before_balance - buck.functions.balanceOf(debtor.address).call() <= 10  # time may advance
        assert ops.facility(debtor_id).activated == ops.facility(debtor_id).issued == 0
        assert buck.functions.balanceOf(pool.address).call() == 0
        _exec(chain, insurer, credit, "updateCredit", [debtor_id, face, face, 0, 0, 0, rate])
        debtor_plan = ops.assess_issue_candidate(debtor.address, debtor_id, face, spendable, premium)
        assert debtor_plan.reasons == ()
        replacement = chain.deploy("SimLP")
        _exec(chain, insurer, registry, "bindContract", [replacement.address, g, e, True, True])
        _exec(chain, insurer, buck, "setInsurerPool", [replacement.address])
        def issue_either(owner, contract, method, args):
            encoded = contract.encode_abi(method, args=args)
            return chain.send(actors[owner.lower()].functions.exec(contract.address, bytes.fromhex(encoded[2:])),
                              expect=Expect.EITHER)
        either_ops = NetCreditOps(chain, stack, issue_either)
        with pytest.raises(IssuancePending) as changed_pool:
            either_ops.issue_candidate(debtor.address, debtor_plan)
        assert changed_pool.value.reverted and changed_pool.value.transaction_hash
        assert buck.functions.totalIssued().call() == before_supply
        assert ops.facility(debtor_id).activated == ops.facility(debtor_id).issued == 0
        assert buck.functions.balanceOf(replacement.address).call() == 0
        _exec(chain, insurer, buck, "setInsurerPool", [pool.address])
        debtor_plan = ops.assess_issue_candidate(debtor.address, debtor_id, face, spendable, premium)
        issued_event = ops.issue_candidate(debtor.address, debtor_plan)
        assert (issued_event.principal, issued_event.premium, issued_event.pool.lower()) == (issued, premium, pool.address.lower())
        issue_receipt = issued_event.receipt
        issued_at = anvil.w3.eth.get_block(issue_receipt["blockNumber"])["timestamp"]
        state = ops.facility(debtor_id)
        assert (state.face, state.activated, state.issued, state.premium_nominal) == (
            face, face, issued, premium)
        assert state.insurer_pool == pool.address
        assert buck.functions.balanceOf(pool.address).call() == premium
        assert buck.functions.balanceOf(pool2.address).call() == 0

        # The same household has a separate facility with another insurer.
        _exec(chain, debtor, credit, "setCreditIssuer", [insurer2.address, True])
        _exec(chain, insurer2, credit, "createCredit",
              [debtor.address, 0, face, face, 0, 0, 0, VECTOR["second_premium_rate_bp"]])
        second_id = credit.functions.tokenOfOwnerByIndex(debtor.address, 1).call()
        second_quote = ops.quote_issue(second_id, face)
        assert second_quote.premium == VECTOR["second_premium"]
        assert second_quote.reserve_shortfall == 0
        ops.issue(debtor.address, second_id, face, VECTOR["second_spendable"], second_quote.premium, pool2.address)
        second = ops.facility(second_id)
        assert second.issued == VECTOR["second_issued"]
        assert second.insurer_pool == pool2.address
        assert buck.functions.balanceOf(pool2.address).call() == VECTOR["second_premium"]

        # Receiving BUCK is not a repayment: only the explicitly authorized
        # selected-NFT reduction changes issued principal.
        _exec(chain, provider, buck, "transfer", [debtor.address, reduction])
        assert ops.facility(debtor_id).issued == issued
        assert ops.facility(second_id).issued == VECTOR["second_issued"]
        anvil.warp_to(issued_at + YEAR)
        household = ops.household(debtor.address, [debtor_id, second_id])
        assert household.issued == issued + VECTOR["second_issued"]
        assert household.premium_nominal == premium + VECTOR["second_premium"]
        assert household.spendable == buck.functions.balanceOf(debtor.address).call()
        with pytest.raises(ValueError, match="duplicate"):
            ops.household(debtor.address, [debtor_id, debtor_id])
        with pytest.raises(ValueError, match="foreign"):
            ops.household(debtor.address, [provider_id])
        with pytest.raises(ValueError, match="non-Carrying"):
            ops.household(pool.address, [])
        capped = ops.plan_reduction(debtor.address, debtor_id, issued)
        assert capped.reason == "ready" and 0 < capped.principal < issued
        affordable = ops.plan_reduction(debtor.address, debtor_id, reduction, buffer=household.spendable - 1000)
        assert affordable.reason == "ready" and 0 < affordable.principal < reduction
        assert affordable.principal * Q - affordable.minimum_relief_scaled <= 1000 * Q
        empty = ops.plan_reduction(debtor.address, debtor_id, reduction, buffer=household.spendable + 1)
        assert empty.reason == "owner-funds" and empty.principal == 0
        with pytest.raises(ValueError, match="invalid"):
            ops.plan_reduction(debtor.address, debtor_id, reduction, relief_slippage_bp=1.5)
        assert ops.facility(debtor_id).issued == issued  # planning is read-only
        before_pool_net = buck.functions.netBalanceOf(pool.address).call()
        assert before_pool_net < premium
        relief, _, exceeds = ops.quote_reduction(debtor_id, reduction)
        assert 0 < relief < reduction * Q and not exceeds
        plan = ops.plan_reduction(debtor.address, debtor_id, reduction)
        assert plan.reason == "ready" and plan.principal == reduction
        ops.authorize_reduction(debtor.address, debtor_id, reduction, 0, issued_at + YEAR + 3600)
        assert ops.plan_reduction(debtor.address, debtor_id, reduction).reason == "authorization-pending"
        ops.cancel_reduction(debtor.address, debtor_id)
        # A failed second operation must leave the authorization for an
        # explicit retry; neither the purchase nor the first tx retires debt.
        def fail_reduce(owner, target, method, args):
            if target.address == fund.address:
                raise RuntimeError("simulated settlement transport failure")
            return _exec(chain, actors[owner.lower()], target, method, args)
        failing = NetCreditOps(chain, stack, fail_reduce)
        with pytest.raises(ReductionPending) as failed:
            failing.settle_plan(debtor.address, plan, issued_at + YEAR + 3600)
        assert failed.value.plan == plan
        assert buck.functions.reductionAuthorization(debtor_id).call()[0] == reduction
        assert ops.facility(debtor_id).issued == issued
        ops.cancel_reduction(debtor.address, debtor_id)
        pending = failing.retire_household(debtor.address, [debtor_id, second_id], reduction,
                                          issued_at + YEAR + 3600)
        assert pending.status == "authorization-pending" and pending.blocked_token_id == debtor_id
        assert pending.retired == 0 and pending.pending_principal == reduction and not pending.settlements
        assert pending.pending_deadline == issued_at + YEAR + 3600
        assert pending.pending_transaction_hash is None and not pending.pending_reverted
        assert buck.functions.reductionAuthorization(debtor_id).call()[0] == reduction
        reduced = ops.reduce_authorized(debtor.address, debtor_id, reduction)
        refund = net_transfer_value(buck, reduced, pool.address, debtor.address)
        assert refund.count == 1 and refund.nominal == VECTOR["nominal_refund"]
        assert 0 < refund.net_scaled < refund.nominal * Q
        after = ops.facility(debtor_id)
        assert after.issued == issued - reduction
        assert after.premium_nominal == premium - VECTOR["nominal_refund"]
        assert buck.functions.balanceOf(pool.address).call() == premium - VECTOR["nominal_refund"]
        assert ops.facility(second_id).issued == VECTOR["second_issued"]
        assert buck.functions.balanceOf(pool2.address).call() == VECTOR["second_premium"]
        # A household batch uses real events, consuming the selected NFTs in
        # order; it does not add their common wallet balance twice.
        deadline = issued_at + YEAR + 3600
        first = ops.retire_household(debtor.address, [second_id], 9 * RAW, deadline)
        assert first.status == "complete" and first.retired == 9 * RAW
        batch = ops.retire_household(debtor.address, [second_id, debtor_id], 2 * RAW, deadline)
        assert batch.status == "complete" and batch.retired == 2 * RAW and len(batch.settlements) == 2
        assert ops.facility(second_id).issued == 0
        assert ops.facility(debtor_id).issued == issued - 2 * RAW
        assert batch.relief_scaled + batch.owner_cost_scaled == batch.retired * Q

        # Actual fund cash changing changes the inferred current rate; the
        # sample observes the contract's own mark rather than a fixed 2%.
        before_rate = ops.quote_relief_rate(debtor_id, RAW)
        _exec(chain, provider, buck, "transfer", [fund.address, 2 * RAW])
        after_rate = ops.quote_relief_rate(debtor_id, RAW)
        assert after_rate.annual_rate_wad > before_rate.annual_rate_wad

        # Quote the existing insurer pool after it has aged, including the
        # dilution from a new full-value premium. With this cash-only insurer
        # the estimate agrees with the pool after actual issuance.
        _exec(chain, insurer, credit, "createCredit", [debtor.address, 0, face, face, 0, 0, 0, 50])
        third_id = credit.functions.tokenOfOwnerByIndex(debtor.address, 2).call()
        refund = ops.quote_premium_refund(third_id, face)
        assert refund.reason == "cash-only" and refund.pool == pool.address
        assert refund.premium == 5 * RAW and 0 < refund.refund_fraction_wad < 10**18
        third_plan = ops.assess_issue_candidate(debtor.address, third_id, face, RAW, refund.premium)
        def omit_events(owner, contract, method, args):
            receipt = dict(_exec(chain, actors[owner.lower()], contract, method, args))
            receipt["logs"] = []
            return receipt
        with pytest.raises(IssuancePending) as omitted:
            NetCreditOps(chain, stack, omit_events).issue_candidate(debtor.address, third_plan)
        assert omitted.value.transaction_hash and not omitted.value.reverted
        actual = anvil.w3.eth.get_transaction_receipt(omitted.value.transaction_hash)
        verified = ops.issuance_from_receipt(debtor.address, third_plan, actual,
                                             expected_hash=omitted.value.transaction_hash)
        assert verified.premium == refund.premium and verified.principal == RAW + refund.premium
        gross, net = buck.functions.accountBalancesScaled(pool.address).call()
        assert abs(net * 10**18 // gross - refund.refund_fraction_wad) < 10**10
        # Already-activated facility room is new issuance, not wallet money;
        # exercising it with zero additional coverage has no second premium.
        before = ops.facility(third_id)
        existing = ops.quote_issue(third_id, 0)
        assert existing.premium == existing.reserve_required == 0 and existing.max_spendable >= RAW
        activated_menu = ops.issue_coverage_menu(debtor.address, third_id, RAW, 0)
        assert len(activated_menu) == 1 and not activated_menu[0].reasons and activated_menu[0].coverage == 0
        capacity = ops.quote_household_activated_capacity(debtor.address, [debtor_id, third_id])
        assert not capacity.maturities_pending
        assert capacity.block_number == chain.w3.eth.block_number
        assert capacity.facilities == ((debtor_id, ops.quote_issue(debtor_id, 0).max_spendable),
                                       (third_id, existing.max_spendable))
        assert capacity.capacity == min(sum(amount for _, amount in capacity.facilities), capacity.shared_limit)
        with pytest.raises(ValueError, match="duplicate"):
            ops.quote_household_activated_capacity(debtor.address, [third_id, third_id])
        with pytest.raises(ValueError, match="foreign"):
            ops.quote_household_activated_capacity(debtor.address, [provider_id])
        ops.issue(debtor.address, third_id, 0, RAW, 0, ZERO)
        after = ops.facility(third_id)
        assert after.activated == before.activated and after.premium_nominal == before.premium_nominal
        assert after.issued == before.issued + RAW
        _exec(chain, debtor, credit, "setCreditIssuer", [provider.address, True])
        _exec(chain, provider, credit, "createCredit", [debtor.address, 0, face, face, 0, 0, 0, 50])
        fourth_id = credit.functions.tokenOfOwnerByIndex(debtor.address, 3).call()
        missing = ops.quote_premium_refund(fourth_id, face)
        assert missing.reason == "no-insurer-pool" and missing.refund_fraction_wad is None
        assert "insurer-pool-unavailable" in ops.assess_issue_candidate(debtor.address, fourth_id, face, RAW,
                                                                        face).reasons
        # Unactivated coverage is measured at the current marked asset value,
        # not at the original face amount; activated and unactivated portions
        # share the same depreciation schedule.
        v = VECTOR["depreciating_candidate"]
        start = anvil.w3.eth.get_block("latest")["timestamp"]
        _exec(chain, insurer, credit, "createCredit", [debtor.address, 0, v["face"], 0, 1,
                                                        v["depreciation_bp"], start, v["premium_rate_bp"]])
        depreciating_id = credit.functions.tokenOfOwnerByIndex(debtor.address, 4).call()
        ops.issue(debtor.address, depreciating_id, v["initial_coverage"], v["initial_spendable"],
                  v["initial_premium_limit"], pool.address)
        anvil.warp_to(start + v["comparison_year"] * YEAR)
        marked = credit.functions.depreciatedFaceValue(depreciating_id).call()
        active = credit.functions.currentValue(depreciating_id).call()
        valued = ops.quote_household_activated_capacity(debtor.address, [depreciating_id])
        assert marked == 80 * RAW and active == 32 * RAW
        assert valued.unactivated_present == marked - active == 48 * RAW

        # At year seven the old principal exceeds the current marked K limit.
        # Some new coverage and its premium must repair this NFT before it can
        # issue. The remaining coverage still makes a positive issue possible.
        anvil.warp_to(start + v["repair_year"] * YEAR)
        requested = v["requested_spendable"]
        blocked = ops.assess_issue_candidate(debtor.address, depreciating_id, 0, requested, 0, reserve_buffer=buffer)
        assert blocked.shortfall_before == v["shortfall_before"]
        assert blocked.premium_pool == ZERO
        assert blocked.reserve_shortfall == 0  # no premium, so no reserve buffer is required
        assert "insufficient-capacity" in blocked.reasons
        partial = ops.assess_issue_candidate(debtor.address, depreciating_id, v["partial_coverage"], requested, RAW)
        assert partial.shortfall_after_premium > 0 and "insufficient-capacity" in partial.reasons
        repaired = ops.assess_issue_candidate(debtor.address, depreciating_id, v["remaining_coverage"], requested, 2 * RAW,
                                              reserve_buffer=buffer)
        assert repaired.block_number == chain.w3.eth.block_number
        assert repaired.shortfall_before == v["shortfall_before"] and repaired.shortfall_after_premium == 0
        assert repaired.premium == v["repair_premium"] and repaired.max_spendable == v["repair_max_spendable"]
        assert repaired.reasons == ()
        # The exact repair premium can turn a seemingly profitable mortgage
        # refinance into a loss; do not score the aggregate coverage estimate.
        added_mark = (repaired.after_coverage - repaired.before_coverage) / RAW
        terms = carry.RefiTerms(debt=10, rate=.12, face=100, premium_bp=100, joined=True, payback_y=10,
                                 k=repaired.current_k / 10**18, headroom=0, unactivated=added_mark,
                                 expected_relief_rate=.02, expected_refund_growth=-.02,
                                 wallet_already_issued=True, issuance_limit=1000)
        naive = carry.refinance(terms, 1, 1)
        full_refund = ops.quote_premium_refund(depreciating_id, repaired.coverage, block_number=repaired.block_number)
        assert full_refund.reason == "cash-only" and full_refund.premium == repaired.premium
        accurate = carry.refinance_quoted(terms, 1, 0, 1, added_mark, repaired.premium / RAW,
                                          premium_refund_fraction=full_refund.refund_fraction_wad / 10**18)
        assert naive.go and not accurate.go and accurate.deposit == 1.8
        menu = ops.issue_coverage_menu(debtor.address, depreciating_id, requested, 2 * RAW, reserve_buffer=buffer)
        assert len(menu) <= 15 and all(item.block_number == repaired.block_number for item in menu)
        assert menu[-1] == ops.assess_issue_candidate(debtor.address, depreciating_id, menu[-1].coverage,
                                                       requested, 2 * RAW, reserve_buffer=buffer,
                                                       block_number=repaired.block_number)
        ready = [item for item in menu if not item.reasons]
        choices = []
        for item in ready:
            refund = ops.quote_premium_refund(depreciating_id, item.coverage, block_number=item.block_number)
            assert refund.block_number == item.block_number and refund.premium == item.premium
            assert refund.reason == "cash-only" and refund.pool.lower() == item.premium_pool.lower()
            choices.append(dict(usd=1, wallet_used=0, new_spendable=1,
                                present_coverage=(item.after_coverage - item.before_coverage) / RAW,
                                premium=item.premium / RAW,
                                premium_refund_fraction=refund.refund_fraction_wad / 10**18))
        selected, value = carry.best_quoted_refinance(terms, choices)
        assert selected is not None and value.go
        chosen = ready[selected]
        assert 30 * RAW < chosen.coverage < 32 * RAW and chosen.premium < repaired.premium
        # Bind the actual quoted premium before submitting the economically
        # preferred candidate; a later price increase must not be accepted.
        chosen = replace(chosen, max_premium=chosen.premium)
        previous = ops.facility(depreciating_id)
        result_issue = ops.issue_candidate(debtor.address, chosen)
        updated = ops.facility(depreciating_id)
        assert updated.activated == previous.activated + chosen.coverage
        assert updated.issued == previous.issued + result_issue.principal
        assert result_issue.premium <= chosen.premium
        fresh = ops.assess_issue_candidate(debtor.address, depreciating_id, 0, requested, 0)
        assert fresh.prior_principal == updated.issued  # no snapshot cache survives issuance
        assert not chain.mismatches
