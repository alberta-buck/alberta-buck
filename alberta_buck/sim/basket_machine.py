"""The basket machine, put in place of the older upkeep after the common pool/bootstrap phase.

An experiment selects it with `[scenario] basket = "equity-machine"` (loop.run), and sizes it in its
`[machine]` table; SIM_EQUITY_MACHINE=1 beside basket "equity-ops" selects it for a scenario that carries
no experiment. It is the upkeep facet with its pair observer and policy, the strict venue, the parity
reference and the outer-band desk policy. No deployed chain is migrated by this simulation helper.
"""
from __future__ import annotations

import math
import os

from alberta_buck.sim.chain import load_artifact

LOG_TICK = math.log(1.0001)
# The pair observer's timing (seconds) and rate floor, as the control lab runs them (doc/control-lab.toml).
OBSERVER = (43200, 86400, 86400, 172800, round(.0002 / LOG_TICK / 86400 * 1e18))
# The machine's own settings, at the control lab's values. An experiment's [machine] table overrides
# them world by world; the environment fills in for a scenario that carries no experiment.
DEFAULTS = {
    "interval": 1800,               # seconds between responses, and between reference samples
    "max_age": 3600,                # seconds a reference sample stays fresh
    "dt_max": None,                 # K's integration horizon, seconds; None: the world's step, or twice the interval
    "leash_bp": 3000,               # a constituent this far from its declared weight is safeguarded
    "tilt": .5,                     # how far a target leans against price: 0 not at all, 1 the most
    "lean_cap_bp": 1500,            # the most a target may lean, relative to its declared weight
    "half_life_days": 1.,           # the most a gap is released: half of what remains in this long
    "release_rate": 4.,             # a braking release, a day, on the rate's own scale
    "slack": .0025,                 # a gap this small, as a share of the book, builds no urgency
    "patience": .02,                # share of the book times days: the urgency that releases in full
    "upkeep": "BasketUpkeepFacet",  # the facet; a trial names its own (test/harness)
    "parity_gain": 5.,              # BUCKs the book aims to hold or to have issued per unit of parity error, of equity
    # The desk beside the basket and its outer-band policy.
    "desk_capital_usd": 400000,     # its founding grant, for each constituent
    "desk_leg_bp": 40,              # one operation's leg, of the pool
    "desk_position_bp": 1000,       # the most it holds open in one constituent
    "desk_outright_bp": 1000,       # the most of that held outright
    "desk_turnover_buck": None,     # a day's turnover budget; None: the founding grant
    "desk_floor_buck": None,        # the capital it stops at; None: a quarter of the founding grant
    "desk_enter_bp": 300,           # the parity error that starts it
    "desk_release_bp": 200,         # and the one that stops it
    "desk_gain_bp": 2500,
    "desk_cap_bp": 40,
    "desk_persist_days": 30,        # days beyond the band before it acts
}
ENVIRONMENT = {"interval": "SIM_MACHINE_INTERVAL", "max_age": "SIM_MACHINE_MAX_AGE", "dt_max": "SIM_MACHINE_DT_MAX",
               "leash_bp": "SIM_MACHINE_LEASH_BP", "tilt": "SIM_MACHINE_TILT", "lean_cap_bp": "SIM_MACHINE_LEAN_CAP_BP",
               "desk_capital_usd": "SIM_OPS_CAPITAL_USD", "desk_leg_bp": "SIM_OPS_LEG_BP",
               "desk_position_bp": "SIM_OPS_POSITION_BP", "desk_outright_bp": "SIM_OPS_OUTRIGHT_BP",
               "desk_turnover_buck": "SIM_OPS_TURNOVER_BUCK", "desk_floor_buck": "SIM_OPS_CAPITAL_FLOOR_BUCK",
               "desk_enter_bp": "SIM_OPS_ENTER_BP", "desk_release_bp": "SIM_OPS_RELEASE_BP",
               "desk_gain_bp": "SIM_OPS_GAIN_BP", "desk_cap_bp": "SIM_OPS_CAP_BP", "desk_persist_days": "SIM_OPS_PERSIST"}
