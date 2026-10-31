"""A linked BuckNet deployment against the independent demurrage reference.

This is a cash-only, zero-premium accounting smoke test. It is not a household
economy, market, insurer model, leveraged basket, or five-year validation.
"""

import json
import math
import shutil
from pathlib import Path

import pytest

from alberta_buck.sim.anvil import Anvil
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.demurrage_reference import DemurrageLedger
from buck_core import deploy_net_stack, net_transfer_value, NetCreditOps, ReductionPending
from buck_core.session import Expect

YEAR = 365 * 24 * 60 * 60 + 6 * 60 * 60
RAW = 10**6
Q = 10**36
ROOT = Path(__file__).resolve().parents[2]
VECTOR = json.loads((ROOT / "test/fixtures/bucknet-credit-operations.json").read_text())
REQUIRED = ("BuckNet", "BuckNetMaturity", "BuckNetTransfers", "BuckNetQuoter", "JubileeFund",
            "IdentityRegistryHarness", "BuckCredit", "BuckKControllerStatic", "UniswapV3Factory",
            "BuckKControllerDirect", "BuckBasketEquity", "SimLP")


def _exec(chain, actor, target, method, args):
    data = target.encode_abi(method, args=args)
    return chain.send(actor.functions.exec(target.address, bytes.fromhex(data[2:])))


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
        issued = _exec(chain, actor, buck, "issue", [0, 100 * RAW, 100 * RAW])
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
        _exec(chain, actor, buck, "issue", [1, 100 * RAW, 100 * RAW])
        _exec(chain, actor, buck, "transfer", [fund.address, 40 * RAW])
        capped = ops.quote_relief_rate(0, RAW)
        assert capped.capped and capped.relief_scaled == RAW * Q and capped.annual_rate_wad is not None
        anvil.warp_to(issued_at + 51 * YEAR)
        assert buck.functions.maturitiesPending().call()
        with pytest.raises(Exception):
            ops.quote_relief_rate(0, RAW)  # stale claim denominator must not become a forecast
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
        ops.issue(provider.address, provider_id, face, VECTOR["provider_issuance"])
        quote = ops.quote_issue(debtor_id, face)
        assert (quote.premium, quote.reserve_required, quote.max_spendable, quote.reserve_shortfall) == (
            premium, reserve, spendable, reserve)
        data = buck.encode_abi("issue", args=[debtor_id, face, spendable])
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
        issue_receipt = ops.issue(debtor.address, debtor_id, face, spendable)
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
        ops.issue(debtor.address, second_id, face, VECTOR["second_spendable"])
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
        ops.issue(debtor.address, third_id, face, RAW)
        gross, net = buck.functions.accountBalancesScaled(pool.address).call()
        assert abs(net * 10**18 // gross - refund.refund_fraction_wad) < 10**10
        _exec(chain, debtor, credit, "setCreditIssuer", [provider.address, True])
        _exec(chain, provider, credit, "createCredit", [debtor.address, 0, face, face, 0, 0, 0, 50])
        fourth_id = credit.functions.tokenOfOwnerByIndex(debtor.address, 3).call()
        missing = ops.quote_premium_refund(fourth_id, face)
        assert missing.reason == "no-insurer-pool" and missing.refund_fraction_wad is None
        assert not chain.mismatches
