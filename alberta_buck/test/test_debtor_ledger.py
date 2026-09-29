"""Audit the honest debtor's BUCK-vs-counterfactual net-worth accounting.

The isolation experiment (alberta_buck/sim/experiments/isolation.toml) runs
exactly ONE BuckCreditDebtorAgent with every knob pinned, so its two ledgers
can be checked against first principles:

  1. The no-BUCK counterfactual (hypo) must equal a pure-Python replica of
     the amortization ledger: daily compounding at apr, monthly income and
     bank payments -- byte-for-byte integer arithmetic.

  2. The BUCK-path advantage must satisfy the conservation identity

         adv(t) = nw(t) - hypo(t)
                = interest_saved(t) + jubilee(t) - trade_loss(t)
                  + hypo_premium(t) - (premium_paid(t) - deposit(t))

     where interest_saved is the cumulative extra interest the hypo mortgage
     accrues over the real one, jubilee is Buck's accrued relief quote on the lien
     (the liability is valued at its close cost), trade_loss is the
     par-value cost of crossing the pool, and hypo_premium is the insurance
     premium the counterfactual pays as a cost.  Both paths insure the same
     asset at the same rate; the BUCK path's premium is instead a DEPOSIT
     (premium_paid, made at each mint) that the pool invests to earn the
     premiums and returns when the insurance is dropped, so it counts in nw
     as an asset (deposit; equal to premium_paid until a refund).
     Everything on the right is independently measured, so a leak anywhere
     breaks the identity.

  3. Doctrine (net of costs, BUCK is superior): interest_saved(t) >= 0 and
     non-decreasing, the unwind only ever buys below USD par, and the
     terminal advantage is positive after charging the deposit its
     opportunity cost -- the mortgage interest the deposited capital would
     have saved had it paid the mortgage down instead.

  4. Jubilee melt: BuckCredit ages activated coverage (~2%/yr) and the
     quoted redeem cost of the position declines year by year -- never
     force-closed.

Workflow:
    make nix-venv-sim-isolation   # runs the experiment, then this test
or manually:
    python -m alberta_buck.sim --experiment \
        alberta_buck/sim/experiments/isolation.toml --backend pyrevm
    python -m pytest alberta_buck/test/test_debtor_ledger.py -v -s
"""

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "test" / "vectors" / "eq-eq-isolation.json"
OUT = REPO / "images" / "equilibrium-isolation.png"

MONTH = 30
M6 = 10 ** 6

# Pinned knobs -- MUST mirror experiments/isolation.toml.
APR = 0.055
MORTGAGE0 = 800 * 1_000 * M6
INCOME = 260 * 1_000 * M6
FACE0 = 900 * 1_000 * M6        # the insured value, in both paths
PREMIUM_BP = 35                 # 0.35%/yr


def _payment(mortgage: int, apr: float) -> int:
    mrate = apr / 12.0
    return int(mortgage * mrate / (1.0 - (1.0 + mrate) ** -300))


def _frames():
    data = json.loads(DATA.read_text())
    frames = [f for f in data["frames"] if f.get("octl")]
    assert frames, "no octl frames in eq-eq-isolation.json"
    days = [f["day"] for f in frames]
    ags = [f["octl"][0] for f in frames]
    assert all(len(f["octl"]) == 1 for f in frames), "isolation = ONE debtor"
    return days, ags


