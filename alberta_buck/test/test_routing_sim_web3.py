"""Externally-driven (anvil + web3.py) routing-sim smoke test.

Spawns a real anvil, deploys the real BuckKControllerDirect / Buck /
BuckBasket / IdentityRegistry stack + Uniswap V3 + Universal Router,
registers every EOA agent with a REAL cryptographic IdentityRegistry
proof, runs a short routing scenario, and asserts the headline:

  * every EOA agent is IdentityRegistry.isVerified() (real NIZK);
  * the BUCK pools are genuinely used by optimized routing (cycle>0);
  * each TOKEN/USDC pool tracks its CSV reference (the whale snaps one
    token/day; the arbs propagate it) within a lenient smoke bound.

Skips cleanly if anvil or web3 is unavailable.  The full run + plot is
`make sim` / `python -m alberta_buck.sim`.
"""

import shutil

import pytest

anvil_missing = shutil.which("anvil") is None
web3_missing = False
try:
    import web3  # noqa: F401
except Exception:
    web3_missing = True


@pytest.mark.skipif(anvil_missing or web3_missing,
                    reason="anvil or web3 not available")
def test_routing_sim_web3():
    from alberta_buck.sim.anvil import Anvil
    from alberta_buck.sim.loop import run
    from alberta_buck.sim.scenario import Scenario

    sc = Scenario(
        name="routing",
        tokens=[("PAXG", "PAX Gold", 18),
                ("cbBTC", "Coinbase Wrapped BTC", 8),
                ("AOIL", "Alberta Oil", 18)],
        csv_files=["paxg.csv", "cbbtc.csv", "aoil.csv"],
        agents={"AnonymousArbAgent": 3, "MarketMakerWhale": 1},
        days=30,
        ticks_per_day=3,
    )
    with Anvil() as anvil:
        s = run(sc, anvil, verbose=True)

    assert s["all_eoa_verified"], "an EOA agent failed real registration"
    assert s["cycle_trades"] > 0, "BUCK pools never entered routing"
    assert s["direct_trades"] > 0, "market maker never acted"
    # Lenient: one token is snapped per day at a random tick; arbs propagate.
    for i, t in enumerate(s["tokens"]):
        assert s["track_err"][i] < 0.35, (
            f"{t} TOKEN/USDC tracking err {100*s['track_err'][i]:.1f}% too high")
