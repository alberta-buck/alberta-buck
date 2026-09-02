"""Unit tests for the markout ledger (WAVE3.org WP-1, L1)."""

from alberta_buck.sim.markout import (
    UB, MarkoutLedger, actor_tag, is_lp_side, swap_from_delta,
)

E6 = 10 ** 6


def test_swap_from_delta_rejects_liquidity_moves():
    assert swap_from_delta(1000, 500, 18, 0.003) is None       # both up
    assert swap_from_delta(-1000, -500, 18, 0.003) is None     # both down
    assert swap_from_delta(0, 500, 18, 0.003) is None
    assert swap_from_delta(1000, 0, 18, 0.003) is None


def test_swap_from_delta_prices_and_fees():
    # Taker sells 2 whole TOKEN (18-dec) into the pool for 4000 BUCK (6-dec):
    # the LP receives base (+2) and pays quote (-4000e6).
    q_lp, p_exec, fee, notional = swap_from_delta(2 * 10 ** 18, -4000 * E6,
                                                  18, 0.003)
    assert q_lp == 2.0
    assert abs(p_exec - 2000 * E6) < 1e-6          # 2000 BUCK per token
    assert abs(fee - 2.0 * 2000 * E6 * 0.003) < 1e-6   # fee on the base input
    assert notional == 4000 * E6
    # Taker buys 1 TOKEN with 2100 BUCK: fee on the quote input.
    q_lp, p_exec, fee, notional = swap_from_delta(-1 * 10 ** 18, 2100 * E6,
                                                  18, 0.003)
    assert q_lp == -1.0
    assert abs(fee - 2100 * E6 * 0.003) < 1e-6


def test_common_mode_markout_is_adverse_when_buck_reverts_to_par():
    # BUCK cheap (bvib 1.10): a taker buys BUCK with TOKEN at 2200 BUCK/tok
    # (10% above the par-implied 2000).  The LP received TOKEN.  BUCK then
    # reverts to par and the pool price falls to 2000: the LP sold cheap
    # BUCK -- adverse, and entirely common-mode.
    L = MarkoutLedger(horizons=(1,), n_pools=1)
    assert L.record(day=10, tick=0, cls="ExcursionArbAgent", pool=0,
                    d_base_raw=10 ** 18, d_quote=-2200 * E6, dec=18,
                    fee_frac=0.003, bvib=1.10)
    assert L.resolve(day=10, prices={0: 2200 * E6}, bvib=1.10) == 0  # not yet
    assert L.resolve(day=11, prices={0: 2000 * E6}, bvib=1.00) == 1
    f = L.frame()
    n, vol, fees, adv_total, adv_cm = f["pool"][0]
    assert n == 1 and vol == 2200 * E6
    assert adv_total == -200 * E6                 # 1 tok * (2000 - 2200)
    assert abs(adv_cm - (-200 * E6)) <= 1         # 2200 * (1/1.1 - 1)
    assert f["cls"]["ExcursionArbAgent"][3] == adv_total


def test_differential_move_is_not_common_mode():
    # No BUCK move (bvib 1.0 -> 1.0) but the constituent repriced 5% down:
    # total markout adverse, common-mode zero.
    L = MarkoutLedger(horizons=(5,), n_pools=1)
    L.record(day=0, tick=0, cls="AnonymousArbAgent", pool=0,
             d_base_raw=10 ** 18, d_quote=-2000 * E6, dec=18,
             fee_frac=0.003, bvib=1.0)
    L.resolve(day=5, prices={0: 1900 * E6}, bvib=1.0)
    n, vol, fees, adv_total, adv_cm = L.frame()["pool"][0]
    assert adv_total == -100 * E6
    assert adv_cm == 0


def test_manipulator_that_reverts_pays_the_lp():
    # A dump: the taker SELLS BUCK (LP receives BUCK? no -- basket pool base
    # is TOKEN: the taker buys TOKEN with BUCK).  LP gives 1 TOKEN, receives
    # 2100 BUCK, at a price above the pre-dump 2000; the pool is left at
    # bvib 1.05.  Full reversion to 2000 / par: the LP sold TOKEN dear.
    L = MarkoutLedger(horizons=(1,), n_pools=1)
    L.record(day=0, tick=0, cls="WhaleRaidAgent:p2", pool=0,
             d_base_raw=-10 ** 18, d_quote=2100 * E6, dec=18,
             fee_frac=0.003, bvib=1.05)
    L.resolve(day=1, prices={0: 2000 * E6}, bvib=1.0)
    n, vol, fees, adv_total, adv_cm = L.frame()["pool"][0]
    assert adv_total == 100 * E6                  # -1 * (2000 - 2100)
    assert adv_cm == 100 * E6                     # -2100 * (1/1.05 - 1)
    assert fees == round(2100 * E6 * 0.003)


def test_ub_pool_books_total_only_and_class_split():
    L = MarkoutLedger(horizons=(1,), n_pools=0)
    # Saver buys 1000 BUCK with 1010 USDC: LP (SimLP) gave BUCK (base -).
    L.record(day=0, tick=0, cls="SaverAgent", pool=UB,
             d_base_raw=-1000 * E6, d_quote=1010 * E6, dec=6,
             fee_frac=0.0005, bvib=1.0)
    L.resolve(day=1, prices={UB: 1.02 * E6}, bvib=1.0)
    f = L.frame()
    n, vol, fees, adv = f["ub"]
    assert n == 1 and vol == 1010 * E6
    # LP sold 1000 BUCK at 1.01, now worth 1.02: adverse 10 USDC.
    assert adv == -10 * E6
    row = f["cls"]["SaverAgent"]
    assert row[0] == 1 and row[1] == 0 and row[2] == 0     # no basket volume
    assert row[-2] == 1010 * E6 and row[-1] == -10 * E6     # vol_ub, advUb_h1


def test_lp_side_classes_are_skipped():
    L = MarkoutLedger(n_pools=1)
    assert not L.record(0, 0, "DirectMintAgent", 0, 10 ** 18, -2000 * E6,
                        18, 0.003, 1.0)
    assert L.frame()["pending"] == 0
    assert is_lp_side("ArrivingDMAgent") and not is_lp_side("SaverAgent")


def test_actor_tag_whale_phase():
    class W:
        phase = 2
    W.__name__ = "WhaleRaidAgent"
    assert actor_tag(W()) == "WhaleRaidAgent:p2"

    class S:
        pass
    S.__name__ = "SaverAgent"
    assert actor_tag(S()) == "SaverAgent"