# Responded's action: what a response did, and which of its two amounts measures it. A sale's is the
# TOKEN sold and a purchase's the TOKEN bought, in BUCK; a placement's is the BUCKs paired.
ACTIONS = {1: ("sell", "proceeds"), 2: ("buy", "buySpent"), 4: ("place", "buySpent")}


def settings(given=None) -> dict:
    """The machine's settings: the experiment's, else the environment's, else the control lab's."""
    out = dict(DEFAULTS)
    for key, name in ENVIRONMENT.items():
        if os.environ.get(name) is not None:
            out[key] = float(os.environ[name])
    unknown = set(given or {}) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown [machine] settings: {sorted(unknown)}")
    out.update(given or {})
    return out


def sizing(s) -> tuple:
    """PairRebalancePolicy's Sizing: a braking release on the rate's own scale, the whole capped at
    half the remaining gap each half life, and the urgency's slack and patience."""
    rate = round(math.log(2) / s["half_life_days"] * 10**18)
    return (0, round(s["release_rate"] * 10**18), 0, rate, rate, 10**18,
            round(s["slack"] * 10**18), round(s["patience"] * 10**18))


def targets_bp(d) -> list | None:
    """The weights the upkeep facet steers to (BasketUpkeepFacet._targets), in bp of the book: each
    declared weight leaned against its TOKEN's price over its reference price, shared over the book
    and held within the lean cap. Worked here from the shell's public state; None while a price is
    unavailable."""
    basket, n = d.basket, len(d.tokens)
    _, _, tilt, cap = basket.functions.response().call()
    held = [basket.functions.constituents(i).call() for i in range(n)]
    declared = [int(c[9]) for c in held]
    if not tilt:
        return declared
    leaned = []
    for i in range(n):
        try:
            price = int(basket.functions.marks(i, 0).call()[4])
        except Exception:
            return None
        anchor = int(basket.functions.referencePrice(i).call()) or int(held[i][3])
        leaned.append(declared[i] * (anchor / price) ** (tilt / 10000) if price and anchor else declared[i])
    total = sum(leaned)
    return [round(min(w * (1 + cap / 10000), max(w * (1 - cap / 10000), sum(declared) * x / total)))
            for w, x in zip(declared, leaned)]


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
        action = int(event["args"]["action"])
        key = f"wh_eq_response_{action}_n"
        ctr[key] = ctr.get(key, 0) + 1
        if action in ACTIONS:
            name, amount = ACTIONS[action]
            ctr[f"wh_eq_{name}"] = ctr.get(f"wh_eq_{name}", 0) + int(event["args"][amount])
            ctr[f"wh_eq_{name}_n"] = ctr.get(f"wh_eq_{name}_n", 0) + 1


