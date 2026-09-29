"""The equity BuckBasket's prototype (alberta_buck/sim/equity_basket.py): one
test per property the design promises (doc/BASKET-EQUITY.org)."""
import pytest

from alberta_buck.sim.equity_basket import (
    BUCK, ME, EquityBasket, Pool, Underwater, equity_wheel_tasks, run_scenario)
from alberta_buck.sim.work_wheel import Clock, WorkWheel

K = 0.75


def world(prices=(1.0,), fee=0.003, k=K, seed=1e6, **kw):
    """Pools seeded by an outside LP (`seed` BUCK a side), the basket, its wheel."""
    pools = {}
    for n, p in enumerate(prices):
        pool = Pool(p, fee)
        pool.add("seed", seed / p, seed)
        pools[f"T{n}"] = pool
    b = EquityBasket(pools, K=k, **kw)
    w = WorkWheel(equity_wheel_tasks())
    w.bind(b)
    return b, w


def settle(b, w, ticks=60):
    """Let the wheel run to rest, a block passing after each tick."""
    for n in range(ticks):
        w.mark_dirty()
        w.tick(b, Clock(n, 0), max_work=4)
        for p in b.pools.values():
            p.mark()


def move(b, t, price):
    b.pools[t].arb_to(price)
    b.pools[t].mark()


def test_a_deposit_only_books_equity():
    b, _ = world()
    rid = b.deposit("T0", 100.0)
    assert b.receipts[rid].shares == pytest.approx(100.0 * (1 - 0.003 * (1 - K) / 2))
    assert b.idle["T0"] == 100.0 and b.debt == 0.0
    assert b.pools["T0"].liq.get(ME, 0.0) == 0.0     # nothing deployed yet


def test_the_wheel_deploys_with_K_credit():
    b, w = world()
    b.deposit("T0", 100.0)
    settle(b, w)
    assert b.debt == pytest.approx(K * 100.0, rel=1e-3)
    assert b.position_value("T0") == pytest.approx((1 + K) * 100.0, rel=5e-3)
    assert b.equity() == pytest.approx(100.0, rel=5e-3)
    assert b.idle["T0"] + b.idle_buck <= b.grain * b.gross()   # only crumbs wait


def test_a_buck_deposit_is_the_same_equity_as_a_token_deposit():
    bt, wt = world()
    bb, wb = world()
    rt = bt.deposit("T0", 100.0)
    rb = bb.deposit(BUCK, 100.0)
    # the same equity, less each one's own deployment charge
    assert bt.receipts[rt].shares == pytest.approx(100.0 * (1 - 0.003 * (1 - K) / 2))
    assert bb.receipts[rb].shares == pytest.approx(100.0 * (1 - 0.003 * (1 + K) / 2))
    settle(bt, wt)
    settle(bb, wb)
    assert bb.debt == pytest.approx(bt.debt, rel=3e-3)
    assert bb.value_of(rb) == pytest.approx(bt.value_of(rt), rel=3e-3)
    assert bb.position_value("T0") == pytest.approx(bt.position_value("T0"), rel=3e-3)


def test_entry_is_at_value_after_a_move():
    b, w = world()
    ra = b.deposit("T0", 100.0)
    settle(b, w)
    move(b, "T0", 1.10)
    a_before = b.value_of(ra)
    rb = b.deposit("T0", 50.0)                       # worth 55 BUCK
    charge = 0.003 * (1 - K) / 2                     # its deployment, paid in
    assert 55.0 * (1 - charge) <= b.value_of(rb) <= 55.0
    assert b.value_of(ra) >= a_before


@pytest.mark.parametrize("spot", [0.992, 1.008])
@pytest.mark.parametrize("asset", ["T0", BUCK])
def test_a_lagging_twap_gives_the_entrant_no_edge(spot, asset):
    """The spot has moved and the TWAP not yet: the entrant is valued at the
    lower of the two, the basket at the higher, so once the TWAP catches up
    the entrant holds no more than it brought (at the true price)."""
    b, w = world(fee=0.0, seed=1e9)
    ra = b.deposit("T0", 1000.0)
    settle(b, w)
    b.pools["T0"].arb_to(spot)                       # no block yet: the TWAP lags
    amount = 100.0
    brought = amount * spot if asset == "T0" else amount
    rb = b.deposit(asset, amount)
    b.pools["T0"].mark()
    assert b.value_of(rb) <= brought * (1 + 1e-9)
    assert b.value_of(ra) >= (b.value_of(ra) + b.value_of(rb) - brought) * (1 - 1e-9)


