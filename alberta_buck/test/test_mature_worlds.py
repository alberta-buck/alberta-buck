"""The mature cast must move BUCKs without losing them in a swap helper."""
from dataclasses import replace

import pytest

pytest.importorskip("pyrevm")

from alberta_buck.sim import identity as idmod
from alberta_buck.sim import experiment as expmod
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.chain import load_artifact
from alberta_buck.sim.deploy import deploy
from alberta_buck.sim.direct_mint import _DMBase, _transit
from alberta_buck.sim.equilibrium_agents import FatCreditBorrowerAgent, _ProxyAgent, _simlp_endow_buck
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
from alberta_buck.sim.scenario import ROUTING


@pytest.fixture
def world():
    scenario                    = replace(ROUTING, days=2, agents={})
    rng                         = idmod.seeded_rng(scenario.seed)
    with PyrevmAnvil(port=0) as evm:
        chain                   = Chain(evm.w3, evm.w3.eth.accounts[0])
        d                       = deploy(chain, evm, scenario, rng, verbose=False, basket_impl="equity-ops")
        yield d, scenario, rng


def test_proxy_swaps_when_pool_seeder_has_no_spendable_bucks(world):
    d, _, _                     = world
    agent                       = _ProxyAgent(0)
    agent._bind_proxy(d)
    escrow                      = agent._new_proxy(d)
    _simlp_endow_buck(d, escrow.address, 100 * 10 ** 6)
    # Lower K below the helper's lien. The old two-transfer swap would repay
    # its lien and could not forward the input; a proxy callback needs none.
    d.chain.send(d.kctrl.functions.setRails(2 * 10 ** 17, 2 * 10 ** 17), sender=d.gov)
    helper                      = d.buck.functions.signedBalanceOf(d.simlp.address).call()
    assert d.buck.functions.balanceOf(d.simlp.address).call() == 0
    agent._swap_via_simlp(d, d.pool_ub, d.buck, 200 * 10 ** 6, agent.address, from_proxy=escrow)
    assert d.buck.functions.balanceOf(escrow.address).call() == 0
    assert d.chain.balance_of(d.usdc, agent.address) > 99 * 10 ** 6
    assert d.buck.functions.signedBalanceOf(d.simlp.address).call() == helper


@pytest.mark.parametrize("refused", [False, True])
def test_depositor_transit_forwards_or_returns_every_buck(world, monkeypatch, refused):
    d, scenario, rng            = world
    agent                       = _DMBase(0)
    agent.setup(d, scenario, rng)
    idmod.identity_approve(d.chain, d.reg, d.buck, agent.account, agent.register_args,
                           agent.identity_sk, d.simlp.address, idmod.seeded_rng(987))
    _simlp_endow_buck(d, agent.address, 100 * 10 ** 6)
    agent._proceeds             = True
    helper                      = d.buck.functions.signedBalanceOf(d.simlp.address).call()
    before                      = d.buck.functions.balanceOf(agent.address).call()
    ctr                         = {}
    send                        = d.chain.send

    def maybe_refuse(fn, *args, **kwargs):
        if refused and fn.fn_name == "swap":
            raise RuntimeError("test: refused swap")
        return send(fn, *args, **kwargs)

    monkeypatch.setattr(d.chain, "send", maybe_refuse)
    agent._sell_proceeds(d, ctr)
    transit                     = _transit(d)
    assert d.buck.functions.creditLimit(transit.address).call() == 0
    assert d.buck.functions.signedBalanceOf(transit.address).call() == 0
    assert d.buck.functions.signedBalanceOf(d.simlp.address).call() == helper
    after                       = d.buck.functions.balanceOf(agent.address).call()
    if refused:
        assert after == before
        assert ctr.get("dmBuckSold", 0) == 0
        assert "refused swap" in ctr["dm_sell_err"]
    else:
        assert "dm_sell_err" not in ctr
        assert before - after == ctr["dmBuckSold"] == 100 * 10 ** 6
        assert d.chain.balance_of(d.usdc, agent.address) > 99 * 10 ** 6


