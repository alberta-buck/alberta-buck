"""Short real economy -> canonical observations -> completed JSON and PDF.

The report threshold is shortened only in this test. This is integration
coverage for the existing runner, not five-year policy or household evidence.
"""
from dataclasses import replace
import json
import re

import pytest
from web3 import Web3

from alberta_buck.sim import experiment as expmod, report
from alberta_buck.sim.loop import run
from alberta_buck.sim.plot_rebalancing import household_position
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
from alberta_buck.sim.scenario import ROUTING


@pytest.mark.parametrize("keeper", [False, True], ids=["builtin", "agent"])
def test_short_canonical_economy_records_actual_books_and_renders_pdf(monkeypatch, tmp_path, keeper):
    monkeypatch.delenv("SIM_WHEEL_ARB", raising=False)
    monkeypatch.setattr(report, "FIVE_YEARS", 3)
    report.preflight()
    from matplotlib.backends.backend_pdf import PdfPages
    savefig = PdfPages.savefig
    plotted_returns = []

    def inspect_page(pdf, figure=None, **kwargs):
        for axis in figure.axes:
            if axis.get_title().startswith("Observed basket positions:"):
                plotted_returns.append(list(axis.lines[0].get_ydata()))
        return savefig(pdf, figure, **kwargs)

    monkeypatch.setattr(PdfPages, "savefig", inspect_page)
    scenario = replace(ROUTING, name="canonical-report-integration", days=3, ticks_per_day=4,
                       tokens=[(*ROUTING.tokens[0][:3], 7500), (*ROUTING.tokens[1][:3], 2500)],
                       csv_files=ROUTING.csv_files[:2], agents={"BootstrapDMAgent": 2, **({"BasketWheelAgent": 1} if keeper else {})})
    scenario.experiment = expmod.load(None, sets=["deploy.target_buck_m=1", "deploy.target_buck_lp_m=1",
                                                 "machine.desk_capital_usd=1000"])
    deployments, observations = [], []

    def beginning(day, d, agents, ctr):
        if not deployments:
            deployments.append(d)

    def observed(frame):
        d = deployments[0]
        buck, credit, basket, fund = d.buck, d.credit, d.basket, d.net_stack.fund
        net = frame["net"]
        assert net["block"] == d.w3.eth.block_number
        issued = sum(credit.functions.issuanceOf(credit.functions.tokenByIndex(i).call()).call()[0]
                     for i in range(credit.functions.totalSupply().call()))
        assert net["active_principal"] == issued
        assert frame["supply"] == net["total_issued"] == issued + net["deferred_principal"]
        assert frame["simlp_drawn"] == net["simlp_principal"] > net["simlp_cash"] > 0
        assert int(net["jubilee"]["net_scaled"]) == buck.functions.netBalanceScaled(fund.address).call()
        assert net["jubilee"]["cash"] == buck.functions.netBalanceOf(fund.address).call()
        assert net["jubilee"]["publication"]["ready"]
        assert net["jubilee"]["relief_position_mark"] == fund.functions.reliefPositionMark().call()
        credit_mark = d.net_credit_ops.mark_household(basket.address, [basket.functions.creditId().call()])
        assert credit_mark.state.issued == frame["eq_lien"] and credit_mark.relief_scaled > 0
        assert int(net["claim_seconds"]) == buck.functions.claimSeconds().call()
        assert int(net["available_relief_scaled"]) == buck.functions.availableReliefScaled().call()
        assert frame["basketNav"] == basket.functions.gross().call() + basket.functions.liquidity().call()
        assert frame["eq_equity"] == frame["basketNav"] - frame["eq_lien"] + frame["eq_unpaid_relief"]
        held = 0
        for i, pool in enumerate(d.pool_buck):
            gross, net_pool = buck.functions.accountBalancesScaled(pool).call()
            nominal = basket.functions.marks(i, basket.functions.liquidityOf(i).call()).call()[0]
            held += nominal * net_pool // gross // 2
        assert frame["eq_held_buck"] == held
        assert frame["eq_carried_fee"] == frame["eq_held_buck_nominal"] - held > 0
        assert frame["lp"]["bootstrap"][1] > 0
        assert len(frame["basket_positions"]) == 2
        for owner in frame["basket_positions"] + [net["jubilee"]["position"]]:
            assert owner["mark"] == sum(basket.functions.valueOf(p["id"]).call() for p in owner["positions"])
            assert owner["basis"] == sum(basket.functions.holdings(p["id"]).call()[1] for p in owner["positions"])
        basis, mark, roi = household_position(frame)
        assert basis > 0 and mark > 0
        assert roi == pytest.approx((mark - basis) / basis)
        observations.append(frame)

    with PyrevmAnvil(port=0) as evm:
        summary = run(scenario, evm, out_path=tmp_path / "canonical.json", verbose=False,
                      on_day_start=beginning, on_frame=observed)
        data = json.loads((tmp_path / "canonical.json").read_text())
        meta = data["meta"]["buck_contract"]
        assert meta["accounting"] == "net" and meta["artifact"] == "Buck"
        assert meta["scale"] == str(10**36)
        assert meta["model"] == Web3.to_hex(Web3.keccak(text="Buck/compounded-cash/per-facility-jubilee/v1"))
        for role in list(meta["roles"].values()) + list(meta["libraries"].values()):
            assert role["code_hash"] == Web3.to_hex(Web3.keccak(evm.w3.eth.get_code(role["address"])))
        assert len(meta["libraries"]) == 5
        # A real price excursion makes publication refuse without spending
        # money, retiring principal or changing the last published mark.
        d = deployments[0]
        pool = d.chain.at("UniswapV3Pool", d.pool_buck[0])
        t0, t1 = pool.functions.token0().call(), pool.functions.token1().call()
        zero = t0.lower() == d.buck.address.lower()
        amount = d.buck.functions.balanceOf(d.simlp.address).call() // 2
        d.chain.send(d.simlp.functions.swap(pool.address, d.simlp.address, zero, amount,
                     4295128740 if zero else 1461446703485210103287273052203988822378723970341, t0, t1))
        fund = d.net_stack.fund
        position = fund.functions.positionId().call()
        before = (d.wheel.functions.publishedValuation().call(), d.wheel.functions.reserve().call(),
                  d.buck.functions.totalIssued().call(), d.buck.functions.fundFlows().call(),
                  d.basket.functions.holdings(position).call())
        from alberta_buck.sim.basket_machine import publish_jubilee_valuation
        assert publish_jubilee_valuation(d)["status"] == 0
        after = (d.wheel.functions.publishedValuation().call(), d.wheel.functions.reserve().call(),
                 d.buck.functions.totalIssued().call(), d.buck.functions.fundFlows().call(),
                 d.basket.functions.holdings(position).call())
        assert after == before
        assert not d.chain.mismatches
    assert data["meta"]["run"]["completed"] and len(observations) == len(data["frames"]) == 3
    assert any(f["net"]["jubilee"]["position"]["positions"] for f in observations)
    assert summary["pdf"] == str(tmp_path / "canonical.pdf")
    raw = (tmp_path / "canonical.pdf").read_bytes()
    assert raw.startswith(b"%PDF-") and raw.rstrip().endswith(b"%%EOF")
    assert len(re.findall(rb"/Type /Page\b", raw)) >= 3
    assert b"BUCK accounting: net" in raw and b"Vector SHA256:" in raw
    assert plotted_returns == [pytest.approx([100 * household_position(f)[2] for f in observations])]
