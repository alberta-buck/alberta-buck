"""Who holds the BUCKs, and who has drawn them, on chosen days of a world.

A world's frames give the supply but not where it sits. This runs a world to the last of the days
asked and on each of them reads every account's signed balance from Buck: the basket and its
pools, the BUCK/dollar pool, the helper that seeds pools, the insurance pool, the desk, and every
agent's wallet, proxy and escrow. Held BUCKs sum to the supply (Buck's invariant I0), so the table
accounts for all of it. Amounts are millions of BUCKs.
    python doc/review/who-holds.py alberta_buck/sim/experiments/rebalancing-eq-5yr-portcast.toml equity-ops 300,500,700 \\
        'agents.ExcursionCreditArbAgent.face_m=[20,60]'
"""
import collections, sys
from alberta_buck.sim import experiment as expmod
from alberta_buck.sim.loop import run
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil

toml, basket                    = sys.argv[1], sys.argv[2]
days                            = sorted(int(x) for x in sys.argv[3].split(","))
exp                             = expmod.load(toml, sets=sys.argv[4:] + [f"scenario.basket={basket}"])
scenario                        = expmod.build(exp)
table                           = {}                    # day -> {who: (held, drawn)}


class Done(Exception):
    pass


def accounts(d, agents):
    """(group, address) for every account that can hold BUCKs."""
    yield "basket", d.basket.address
    for p in d.pool_buck:
        yield "basket's pools", p
    if d.pool_ub:
        yield "BUCK/dollar pool", d.pool_ub
    for name, c in (("pool-seeding helper", d.simlp), ("desk", d.desk), ("wheel", d.wheel), ("router", d.router)):
        if c is not None:
            yield name, c.address
    transit                     = getattr(d, "_transit", None)
    if transit is not None:
        yield "depositor transit", transit.address
    for name, a in (("insurance pool", d.pool_acct), ("governor", d.gov), ("issuer", d.issuer_addr)):
        if a:
            yield name, a
    for ag in agents:
        cls                     = type(ag).__name__
        if getattr(ag, "account", None) is not None:
            yield cls, ag.account.address
        for field in ("proxy", "escrow"):
            c                   = getattr(ag, field, None)
            if c is not None:
                yield cls, c.address


def dump(day, d, agents, ctr):
    if day not in days:
        return
    held, drawn, seen           = collections.Counter(), collections.Counter(), set()
    for who, addr in accounts(d, agents):
        if addr in seen:
            continue
        seen.add(addr)
        s                       = d.buck.functions.signedBalanceOf(addr).call()
        (held if s > 0 else drawn)[who] += s
    supply                      = d.buck.functions.totalSupply().call()
    table[day]                  = (supply, held, drawn, d.basket.functions.basketValueInBuck().call() / 1e18,
                                   d.kctrl.functions.buckK().call() / 1e18,
                                   (ctr.get("dmExits", 0), ctr.get("dmBuckSold", 0), ctr.get("dm_sell_err", "")))
    report()
    if day == days[-1]:
        raise Done


def report():
    done                        = sorted(table)
    whos                        = sorted({w for _, h, dr, *_ in table.values() for w in list(h) + list(dr)})
    print(f"\n{'':28s}" + "".join(f"{'day ' + str(d):>22s}" for d in done))
    print(f"{'':28s}" + "".join(f"{'held':>11s}{'drawn':>11s}" for _ in done))
    for w in whos:
        print(f"{w:28s}" + "".join(f"{table[d][1][w] / 1e12:11.2f}{table[d][2][w] / 1e12:11.2f}" for d in done))
    print(f"{'all accounts read':28s}" + "".join(f"{sum(table[d][1].values()) / 1e12:11.2f}{sum(table[d][2].values()) / 1e12:11.2f}" for d in done))
    print(f"{'supply':28s}" + "".join(f"{table[d][0] / 1e12:11.2f}{'':11s}" for d in done))
    print(f"{'basket cost in BUCK, K':28s}" + "".join(f"{table[d][3]:11.3f}{table[d][4]:11.3f}" for d in done))
    print(f"{'exits; BUCKs they sold':28s}" + "".join(f"{table[d][5][0]:11d}{table[d][5][1] / 1e12:11.2f}" for d in done))
    for d in done:
        if table[d][5][2]:
            print(f"  day {d}: a depositor's sale failed: {table[d][5][2]}")
    sys.stdout.flush()


try:
    with PyrevmAnvil(port=0) as anvil:
        run(scenario, anvil, out_path=None, verbose=False, basket_impl=basket, on_day_start=dump)
except Done:
    pass
