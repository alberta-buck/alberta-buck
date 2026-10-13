"""The mature cast must move BUCKs without losing them in a swap helper."""
from dataclasses import replace

import pytest

pytest.importorskip("pyrevm")

from alberta_buck.sim import identity as idmod
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.deploy import deploy
from alberta_buck.sim.direct_mint import _DMBase, _transit
from alberta_buck.sim.equilibrium_agents import _ProxyAgent, _simlp_endow_buck
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