def test_funding_fills_every_pool_to_target_without_churn():
    """A BUCK deposit is routed pool by pool to the targets: no position is
    ever unwound to fix where the deposit landed."""
    b, w = world(prices=(1.0, 2.0, 4.0))
    removed = []
    op_remove = b.op_remove
    b.op_remove = lambda t, l: (removed.append(t), op_remove(t, l))
    b.deposit(BUCK, 3000.0)
    settle(b, w)
    assert removed == []
    for t, x in b.weights().items():
        assert abs(x - 1 / 3) <= b.band
    # the swaps' costs leave equity a little under 3000: K x equity binds
    assert b.debt == pytest.approx(K * b.equity(), rel=1e-3)


@pytest.mark.parametrize("prices, asset", [((1.0,), "T0"), ((1.0, 2.0, 4.0), BUCK)])
def test_a_deposit_pays_for_its_own_deployment(prices, asset):
    """The wheel's swaps to pair a deposit cost the pool fee; the deposit's
    shares are minted net of it, so the holders already there lose nothing.
    (A TOKEN deposit that overweights its pool leaves Rebalance a job: that
    trade is the basket's, as all rebalancing is.)"""
    b, w = world(prices=prices, seed=1e9)            # deep: no price impact
    ra = b.deposit(BUCK, 500.0)
    settle(b, w, ticks=200)
    for t, p in zip(b.pools, prices):               # arbitrage restores the market
        move(b, t, p)
    a0 = b.value_of(ra)
    b.deposit(asset, 3000.0 if asset == BUCK else 3000.0 / prices[0])
    settle(b, w, ticks=200)
    for t, p in zip(b.pools, prices):               # arbitrage restores the market
        move(b, t, p)
    # (what the charge does not cover is the swaps' price impact, which
    # grows with the deposit against the pools' depth: shared)
    assert b.value_of(ra) >= a0 * (1 - 1e-5)


def test_a_round_trip_takes_back_only_its_own():
    """Fees accrued to the positions before a deposit belong to those who
    earned them: a deposit redeemed at once takes back its own, less its
    costs -- never the pool's history or its fees."""
    b, w = world()
    b.deposit("T0", 1000.0)
    settle(b, w)
    for _ in range(20):                              # volume: fees accrue
        move(b, "T0", 1.05)
        move(b, "T0", 1.00)
    rb = b.deposit("T0", 10.0)
    pay = b.redeem(rb)
    assert pay["T0"] <= 10.0 * (1 + 1e-9)
    assert pay["T0"] >= 10.0 * 0.98


def test_a_pro_rata_exit_leaves_the_others_price_whole():
    b, w = world()
    b.deposit("T0", 100.0)
    rb = b.deposit("T0", 100.0)
    settle(b, w)
    move(b, "T0", 1.2)
    price = b.price()
    b.redeem(rb)
    for p in b.pools.values():
        p.mark()
    assert b.price() >= price * (1 - 1e-9)           # the exit's costs are its own


def test_the_debt_is_whole_and_burned_before_any_payout():
    b, w = world()
    rids = [b.deposit("T0", 100.0), b.deposit(BUCK, 60.0)]
    settle(b, w)
    move(b, "T0", 0.9)
    assert b.minted - b.burned == pytest.approx(b.debt)
    for r in rids:
        b.redeem(r)
        assert b.minted - b.burned == pytest.approx(b.debt)
    # only the treasury's shares remain, carrying only their share of the debt
    assert b.S == pytest.approx(b.treasury, abs=1e-9)


def test_a_K_cut_is_no_margin_call():
    k = {"K": 0.75}
    b, w = world(k=lambda: k["K"])
    b.deposit("T0", 100.0)
    settle(b, w)
    debt = b.debt
    k["K"] = 0.5
    settle(b, w)
    assert b.debt == pytest.approx(debt)             # nothing called back
    # New money gets only the room the smaller offer leaves: the pooled
    # debt rises to K x equity, where the old money alone was over it.
    b.deposit("T0", 100.0)
    settle(b, w)
    assert b.debt > debt
    assert b.debt == pytest.approx(0.5 * b.equity(), rel=1e-3)


def test_the_basket_takes_a_quarter_of_the_gain_and_none_of_a_loss():
    b, w = world(fee=0.0)
    rid = b.deposit("T0", 100.0)
    b.deposit("T0", 1000.0)                          # others keep the pool deep
    settle(b, w)
    move(b, "T0", 1.21)
    gain = b.value_of(rid) - 100.0
    t0 = b.treasury
    price = b.price()
    b.redeem(rid)
    assert (b.treasury - t0) * price == pytest.approx(0.25 * gain, rel=1e-9)

    b2, w2 = world(fee=0.0)
    rid2 = b2.deposit("T0", 100.0)
    b2.deposit("T0", 1000.0)
    settle(b2, w2)
    move(b2, "T0", 0.81)
    b2.redeem(rid2)
    assert b2.treasury == 0.0