def borrower(world, mode="gate", budget=3):
    d, scenario, rng            = world
    scenario.experiment         = expmod.load(None, sets=[
        f"agents.FatCreditBorrowerAgent.{key}={value}" for key, value in dict(
            reserve_mode=mode, fund_budget_m=budget, internal_share=0, shock_prob=0,
            mid_frac=-0.5, util_target=0.7, band=0, open="market", max_impact_bp=0).items()])
    agent                       = FatCreditBorrowerAgent(0)
    agent.setup(d, scenario, rng)
    return agent


@pytest.mark.parametrize("mode", ["gate", "rolling", "none"])
def test_borrower_reserve_matches_the_selected_gate(world, mode):
    d, scenario, _              = world
    agent                       = borrower(world, mode)
    k                           = d.kctrl.functions.buckK().call() / 1e18
    factor                      = d.kctrl.functions.fundingFactor().call() / 1e18
    ctr                         = {}
    agent.act(d, scenario, 0, 0, ctr)
    sold                        = ctr["fatIssued"]
    held                        = agent._reserve_balance(d)
    assert sold > 0
    assert not any(key.endswith("_err") for key in ctr), ctr
    if mode == "gate":
        expected                = sold * agent.gate_deposit / (k - agent.gate_deposit) * factor
        assert held == pytest.approx(expected, rel=0.001, abs=10)
        assert agent.reserve_req == 0 and not agent._tranches
        assert agent.pending_release == held
        agent._release_pending(d, 1.1, ctr)
        assert agent._reserve_balance(d) == held  # do not release into a cheap market
    elif mode == "rolling":
        assert agent.reserve_req > sold // 2
        assert agent._tranches[0][0] == sold
        assert agent.pending_release == 0
    else:
        assert held == agent.reserve_req == agent.pending_release == 0
        assert ctr.get("fatPreFundBought", 0) == 0


def test_gate_throttles_unfunded_draws_and_cannot_draw_below_the_deposit_share(world):
    d, scenario, _              = world
    agent                       = borrower(world, budget=0)
    ctr                         = {}
    agent.act(d, scenario, 0, 0, ctr)
    assert ctr["fatThrottled"] == 1 and ctr.get("fatIssued", 0) == 0
    d.chain.send(d.usdc.functions.mint(agent.address, 10 ** 12))
    agent.gate_deposit          = 0.9
    agent.act(d, scenario, 0, 1, ctr)
    assert ctr.get("fatIssued", 0) == 0


def test_reserve_settings_preserve_the_borrowers_random_stream(world):
    states                      = []
    for mode in ("gate", "rolling", "none"):
        agent                   = borrower(world, mode)
        states.append(agent._rng.getstate())
    assert states[0] == states[1] == states[2]


def test_placed_borrower_moves_the_lien_without_moving_the_pool(world, monkeypatch):
    from types import SimpleNamespace

    d, scenario, _              = world
    agent                       = borrower(world)
    agent.open_mode             = "placed"
    pool                        = d.w3.eth.contract(address=d.pool_ub, abi=load_artifact("UniswapV3Pool")[0])
    slot                        = pool.functions.slot0().call()
    supply                      = d.buck.functions.totalSupply().call()
    helper                      = d.buck.functions.signedBalanceOf(d.simlp.address).call()
    target                      = int(agent.util_target * d.buck.functions.creditLimit(agent.address).call()
                                      * agent._cap_frac(0, scenario.days))
    ctr                         = {}
    agent.bootstrap(d, scenario, ctr)
    assert ctr["fatPlaced"] == target
    assert d.buck.functions.signedBalanceOf(agent.address).call() == -target
    assert d.buck.functions.signedBalanceOf(d.simlp.address).call() == helper + target
    assert d.buck.functions.totalSupply().call() == supply
    assert pool.functions.slot0().call() == slot
    assert agent._reserve_balance(d) == agent.pending_release == agent.reserve_req == 0
    monkeypatch.setattr(d, "basket", SimpleNamespace(functions=SimpleNamespace(
        basketValueInBuck=lambda: SimpleNamespace(call=lambda: 10 ** 18))))
    agent.act(d, scenario, 0, 0, ctr)
    assert ctr.get("fatIssued", 0) == 0


