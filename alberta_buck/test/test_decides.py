"""Agent._decides: the randomized decision that keeps a population on one
threshold from acting in lockstep (alberta_buck/sim/agents.py)."""

import math
from types import SimpleNamespace

from alberta_buck.sim.agents import Agent


class _A(Agent):
    DECIDE_W = 0.01


class _Hard(Agent):
    DECIDE_W = 0.0


def _scn(seed=7):
    return SimpleNamespace(seed=seed, experiment=None)


def test_hard_threshold_when_the_width_is_zero():
    a = _Hard(0)
    assert a._decides(_scn(), 1e-9) is True
    assert a._decides(_scn(), 0.0) is False
    assert a._decides(_scn(), -0.5) is False
    assert getattr(a, "_decide_rng", None) is None, "a hard gate draws nothing"


def test_never_acts_or_draws_below_the_threshold():
    a = _A(0)
    for x in (0.0, -1e-6, -0.2):
        assert a._decides(_scn(), x) is False
    assert getattr(a, "_decide_rng", None) is None, "no draw below the threshold"


def test_deterministic_per_agent_and_independent_across_agents():
    a, b = _A(3), _A(3)
    seq_a = [a._decides(_scn(), 0.01) for _ in range(200)]
    seq_b = [b._decides(_scn(), 0.01) for _ in range(200)]
    assert seq_a == seq_b, "same seed, class and index: the same decisions"
    c = _A(4)
    seq_c = [c._decides(_scn(), 0.01) for _ in range(200)]
    assert seq_a != seq_c, "another agent draws its own"


def test_the_rate_follows_one_minus_exp():
    n = 4000
    for x in (0.005, 0.01, 0.03):
        a = _A(11)
        k = sum(a._decides(_scn(), x) for _ in range(n))
        p = 1.0 - math.exp(-x / 0.01)
        sd = math.sqrt(p * (1 - p) / n)
        assert abs(k / n - p) < 4 * sd + 1e-3, (x, k / n, p)


def test_the_knob_overrides_the_class_default():
    exp = SimpleNamespace(knob=lambda cls, name, default: 0.0 if name == "decide_w" else default)
    scn = SimpleNamespace(seed=7, experiment=exp)
    a = _A(0)
    assert all(a._decides(scn, 1e-6) for _ in range(50)), "decide_w=0: the hard gate"
