"""MonetaryKeeperAgent keeps the desk's signal fresh (alberta_buck/sim/director_agent.py).

The desk asks its director for advice, and the director advances only when
poked.  DirectorKeeperAgent pokes it on the pro-rata ops basket; it skips
equity baskets, and a run can cast it away to remove the director's
rebalancing.  Until 2026-09-30 nothing else poked the director, so the equity
desk read an unsampled director and never operated: NoAdvice on every day of
every equity run.  The keeper now pokes the director itself whenever
DirectorKeeperAgent will not -- and only then, so runs where that agent pokes
are unchanged.
"""

from types import SimpleNamespace

from alberta_buck.sim.director_agent import MonetaryKeeperAgent


class _Call:
    def __init__(self, name, ret):
        self.name, self.ret = name, ret

    def call(self, *_a, **_k):
        return self.ret


class _Functions:
    def __init__(self, name, rets):
        self._name, self._rets = name, rets

    def __getattr__(self, fn):
        return lambda *_a: _Call(f"{self._name}.{fn}", self._rets.get(fn, 0))


class _Contract:
    def __init__(self, name, rets=None):
        self.functions = _Functions(name, rets or {})


class _Chain:
    def __init__(self):
        self.sent = []

    def send(self, fn, sender=None, gas=None):
        self.sent.append(fn.name)


def _world(basket_impl, director_keepers, director=True):
    chain = _Chain()
    d = SimpleNamespace(
        basket_impl=basket_impl, chain=chain, tokens=[],
        director=_Contract("director") if director else None,
        desk=_Contract("desk", {"monetaryOperation": 1}) if basket_impl == "equity-ops" else None,
        basket=_Contract("desk", {"monetaryOperation": 1}))
    scenario = SimpleNamespace(agents={"MonetaryKeeperAgent": 1,
                                       "DirectorKeeperAgent": director_keepers})
    keeper = MonetaryKeeperAgent(0)
    keeper.account = SimpleNamespace(address="0xkeeper")
    return keeper, d, scenario, chain


def _act(basket_impl, director_keepers, tick=0, director=True):
    keeper, d, scenario, chain = _world(basket_impl, director_keepers, director)
    ctr = {}
    keeper.act(d, scenario, day=30, tick=tick, ctr=ctr)
    return chain.sent, ctr


def test_the_equity_desk_samples_its_own_signal_first():
    sent, ctr = _act("equity-ops", director_keepers=1)
    assert sent == ["director.pokeAll", "desk.monetaryOperation"], sent
    assert ctr.get("mkPokes") == 1 and ctr.get("mkOps") == 1


def test_a_run_without_the_director_keeper_still_feeds_the_desk():
    sent, ctr = _act("ops", director_keepers=0)
    assert sent[0] == "director.pokeAll", sent
    assert ctr.get("mkPokes") == 1


def test_where_the_director_keeper_pokes_nothing_changes():
    sent, ctr = _act("ops", director_keepers=1)
    assert "director.pokeAll" not in sent, sent
    assert "mkPokes" not in ctr


def test_off_tick_and_without_a_director_the_keeper_is_as_before():
    sent, _ = _act("equity-ops", director_keepers=0, tick=1)
    assert sent == []
    sent, _ = _act("equity-ops", director_keepers=0, director=False)
    assert sent == ["desk.monetaryOperation"], sent


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