def test_opening_sale_is_capped_at_the_helpers_lien(world):
    d, _, _                     = world
    agent                       = _ProxyAgent(0)
    agent._bind_proxy(d)
    agent._proxy_exec(d, d.credit.address, d.credit.encode_abi(
        "setCreditIssuer", args=[d.chain.deployer, True]))
    d.chain.send(d.credit.functions.createCredit(
        agent.address, 0, 10 ** 15, 0, 0, 0, d.w3.eth.get_block("latest")["timestamp"], 0))
    agent._proxy_exec(d, d.buck.address, d.buck.encode_abi("mint(uint256)", args=[2 ** 256 - 1]))
    lien                        = -d.buck.functions.signedBalanceOf(d.simlp.address).call()
    supply                      = d.buck.functions.totalSupply().call()
    assert agent._otc_sell(d, 2 * lien) == lien
    assert d.buck.functions.signedBalanceOf(d.simlp.address).call() == 0
    assert d.buck.functions.signedBalanceOf(agent.address).call() == -lien
    assert d.buck.functions.totalSupply().call() == supply
    assert agent._otc_sell(d, 100 * 10 ** 6) == 0
    assert agent._otc_buy(d, 100 * 10 ** 6) == 100 * 10 ** 6
    assert d.buck.functions.signedBalanceOf(d.simlp.address).call() == -100 * 10 ** 6


def test_borrower_sales_stay_inside_the_impact_cap_and_resume_next_tick(world):
    d, scenario, _              = world
    agent                       = borrower(world, "none")
    agent.max_impact_bp          = 100
    pool                        = d.w3.eth.contract(address=d.pool_ub, abi=load_artifact("UniswapV3Pool")[0])
    ctr                         = {}
    for tick in (0, 1):
        before                  = pool.functions.slot0().call()[0]
        issued                  = ctr.get("fatIssued", 0)
        agent.act(d, scenario, 0, tick, ctr)
        after                   = pool.functions.slot0().call()[0]
        assert ctr["fatIssued"] > issued
        assert abs((after / before) ** 2 - 1) < 0.0102


@pytest.mark.parametrize("bvib,use", [(1., .7), (.975, .85), (.95, 1.)])
def test_dear_buck_brings_out_unused_credit(world, monkeypatch, bvib, use):
    from types import SimpleNamespace

    d, scenario, _              = world
    agent                       = borrower(world, "none")
    monkeypatch.setattr(agent, "_cap_frac", lambda *_: 1.)
    monkeypatch.setattr(d, "basket", SimpleNamespace(functions=SimpleNamespace(
        basketValueInBuck=lambda: SimpleNamespace(call=lambda: round(bvib * 10 ** 18)))))
    limit                       = d.buck.functions.creditLimit(agent.address).call()
    ctr                         = {}
    agent.act(d, scenario, 0, 0, ctr)
    drawn                       = -d.buck.functions.signedBalanceOf(agent.address).call()
    assert drawn == pytest.approx(use * limit, abs=2)
    if bvib < 1:
        # Returning to parity lowers the target and uses the existing retire leg.
        monkeypatch.setattr(d, "basket", SimpleNamespace(functions=SimpleNamespace(
            basketValueInBuck=lambda: SimpleNamespace(call=lambda: 10 ** 18))))
        agent.act(d, scenario, 0, 1, ctr)
        assert -d.buck.functions.signedBalanceOf(agent.address).call() < drawn
        assert ctr["fatRetired"] > 0


def test_dear_response_can_be_disabled(world, monkeypatch):
    from types import SimpleNamespace

    d, scenario, _              = world
    agent                       = borrower(world, "none")
    agent.dear_full              = 0
    monkeypatch.setattr(agent, "_cap_frac", lambda *_: 1.)
    monkeypatch.setattr(d, "basket", SimpleNamespace(functions=SimpleNamespace(
        basketValueInBuck=lambda: SimpleNamespace(call=lambda: 9 * 10 ** 17))))
    limit                       = d.buck.functions.creditLimit(agent.address).call()
    agent.act(d, scenario, 0, 0, {})
    assert -d.buck.functions.signedBalanceOf(agent.address).call() == int(.7 * limit)
