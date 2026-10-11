"""The sizing campaign's inputs, and the arithmetic that revalues the recorded book."""
import copy
import math
from pathlib import Path

import pytest

from alberta_buck.sim.control_lab.basket_sizing import AVAILABLE, FLAT, LAB, SIZINGS, TWO_STEP, half_life, lean, patient, score
from alberta_buck.sim.control_lab.book import holdings, recorded, reference_value
from alberta_buck.sim.control_lab.model import DEFAULTS, dependencies, feed_rows, load_config, pair_sizing, response_params, validate


def frame(day=0., idle=(10., 20., 30.), liquidity=(4e15, 0, 9e15), price=(100., 100., 100.),
          feed=(100., 100., 100.), account=-500., relief=5., reference=1., buck_usdc=1., equity=0., events=()):
    return dict(day=day, token_buck=list(price), target_usd=list(feed), buck_usdc=buck_usdc,
                reference=dict(target_usd=reference), events=list(events), cumulative=dict(arb_profit_buck=0.),
                basket=dict(idle=list(idle), liquidity=[str(int(x)) for x in liquidity], account=account,
                            relief=relief, equity=equity, weights=[3333, 3333, 3334]))


def test_full_range_position_splits_evenly_at_its_price():
    f = frame()
    tokens, buck = holdings(f)
    # L = 4e15 at 100 BUCK: 400 TOKEN beside 40,000 BUCK, equal in value.
    assert tokens == pytest.approx([10 + 400, 20, 30 + 900])
    assert buck == pytest.approx(40_000 + 90_000 - 500 + 5)
    assert reference_value(f) == pytest.approx(100 * (410 + 20 + 930) + 129_505)


def test_position_follows_the_pool_price_and_the_reference_prices_buck():
    f = frame(price=(400., 100., 100.), feed=(410., 100., 100.), reference=1.25, buck_usdc=1.6)
    tokens, buck = holdings(f)
    assert tokens[0] == pytest.approx(10 + 4e15 / (20 * 1e12))        # fewer TOKEN at the higher pool price
    assert buck == pytest.approx(4e15 * 20 / 1e12 + 90_000 - 495)
    # TOKEN at the external feed, net BUCK at the reference basket's cost: the BUCK/USDC spot is not used.
    assert reference_value(f) == pytest.approx(tokens[0] * 410 + 2000 + 93_000 + buck * 1.25)
    assert reference_value(f) == reference_value({**f, "buck_usdc": 9.})


def test_an_untouched_book_is_revalued_at_later_prices():
    opening = frame()
    later = frame(day=30., idle=(0., 0., 0.), liquidity=(0, 0, 0), feed=(110., 95., 100.), reference=1.02, account=0., relief=0.)
    assert reference_value(later) == 0
    tokens, buck = holdings(opening)
    assert reference_value(later, holdings(opening)) == pytest.approx(tokens[0] * 110 + tokens[1] * 95 + tokens[2] * 100 + buck * 1.02)


def test_frames_saved_before_the_book_was_recorded_are_recognized():
    f = frame()
    assert recorded(f)
    del f["basket"]["idle"]
    assert not recorded(f)