def _replay_ledgers(days):
    """Replay the agent's BOTH ledgers' deterministic parts: daily accrual +
    monthly income/payments.  Returns per-frame-day dicts of the hypo ledger
    and the per-step accrual amounts for both mortgages (the real ledger's
    accrual is applied to whatever principal survives deploys, so it is
    reconstructed in the identity test from the frame series instead)."""
    payment = _payment(MORTGAGE0, APR)
    hypo_m, hypo_c, hypo_prem = MORTGAGE0, 0, 0
    last_day, last_month = 0, -MONTH
    out = []
    for day in days:
        gap = max(0, day - last_day)
        last_day = day
        if gap > 0:
            g = (1.0 + APR / 365.0) ** gap
            hypo_m = int(hypo_m * g)
        if day - last_month >= MONTH:
            months = max(1, (day - last_month) // MONTH)
            last_month = day
            inc = INCOME * months // 12
            hypo_c += inc
            hprem = min(FACE0 * PREMIUM_BP // 10_000 * months // 12, hypo_c)
            hypo_c -= hprem
            hypo_prem += hprem
            hdue = min(payment * months, hypo_m)
            hpaid = min(hdue, hypo_c)
            hypo_c -= hpaid
            hypo_m -= hpaid
        out.append({"hypo": hypo_c - hypo_m, "hypo_m": hypo_m,
                    "hypo_c": hypo_c, "hypo_prem": hypo_prem})
    return out


@pytest.mark.skipif(not DATA.exists(),
                    reason="eq-eq-isolation.json not generated; "
                           "run: make nix-venv-sim-isolation")
def test_counterfactual_ledger_exact():
    """The agent's hypo ledger IS the closed-form amortization replica,
    premiums included."""
    days, ags = _frames()
    replica = _replay_ledgers(days)
    for day, a, r in zip(days, ags, replica):
        assert a["hypo"] == r["hypo"], (
            f"day {day}: agent hypo {a['hypo']} != replica {r['hypo']} "
            f"(replica cash {r['hypo_c']}, mortgage {r['hypo_m']})")
        assert a["hypo_premium"] == r["hypo_prem"], (
            f"day {day}: agent premiums {a['hypo_premium']} != replica "
            f"{r['hypo_prem']}")


@pytest.mark.skipif(not DATA.exists(),
                    reason="eq-eq-isolation.json not generated; "
                           "run: make nix-venv-sim-isolation")
def test_advantage_conservation_identity():
    """adv(t) = interest_saved + jubilee - trade_loss + hypo_premium
              - (premium_paid - deposit).

    interest_saved is reconstructed step-by-step from the frame series
    itself: between post-act frames, each mortgage first compounds by g,
    then payments reduce it; the accrual part is int(m*g) - m.  Any flow
    the telemetry misses (a leak) breaks this equality."""
    days, ags = _frames()
    replica = _replay_ledgers(days)
    tol_step = 50 * M6            # $50/step integer-rounding headroom
    worst = 0
    interest_saved = 0
    for i in range(1, len(days)):
        gap = days[i] - days[i - 1]
        g = (1.0 + APR / 365.0) ** gap
        acc_real = int(ags[i - 1]["mortgage"] * g) - ags[i - 1]["mortgage"]
        acc_hypo = int(replica[i - 1]["hypo_m"] * g) - replica[i - 1]["hypo_m"]
        interest_saved += acc_hypo - acc_real
        adv = ags[i]["nw"] - ags[i]["hypo"]
        rhs = (interest_saved + ags[i].get("jub", 0) - ags[i]["trade_loss"]
               + ags[i]["hypo_premium"]
               - (ags[i]["premium_paid"] - ags[i]["deposit"]))
        drift = abs(adv - rhs)
        worst = max(worst, drift)
        assert drift <= tol_step * (i + 1), (
            f"day {days[i]}: adv {adv/M6:,.0f} != interest_saved "
            f"{interest_saved/M6:,.0f} + counterfactual premiums "
            f"{ags[i]['hypo_premium']/M6:,.0f} - trade_loss "
            f"{ags[i]['trade_loss']/M6:,.0f} (drift ${drift/M6:,.0f})")
    print(f"\n  conservation drift, worst: ${worst/M6:,.2f}")


@pytest.mark.skipif(not DATA.exists(),
                    reason="eq-eq-isolation.json not generated; "
                           "run: make nix-venv-sim-isolation")
def test_buck_path_uniformly_superior_net_of_costs():
    """Doctrine: compared fairly -- both paths insured alike, the
    counterfactual paying its premiums as a cost, the BUCK path's deposit
    charged its opportunity cost -- the BUCK path comes out ahead:
    interest_saved(t) >= 0 non-decreasing, the costs stay tranche-sized,
    and the terminal advantage is positive."""
    days, ags = _frames()
    replica = _replay_ledgers(days)
    interest_saved, prev_saved, opportunity = 0, 0, 0
    for i in range(1, len(days)):
        gap = days[i] - days[i - 1]
        g = (1.0 + APR / 365.0) ** gap
        acc_real = int(ags[i - 1]["mortgage"] * g) - ags[i - 1]["mortgage"]
        acc_hypo = int(replica[i - 1]["hypo_m"] * g) - replica[i - 1]["hypo_m"]
        interest_saved += acc_hypo - acc_real
        # The deposit could have paid the mortgage down instead: what it
        # would have saved, at the mortgage rate, is its cost.
        opportunity += int(ags[i - 1]["deposit"] * g) - ags[i - 1]["deposit"]
        assert interest_saved >= prev_saved - M6, (
            f"day {days[i]}: interest_saved regressed")
        prev_saved = interest_saved
    final = ags[-1]
    # The voluntary unwind may only ever buy BELOW USD par (the obligation
    # is a par-valued claim on own assets; there is never a reason to pay
    # above par to extinguish it).  Small headroom for the pool fee.
    assert final["unwind_loss"] <= 2_000 * M6, (
        f"unwind bought above par: unwind_loss "
        f"${final['unwind_loss']/M6:,.0f} on ${final['unwound']/M6:,.0f} "
        f"unwound")
    # poolPrincipal ~= premiumRate (0.35%) x POOL_ROI_INV (10) of gross
    # minted: a returnable insurance-pool deposit, structurally ~3.5% of the
    # retired principal (see Buck.sol _allocateMint / quoteBurn poolRefund).
    assert final["premium_paid"] < 0.05 * MORTGAGE0, (
        f"deposit ${final['premium_paid']/M6:,.0f} exceeds the structural "
        f"~3.5% pool-principal bound")
    adv = final["nw"] - final["hypo"] - opportunity
    assert adv > 0, (f"terminal advantage ${adv/M6:,.0f} <= 0: interest "
                     f"saved ${interest_saved/M6:,.0f}, counterfactual "
                     f"premiums ${final['hypo_premium']/M6:,.0f}, deposit "
                     f"${final['deposit']/M6:,.0f} (opportunity cost "
                     f"${opportunity/M6:,.0f}), trade loss "
                     f"${final['trade_loss']/M6:,.0f}")
    print(f"\n  terminal: adv ${adv/M6:,.0f}  interest_saved "
          f"${interest_saved/M6:,.0f}  jubilee ${final.get('jub', 0)/M6:,.0f}"
          f"  counterfactual premiums ${final['hypo_premium']/M6:,.0f}"
          f"  deposit ${final['deposit']/M6:,.0f} (opportunity cost "
          f"${opportunity/M6:,.0f})  trade_loss ${final['trade_loss']/M6:,.0f}"
          f"  unwound ${final['unwound']/M6:,.0f} (loss "
          f"${final['unwind_loss']/M6:,.0f})")


@pytest.mark.skipif(not DATA.exists(),
                    reason="eq-eq-isolation.json not generated; "
                           "run: make nix-venv-sim-isolation")
def test_jubilee_melts_obligations():
    """The liability side of a BUCK position reduces year by year: Buck
    ages the lien (issuance-seconds) and quotes the accrued relief via
    reliefOf(account) -- the redemption discount paid from the fund when
    the lien is repaid or the credit burned.  The agent values its drawn
    obligation net of that quote; measure the quoted relief's growth over
    the carried window against the ~2%/yr doctrine rate."""
    days, ags = _frames()
    carried = [(d, a) for d, a in zip(days, ags) if a["drawn"] > M6]
    if len(carried) < 2:
        pytest.skip("agent never carried a lien")
    (d0, a0), (d1, a1) = carried[0], carried[-1]
    span_years = (d1 - d0) / 365.0
    if span_years < 0.5:
        pytest.skip("carried window too short to observe melt")
    accrued = a1.get("jub", 0) - a0.get("jub", 0)
    # The lien varies over the window; demand at least half the doctrine
    # rate on the window's MINIMUM lien as a conservative floor.
    lien_floor = min(a["drawn"] for _, a in carried)
    assert accrued >= 0.5 * 0.02 * span_years * lien_floor, (
        f"quoted relief ${accrued/M6:,.0f} over {span_years:.1f}y on a "
        f"lien >= ${lien_floor/M6:,.0f}: below the ~2%/yr doctrine")


@pytest.mark.skipif(not DATA.exists(),
                    reason="eq-eq-isolation.json not generated; "
                           "run: make nix-venv-sim-isolation")
def test_isolation_plot():
    """Diagnostic: both net-worth curves + the advantage decomposition."""
    import os
    cache = Path(os.environ.get("TMPDIR", "/tmp")) / "alberta-buck-mpl"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    days, ags = _frames()
    replica = _replay_ledgers(days)
    nw = [a["nw"] / M6 for a in ags]
    hypo = [a["hypo"] / M6 for a in ags]
    adv = [(a["nw"] - a["hypo"]) / M6 for a in ags]
    hprem = [a["hypo_premium"] / M6 for a in ags]
    tl = [a["trade_loss"] / M6 for a in ags]
    jub = [a.get("jub", 0) / M6 for a in ags]
    saved, s = [0.0], 0
    for i in range(1, len(days)):
        g = (1.0 + APR / 365.0) ** (days[i] - days[i - 1])
        s += ((int(replica[i - 1]["hypo_m"] * g) - replica[i - 1]["hypo_m"])
              - (int(ags[i - 1]["mortgage"] * g) - ags[i - 1]["mortgage"]))
        saved.append(s / M6)

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    ax = axes[0]
    ax.plot(days, nw, color="#2a78d6", lw=2, label="BUCK path net worth")
    ax.plot(days, hypo, color="#52514e", lw=2, ls="--",
            label="no-BUCK counterfactual")
    ax.set_title("net worth: BUCK path vs counterfactual (isolation)")
    ax.set_xlabel("day"); ax.set_ylabel("$"); ax.grid(True, alpha=0.25)
    ax.legend(fontsize=9)
    ax = axes[1]
    ax.plot(days, adv, color="#1baf7a", lw=2, label="advantage (nw - hypo)")
    ax.plot(days, saved, color="#2a78d6", lw=1.6, ls=":",
            label="interest saved")
    ax.plot(days, jub, color="#7d3ec1", lw=1.6, ls=":",
            label="jubilee relief")
    ax.plot(days, hprem, color="#eda100", lw=1.6, ls=":",
            label="counterfactual premiums")
    ax.plot(days, tl, color="#e34948", lw=1.6, ls=":", label="trade loss")
    ax.axhline(0, color="#0b0b0b", lw=0.8, alpha=0.3)
    ax.set_title("advantage decomposition\n"
                 "adv = interest_saved + jubilee + counterfactual premiums"
                 " - trade_loss")
    ax.set_xlabel("day"); ax.set_ylabel("$"); ax.grid(True, alpha=0.25)
    ax.legend(fontsize=9)
    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=140)
    plt.close(fig)
    print(f"\nWrote {OUT.relative_to(REPO)}")
