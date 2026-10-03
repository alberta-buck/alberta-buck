"""Opt-in replacement after the comparison's common pool/bootstrap phase.

SIM_EQUITY_MACHINE=1 selects the new response, strict venue, shared signals
and outer-band policy. The old director/facet remain available with =0.
No deployed chain is migrated by this simulation helper.
"""
from __future__ import annotations

import os
import json
from pathlib import Path

from alberta_buck.sim.chain import load_artifact


def book_receipt(d, receipt, ctr) -> None:
    """Wheel-driven desk actions use the same counters as keeper actions."""
    if not getattr(d, "equity_machine", False):
        return
    from web3.logs import DISCARD
    if d.desk is not None:
        for event in d.desk.events.MonetaryOperation().process_receipt(receipt, errors=DISCARD):
            quadrant = int(event["args"]["quadrant"])
            ctr["mkOps"] = ctr.get("mkOps", 0) + 1
            key = f"mkQ{quadrant}"
            ctr[key] = ctr.get(key, 0) + 1
            key = f"dk_q{quadrant}"
            ctr[key] = ctr.get(key, 0) + int(event["args"]["buckMoved"])
    for event in d.basket.events.Responded().process_receipt(receipt, errors=DISCARD):
        key = f"wh_eq_response_{int(event['args']['action'])}_n"
        ctr[key] = ctr.get(key, 0) + 1


def activate(d) -> None:
    if not d.equity_machine:
        return
    chain, gov, basket, wheel = d.chain, d.gov, d.basket, d.wheel
    pool_abi, _ = load_artifact("UniswapV3Pool")
    for pool in d.pool_buck:
        pc = d.w3.eth.contract(address=pool, abi=pool_abi)
        if pc.functions.liquidity().call() == 0:
            raise ValueError("basket machine requires seeded TOKEN/BUCK pools before activation")
    # The shared bootstrap uses initialized prices to create the first LPs.
    # User trading starts only after the full observation window exists.
    window = int(basket.functions.twapWindow().call())
    d.anvil.warp_to(d.w3.eth.get_block("latest")["timestamp"] + window + 1)
    venue = chain.deploy("ValidatedBasketVenue")
    response = chain.deploy("BuckBasketEquityResponse")
    interval = int(os.environ.get("SIM_MACHINE_INTERVAL", "1800"))
    signals = chain.deploy("BasketSignals", basket.address, interval,
                           int(os.environ.get("SIM_MACHINE_MAX_AGE", "3600")), 500)
    # Historical observations must predate the experiment; never use the
    # simulated future CSV as a warm-up or invent an anchor from its day 0.
    history_path = os.environ.get("SIM_MACHINE_HISTORY")
    if history_path:
        from alberta_buck.sim.signal_history import initialize_history
        history = json.loads(Path(history_path).read_text())
        initialize_history(chain, signals, basket, gov, history["rows"], history["mean_prices"])
    # Preserve an explicitly configured integration horizon. The otherwise
    # unlimited default must not integrate a recovered observation over an
    # arbitrarily long outage.
    dt_max = os.environ.get("SIM_MACHINE_DT_MAX")
    if dt_max is not None or int(d.kctrl.functions.dTMax().call()) == 2**256 - 1:
        horizon = int(dt_max) if dt_max is not None else max(2 * interval, int(d.kctrl.functions.dT().call()))
        chain.send(d.kctrl.functions.setDTMax(horizon), sender=gov)
    chain.send(basket.functions.setVenue(venue.address), sender=gov)
    chain.send(basket.functions.setEquityWheel(response.address), sender=gov)
    chain.send(basket.functions.setResponse(signals.address, (
        int(os.environ.get("SIM_MACHINE_LEASH_BP", "3000")), interval,
        int(float(os.environ.get("SIM_MACHINE_TILT", "1")) * 10**18))), sender=gov)
    chain.send(wheel.functions.setResponseMode(True), sender=gov)
    chain.send(wheel.functions.setController(d.kctrl.address), sender=gov)
    # These were comparison advisors; the replacement has one price service.
    zero = "0x" + "0" * 40
    chain.send(wheel.functions.setDirector(zero), sender=gov)
    d.equity_director = None
    d.venue, d.signals = venue, signals
    source = signals
    if d.desk is not None:
        # Configuration of the replacement is independent of which legacy
        # advisory director was selected for the comparison bootstrap.
        desk = d.desk
        chain.send(desk.functions.mirrorConstituents(), sender=gov)
        capital = int(os.environ.get("SIM_OPS_CAPITAL_USD", "400000"))
        if d.desk_grant is None:
            d.desk_grant = []
            for i, token in enumerate(d.tokens):
                initial_price = int(basket.functions.constituents(i).call()[3])
                amount = capital * 10**6 * 10**d.dec[i] // initial_price
                if amount:
                    chain.send(token.functions.mint(gov, amount))
                    chain.send(token.functions.approve(desk.address, amount), sender=gov)
                    chain.send(desk.functions.capitalizeMonetary(i, amount), sender=gov)
                d.desk_grant.append(amount)
        chain.send(desk.functions.setOpsParams((
            int(os.environ.get("SIM_OPS_LEG_BP", "40")),
            int(os.environ.get("SIM_OPS_POSITION_BP", "1000")),
            int(os.environ.get("SIM_OPS_OUTRIGHT_BP", "1000")), True)), sender=gov)
        initial_capital = capital * len(d.tokens)
        chain.send(desk.functions.setRiskBudget(
            int(os.environ.get("SIM_OPS_TURNOVER_BUCK", str(initial_capital))) * 10**6,
            int(os.environ.get("SIM_OPS_CAPITAL_FLOOR_BUCK", str(initial_capital // 4))) * 10**6,
            500), sender=gov)
        policy = chain.deploy("OuterBandMonetaryDirector", signals.address,
            int(os.environ.get("SIM_OPS_ENTER_BP", "300")),
            int(os.environ.get("SIM_OPS_RELEASE_BP", "200")),
            int(os.environ.get("SIM_OPS_GAIN_BP", "2500")),
            int(os.environ.get("SIM_OPS_CAP_BP", "40")),
            int(os.environ.get("SIM_OPS_PERSIST", "30")))
        chain.send(d.desk.functions.setVenue(venue.address), sender=gov)
        chain.send(d.desk.functions.setMonetaryDirector(policy.address), sender=gov)
        chain.send(d.desk.functions.setExecutionGuard(policy.address), sender=gov)
        chain.send(wheel.functions.setOps(d.desk.address, policy.address), sender=gov)
        d.monetary_policy = source = policy
    chain.send(wheel.functions.setSample(source.address), sender=gov)
    chain.send(source.functions.observe())
    if not signals.functions.fresh().call():
        raise ValueError("basket machine could not publish a valid initial observation")
    # Include the new event ABI; the shell still wins duplicate selectors.
    abi, _ = load_artifact("BuckBasketEquityResponse")
    union = list(basket.abi)
    keys = {(e.get("type"), e.get("name"), tuple(i.get("type") for i in e.get("inputs", []))) for e in union}
    for entry in abi:
        key = (entry.get("type"), entry.get("name"), tuple(i.get("type") for i in entry.get("inputs", [])))
        if key not in keys:
            union.append(entry)
            keys.add(key)
    d.basket = d.w3.eth.contract(address=basket.address, abi=union)
    chain.clear_balance_cache()