def test_the_treasury_leaves_by_the_same_door():
    b, w = world(fee=0.0)
    rid = b.deposit("T0", 100.0)
    settle(b, w)
    move(b, "T0", 1.21)
    b.redeem(rid)
    assert b.treasury > 0 and b.debt > 0              # its shares carry their debt
    pay = b.redeem_treasury()
    assert pay["T0"] > 0
    assert b.S == pytest.approx(0.0, abs=1e-9)
    assert b.debt == pytest.approx(0.0, abs=1e-9)
    assert b.minted == pytest.approx(b.burned)


def test_an_exit_fee_stays_with_the_holders_who_stay():
    fee = {"f": 0.0}
    b, w = world(fee=0.0, exit_fee=lambda: fee["f"])
    ra = b.deposit("T0", 100.0)
    rb = b.deposit("T0", 100.0)
    settle(b, w)
    a0, b0 = b.value_of(ra), b.value_of(rb)
    fee["f"] = 0.02
    pay = b.redeem(rb)
    for p in b.pools.values():
        p.mark()
    assert pay["T0"] * b.pools["T0"].price == pytest.approx(0.98 * b0, rel=2e-3)
    assert b.value_of(ra) == pytest.approx(a0 + 0.02 * b0, rel=2e-3)
    assert b.minted - b.burned == pytest.approx(b.debt)


@pytest.mark.parametrize("p, equity", [(1.21, 117.5), (1.00, 100.0), (0.81, 82.5)])
def test_the_design_payoffs(p, equity):
    """doc/BASKET-EQUITY.org section 8 at K = 0.75, no fees: the depositor's
    equity is (1+K) E sqrt(p) - K E; the payout is that less 25% of a gain."""
    b, w = world(fee=0.0, seed=1e9)
    rid = b.deposit("T0", 100.0)
    settle(b, w)
    move(b, "T0", p)
    assert b.value_of(rid) == pytest.approx(equity, rel=1e-3)
    net = equity - 0.25 * max(equity - 100.0, 0.0)
    pay = b.redeem(rid)
    assert pay["T0"] * b.pools["T0"].price == pytest.approx(net, rel=2e-3)


def test_fees_are_equity_and_compound_unlevered():
    """Credit comes with deposits; earnings compound without new debt."""
    b, w = world()
    b.deposit("T0", 1000.0)
    settle(b, w)
    eq0, debt0, pos0 = b.equity(), b.debt, b.position_value("T0")
    for _ in range(20):
        move(b, "T0", 1.05)
        move(b, "T0", 1.00)
    settle(b, w)                                     # Sync collects, Deploy compounds
    grown = b.equity() - eq0
    assert grown > 0
    assert b.debt == pytest.approx(debt0)
    # all of it in the position, but for less than a grain or two still
    # owed or waiting in the wallet (at either end)
    assert b.position_value("T0") - pos0 == pytest.approx(
        grown, abs=2 * b.grain * b.gross())
    assert b.gross() - b.position_value("T0") <= b.grain * b.gross()


def test_residue_never_mints():
    """Rebalancing and exits move BUCK through the wallet; only a deposit's
    pending credit is ever minted."""
    b, w = world(prices=(1.0, 2.0))
    b.deposit("T0", 1000.0)
    settle(b, w, ticks=400)                          # rebalancing all the way
    assert b.pending < 1e-6
    assert b.debt == pytest.approx(K * 1000.0, rel=1e-3)


def test_rebalancing_moves_value_to_the_underweight_pool():
    b, w = world(prices=(1.0, 2.0))
    b.deposit("T0", 1000.0)                          # all of it lands in T0
    settle(b, w, ticks=400)
    wt = b.weights()
    assert abs(wt["T0"] - 0.5) <= b.band + 0.01
    assert abs(wt["T1"] - 0.5) <= b.band + 0.01
    assert b.equity() == pytest.approx(1000.0, rel=0.02)   # at the cost of its trades


def test_an_underwater_redemption_refuses_rather_than_steals():
    b, w = world(fee=0.0, seed=1e3)                  # a shallow pool
    rid = b.deposit("T0", 1000.0)
    settle(b, w)
    move(b, "T0", 0.05)                              # a 95% collapse
    with pytest.raises(Underwater):
        b.redeem(rid)


def _invariants(b):
    assert b.minted - b.burned == pytest.approx(b.debt, abs=1e-6)
    shares = sum(r.shares for r in b.receipts.values()) + b.treasury
    assert shares == pytest.approx(b.S, rel=1e-9, abs=1e-9)
    assert -1e-9 <= b.pending <= b.k() * b.gross() + 1e-6
    # (the debt may exceed K x equity after prices fall -- no margin call;
    # op_mint asserts the cap where it binds, at every mint)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_whole_machine_keeps_its_books(seed):
    """Reverting prices, arbitrage, depositors in and out with TOKEN and
    BUCK, the wheel every step: the books balance at every step, and on a
    reverting market the harvest reaches the depositors and the treasury."""
    r = run_scenario(seed=seed, check=_invariants)
    assert r["exits"] > 10
    assert r["treasury_value"] > 0
    assert r["leverage"] <= K + 1e-6