def test_sizings_resolve_to_valid_policy_arguments():
    assert "pair" not in SIZINGS["maintenance"] and SIZINGS["first"]["pair"] == {}
    assert pair_sizing(DEFAULTS["pair"]) == (2 * 10**15, 10**17, 20, 0, 10**18, 10**18, 0, 0)
    assert SIZINGS["lab"] == dict(title=SIZINGS["lab"]["title"], pair=LAB, basket=lean(.5))
    assert SIZINGS["tilt-0"]["pair"] == LAB and SIZINGS["tilt-0"]["basket"] == dict(tilt=0, lean_cap_bp=0)
    for name, arm in SIZINGS.items():
        if "pair" not in arm:
            continue
        cfg = copy.deepcopy(DEFAULTS)
        cfg["pair"].update(arm["pair"])
        cfg["basket"].update(arm.get("basket", {}))
        validate(cfg)
        speed, rate, gain, closure, ceiling, impact, slack, patience = pair_sizing(cfg["pair"])
        assert all(isinstance(x, int) and 0 <= x < 2**64 for x in (speed, rate, gain, closure, ceiling, impact, slack, patience))
        assert closure <= ceiling
        assert (patience > 0) == (not name.startswith("band-") and name != "first")
        assert response_params(cfg["basket"], 1800)[2:] in ((0, 0), (2500, 1500), (5000, 1500), (10000, 1500))
    # Half the remaining gap a day is a rate of ln 2, whatever the braking release may ask for.
    assert half_life(1) == dict(release_speed=0., release_rate=4., closure_gain=0, closure_rate=.693147, max_rate=.693147)
    assert half_life(.5)["max_rate"] == pytest.approx(2 * math.log(2), abs=1e-6)
    # A quarter point of slack and two point-days of patience, as shares of the book.
    assert pair_sizing({**DEFAULTS["pair"], **patient(2)})[6:] == (25 * 10**14, 2 * 10**16)
    # The configuration's own rule is the campaign's `lab` arm.
    cfg = load_config(Path(__file__).resolve().parents[2] / "doc/control-lab.toml")
    assert {k: cfg["pair"][k] for k in LAB} == LAB
    assert {k: cfg["basket"][k] for k in ("tilt", "lean_cap_bp")} == lean(.5)


@pytest.mark.parametrize("change", [dict(max_rate=0.), dict(impact_share=1.5), dict(release_rate=-1.),
                                    dict(closure_gain=1.5), dict(closure_rate=math.inf),
                                    dict(slack=1.), dict(patience=-.01), dict(patience=19.)])
def test_invalid_sizings_are_rejected(change):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["pair"].update(change)
    with pytest.raises(ValueError, match="pair sizing"):
        validate(cfg)


def test_sizing_invalidates_only_stages_that_run_the_basket():
    cfg = copy.deepcopy(DEFAULTS)
    other = copy.deepcopy(cfg)
    other["pair"].update(half_life(1))
    profile = AVAILABLE["three-phase"]
    for stage in ("feeds", "monitor", "monitor-k", "linked"):
        assert dependencies(cfg, profile, stage) == dependencies(other, profile, stage)
    for stage in ("basket", "k", "desk", "demand", "supply", "both", "all"):
        assert dependencies(cfg, profile, stage) != dependencies(other, profile, stage)


def test_two_step_reprices_one_token_and_never_returns():
    cfg = copy.deepcopy(DEFAULTS)
    assert "two-step" in FLAT
    cfg["feed"]["amplitude"] = 0
    rows = feed_rows(cfg, TWO_STEP)
    a = [r["target_usd"][0] for r in rows]
    assert a[0] == pytest.approx(100, abs=1) and a[45 * 4] == pytest.approx(115, abs=1.5)
    assert a[55 * 4] == pytest.approx(115, abs=1.5) and a[-1] == pytest.approx(130, abs=1.5)
    assert all(abs(r["target_usd"][i] - 100) < 1.5 for r in rows for i in (1, 2))


def test_score_counts_transfers_and_differences_cycles():
    def transfer(sold, bought):
        return dict(actor="basket", action="transfer", trades=[
            dict(side="sell", quote_amount=sold), dict(side="buy", quote_amount=bought)])
    cfg = copy.deepcopy(DEFAULTS)
    cfg["feed"]["period_days"] = 1.
    frames = [frame(day=k / 4, account=-500. + 10 * k, equity=1000. + k, buck_usdc=1.1,
                    events=[transfer(40., 20.)] if k in (2, 5) else [dict(actor="arb", action="cycle", trades=[])])
              for k in range(9)]
    s = score(dict(frames=frames, config=cfg, summary=dict(mean_abs_parity_error=.001, end_k=.75)))
    assert (s["transfers"], s["sold"], s["bought"]) == (2, 80., 40.)
    assert s["usd"] == pytest.approx(1008 * 1.1) and s["buck"] == 1008
    assert len(s["cycles"]) == 3                                         # day 0, 1 and 2 at four steps a day
    assert s["cycles"][1] - s["cycles"][0] == pytest.approx(40) and s["reference"] - s["opened"] == pytest.approx(80)
    assert s["untouched"] == pytest.approx(s["opened"])                  # prices never moved
    assert s["error_pp"] == 0
