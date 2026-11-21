"""The retired keepers' cast slots, and the star report's silent-desk flag.

DirectorKeeperAgent poked the pairs director and MonetaryKeeperAgent turned the ops desk's crank;
both retired with the pro-rata and ops baskets and the pairs director (2026-11-20). The machine's
wheel samples and works its own desk. The two agents stay in the cast as no-op slots: an agent's
number seeds its identity and its random stream, so removing one would renumber every agent after it.
"""

from types import SimpleNamespace

from alberta_buck.sim.director_agent import DirectorKeeperAgent, MonetaryKeeperAgent


def test_the_retired_keepers_do_nothing():
    sent = []
    chain = SimpleNamespace(send=lambda *a, **k: sent.append(a))
    d = SimpleNamespace(basket_impl="equity-ops", chain=chain, tokens=[], director=None, desk=None)
    ctr = {}
    for cls in (DirectorKeeperAgent, MonetaryKeeperAgent):
        cls(0).act(d, SimpleNamespace(agents={}), day=30, tick=0, ctr=ctr)
    assert sent == [] and ctr == {}


def test_the_star_report_flags_a_silent_desk(tmp_path):
    import json
    from alberta_buck.sim.star import desk_silent

    def vec(name, last):
        p = tmp_path / name
        p.write_text(json.dumps({"frames": [{"day": 0}, last]}))
        return p

    assert desk_silent(vec("silent.json", {"mk_no_advice": 731, "mk_ops": 0, "directorPokes": 0}))
    assert not desk_silent(vec("fed.json", {"mk_no_advice": 700, "mk_pokes": 731}))
    assert not desk_silent(vec("ops.json", {"mk_no_advice": 700, "directorPokes": 2924}))
    assert not desk_silent(vec("nodesk.json", {"day": 1}))
    # poked, but the signal never moved: the director could not read the basket
    dead = tmp_path / "dead.json"
    dead.write_text(json.dumps({"frames": [{"day": i, "mk_cm": 0} for i in range(40)]
                                + [{"mk_no_advice": 40, "mk_pokes": 40, "mk_cm": 0}]}))
    assert desk_silent(dead)
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"frames": [{"day": i, "mk_cm": i} for i in range(40)]
                                + [{"mk_no_advice": 40, "mk_pokes": 40, "mk_cm": 3}]}))
    assert not desk_silent(live)
