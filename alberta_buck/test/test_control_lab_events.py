"""Receipt attribution and executed-fill presentation, independently of advice."""
from pathlib import Path

import pytest

from alberta_buck.sim.control_lab.event_view import execution_counts, execution_log, price_markers
from alberta_buck.sim.control_lab.model import DEFAULTS, load_config


def test_markers_use_volume_weighted_fills_and_pairing_is_not_a_buy():
    trades = [dict(token=0, side="buy", quote="BUCK", token_amount=2, quote_amount=120, price_quote=60),
              dict(token=0, side="buy", quote="BUCK", token_amount=1, quote_amount=90, price_quote=90)]
    events = [dict(actor="desk", action="supply", trades=[t]) for t in trades]
    events += [dict(actor="basket", action="pair", buy_token=1, spent_buck=50, trades=[])]
    frame = dict(day=0, token_buck=[100, 110, 90], buck_usdc=1.25, events=events)
    r = dict(config=DEFAULTS, frames=[frame])
    native = {g["id"]: g for g in price_markers(r, native=True)}
    usd = {g["id"]: g for g in price_markers(r)}
    assert set(native) == {"desk-buy", "basket-pair"}
    assert native["desk-buy"]["points"][0]["value"] == pytest.approx(.7)
    assert usd["desk-buy"]["points"][0]["value"] == pytest.approx(.875)
    assert native["desk-buy"]["points"][0]["count"] == 2
    assert native["basket-pair"]["points"][0]["value"] == pytest.approx(1.1)
    assert execution_counts([frame])["basket_buys"] == 0
    assert execution_counts([frame])["basket_pairs"] == 1


def test_arb_marker_uses_internal_leg_but_log_retains_both_venues():
    event = dict(actor="arb", action="cycle", trades=[
        dict(token=2, side="buy", quote="USDC", token_amount=2, quote_amount=170, price_quote=85),
        dict(token=2, side="sell", quote="BUCK", token_amount=2, quote_amount=180, price_quote=90)])
    r = dict(config=DEFAULTS, frames=[dict(day=0, token_buck=[100]*3, buck_usdc=1, events=[event])])
    markers = price_markers(r)
    assert [m["id"] for m in markers] == ["arb-sell"]
    assert len(execution_log(r)[0]["trades"]) == 2
    assert execution_counts(r["frames"])["arb_cycles"] == 1
    r["frames"][0].pop("events")
    assert not execution_counts(r["frames"])["recorded"]
    assert not price_markers(r)


def test_receipts_record_desk_basket_and_both_arb_legs():
    pytest.importorskip("pyrevm")
    from alberta_buck.sim.control_lab.block_pairs import simulate_observed as simulate
    c = load_config(Path(__file__).resolve().parents[2] / "doc/control-lab.toml")
    # Compress the oscillation and tighten these test-only bands to exercise
    # real swaps on both basket legs and a monetary operation in three days.
    c["days"] = 3
    c["feed"]["period_days"] = 2
    c["basket"].update(leash_bp=100, tilt=0., lean_cap_bp=0)       # a lean must stay inside the leash
    c["desk"].update(enter_bp=20, release_bp=10, persist_days=1)
    result = simulate(c, c["profiles"][0], "all")
    counts = execution_counts(result["frames"])
    assert counts["desk_ops"] == result["summary"]["desk_ops"] > 0
    assert counts["arb_cycles"] == result["summary"]["arb_cycles"] > 0
    assert counts["basket_buys"] > 0 and counts["basket_sells"] > 0
    cycles = desk = 0
    for frame in result["frames"]:
        for event in frame["events"]:
            cycles += event["actor"] == "arb"
            desk += event["actor"] == "desk"
            if event["actor"] == "arb":
                assert {t["side"] for t in event["trades"]} == {"buy", "sell"}
                assert {t["quote"] for t in event["trades"]} == {"BUCK", "USDC"}
                assert len({t["token"] for t in event["trades"]}) == 1
                assert event["profit_buck"] > 0
            for trade in event["trades"]:
                assert trade["token_amount"] == int(trade["token_raw"]) / 1e18
                assert trade["quote_amount"] == int(trade["quote_raw"]) / 1e6
                assert trade["token_amount"] * trade["price_quote"] == pytest.approx(trade["quote_amount"])
        assert frame["cumulative"]["arb_cycles"] == cycles
        assert frame["cumulative"]["desk_ops"] == desk