def activate(d, given=None, turn_seconds=0) -> None:
    """Put the machine in place. `turn_seconds` is the world's own step: the time between two
    turns of its wheel, which K must be able to integrate over."""
    if not d.equity_machine:
        return
    s = settings(given)
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
    venue = chain.deploy("BasketVenueFacet")
    interval = int(s["interval"])
    signals = chain.deploy("BasketReference", basket.address, interval, int(s["max_age"]), 500)
    # Both quote routes inform the pair observer. The policy's reference is each
    # constituent's declared price, never a live one.
    observer = chain.deploy("PairSignals", [t.address for t in d.tokens], [d.buck.address, d.usdc.address],
                            [*d.pool_buck, *d.pool_usdc], *OBSERVER)
    reference = [round(math.log(int(basket.functions.constituents(i).call()[3]) / 10**6) / LOG_TICK * 10**18)
                 for i in range(len(d.tokens))]
    policy = chain.deploy("PairRebalancePolicy", observer.address, basket.address, reference, sizing(s))
    # A facet's constructor names what it needs; a trial may need more than the policy.
    wanted = next((e["inputs"] for e in load_artifact(s["upkeep"])[0] if e.get("type") == "constructor"), [])
    given_to = {"policy": policy.address, "parity_": signals.address, "parityGain_": round(s["parity_gain"] * 10**18)}
    response = chain.deploy(s["upkeep"], *(given_to[i["name"]] for i in wanted))
    # Preserve an explicitly configured integration horizon. The otherwise
    # unlimited default must not integrate a recovered observation over an
    # arbitrarily long outage; but it must cover the time between two turns,
    # or K integrates only a part of every step.
    dt_max = s["dt_max"]
    if dt_max is not None or int(d.kctrl.functions.dTMax().call()) == 2**256 - 1:
        horizon = (int(dt_max) if dt_max is not None
                   else max(2 * interval, int(d.kctrl.functions.dT().call()), int(turn_seconds)))
        chain.send(d.kctrl.functions.setDTMax(horizon), sender=gov)
    chain.send(basket.functions.setVenue(venue.address), sender=gov)
    chain.send(basket.functions.setEquityWheel(response.address), sender=gov)
    # The upkeep facet is written for a credit marked net: K bounds the BUCKs let out, not those held.
    chain.send(basket.functions.setNetMark(True), sender=gov)
    chain.send(basket.functions.setResponse((int(s["leash_bp"]), interval, round(s["tilt"] * 10000),
                                             int(s["lean_cap_bp"]))), sender=gov)
    chain.send(wheel.functions.setResponseMode(True), sender=gov)
    chain.send(wheel.functions.setController(d.kctrl.address), sender=gov)
    # These were comparison advisors; the replacement has one price service.
    zero = "0x" + "0" * 40
    chain.send(wheel.functions.setDirector(zero), sender=gov)
    d.equity_director = None
    d.venue, d.signals, d.pair_signals, d.pair_policy = venue, signals, observer, policy
    source = signals
    if d.desk is not None:
        # Configuration of the replacement is independent of which legacy
        # advisory director was selected for the comparison bootstrap.
        desk = d.desk
        chain.send(desk.functions.mirrorConstituents(), sender=gov)
        capital = int(s["desk_capital_usd"])
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
        chain.send(desk.functions.setOpsParams((int(s["desk_leg_bp"]), int(s["desk_position_bp"]),
                                                int(s["desk_outright_bp"]), True)), sender=gov)
        initial_capital = capital * len(d.tokens)
        turnover = initial_capital if s["desk_turnover_buck"] is None else int(s["desk_turnover_buck"])
        floor = initial_capital // 4 if s["desk_floor_buck"] is None else int(s["desk_floor_buck"])
        chain.send(desk.functions.setRiskBudget(turnover * 10**6, floor * 10**6, 500), sender=gov)
        desk_policy = chain.deploy("OuterBandDeskPolicy", signals.address, int(s["desk_enter_bp"]),
                                   int(s["desk_release_bp"]), int(s["desk_gain_bp"]), int(s["desk_cap_bp"]),
                                   int(s["desk_persist_days"]))
        chain.send(d.desk.functions.setVenue(venue.address), sender=gov)
        chain.send(d.desk.functions.setMonetaryDirector(desk_policy.address), sender=gov)
        chain.send(d.desk.functions.setExecutionGuard(desk_policy.address), sender=gov)
        chain.send(wheel.functions.setOps(d.desk.address, desk_policy.address), sender=gov)
        d.monetary_policy = source = desk_policy
    chain.send(wheel.functions.setSample(source.address), sender=gov)
    chain.send(source.functions.observe())
    if not signals.functions.fresh().call():
        raise ValueError("basket machine could not publish a valid initial observation")
    # Include the new event ABI; the shell still wins duplicate selectors.
    abi, _ = load_artifact("BasketUpkeepFacet")
    union = list(basket.abi)
    keys = {(e.get("type"), e.get("name"), tuple(i.get("type") for i in e.get("inputs", []))) for e in union}
    for entry in abi:
        key = (entry.get("type"), entry.get("name"), tuple(i.get("type") for i in entry.get("inputs", [])))
        if key not in keys:
            union.append(entry)
            keys.add(key)
    d.basket = d.w3.eth.contract(address=basket.address, abi=union)
    chain.clear_balance_cache()
