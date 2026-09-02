#!/usr/bin/env python3
"""A/B a chain-sim run with monetary operations against one without.

    python -m alberta_buck.sim.compare_ops OFF.json ON.json

The two vectors must come from the SAME scenario and seed, differing only in
whether MonetaryOpsAgent was in the roster:

    make nix-venv-sim-run-rebalancing-revert                    # OFF
    SIM_MONETARY_OPS=1 make nix-venv-sim-run-rebalancing-revert # ON

WHAT IS BEING COMPARED

The claim in alberta-buck-operations.org is that a basket which trades the
COMMON mode of its own filter bank -- the mean of the per-leg ladders, which
is basketValueInBuck -- damps excursions in BUCK's own valuation faster than
BUCK_K can, because K acts only through creditLimit and a credit book turns
over in months.  Three things have to be true for that to be worth doing:

  1. the excursion is smaller;
  2. the desk is paid for absorbing it;
  3. the depositors are not worse off for having it there.

The third is the one that is easy to lose.  A desk that damps the deviation
by accumulating an inventory it can never unwind has moved the loss, not
removed it, and the deviation series alone will not say so -- which is why
the desk's own terminal inventory is reported next to its P&L.

WHAT AN AGENT CANNOT SHOW

`mintFromBasket` and `burnFromBasket` both require msg.sender == basket, so
this agent reaches supply only through its own credit line.  Supply is
sum_a max(0, signedRaw(a)), so buying BUCK back while the agent's own signed
balance is negative removes float; buying beyond that just accumulates
inventory.  `noBook` counts the days the desk wanted to retire and had no
drawn line left to retire against.  That number is the size of the gap
between this agent and the contract change in phases 3 and 4 -- it is the
part of the mechanism that cannot be tested from outside the basket.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

DEV_THRESHOLD = 0.02        # the article's "days off 2%" line


def _load(p: str) -> dict:
    return json.loads(Path(p).read_text())


def _dev_series(frames) -> list[float]:
    """log(basketValueInBuck) per day.  bvib > 1 means BUCK is CHEAP: the
    basket costs more BUCK than it should."""
    out = []
    for f in frames:
        bv = f.get("basketVal", 0)
        out.append(math.log(bv / 1e18) if bv and bv > 0 else 0.0)
    return out


def _desk(frames) -> dict | None:
    """The operations desk's own book, marked in USDC."""
    def state(f):
        for a in f.get("arb2", []) or []:
            if a.get("cls") == "mo":
                return a
        return None

    first = next((state(f) for f in frames if state(f)), None)
    last = state(frames[-1])
    if first is None or last is None:
        return None

    def mark(f, st):
        # BUCK marked at the floating BUCK/USDC pool price (6-dec USDC per
        # BUCK); TOKEN already carries a USDC valuation from _tok_value.
        px = f.get("buck_usd", 1_000_000) or 1_000_000
        buck = (st.get("held", 0) - st.get("drawn", 0)) * px // 1_000_000
        return st.get("cash", 0) + st.get("tok", 0) + buck

    f0 = next(f for f in frames if state(f))
    return {
        "pnl": mark(frames[-1], last) - mark(f0, first),
        "inventory": last.get("held", 0),
        "drawn": last.get("drawn", 0),
        "cash": last.get("cash", 0),
        "tok": last.get("tok", 0),
    }


def summarize(v: dict) -> dict:
    f = v["frames"]
    dev = _dev_series(f)
    last = f[-1]
    kmax = max(x.get("buckK", 0) for x in f)
    return {
        "days": len(f),
        "peakDevBp": 1e4 * max(abs(x) for x in dev),
        "meanDevBp": 1e4 * sum(abs(x) for x in dev) / max(1, len(dev)),
        "endDevBp": 1e4 * dev[-1],
        "daysOff": sum(1 for x in dev if abs(x) > DEV_THRESHOLD),
        "supplyEnd": last.get("supply", 0),
        "treasuryBuck": last.get("treasuryBuck", 0),
        "treasuryShare": last.get("treasuryShare", 0),
        "dmProfitUsd": last.get("dmProfitUsd", 0),
        "navPost": v.get("meta", {}).get("teardown", {}).get("navPost", 0),
        "kClampDays": sum(1 for x in f if x.get("buckK", 0) >= kmax > 0),
        "q": [last.get(f"mo_q{i}", 0) for i in (1, 2, 3, 4)],
        "issued": last.get("mo_issued", 0),
        "retired": last.get("mo_retired", 0),
        "burned": last.get("mo_burned", 0),
        "bought": last.get("mo_bought", 0),
        "sold": last.get("mo_sold", 0),
        "noBook": last.get("mo_no_book", 0),
        "posLimit": last.get("mo_pos_limit", 0),
        "cumLimit": last.get("mo_cum_limit", 0),
        "throttled": last.get("mo_throttled", 0),
        "why": last.get("mo_why", {}),
        "err": last.get("mo_err", ""),
        "desk": _desk(f),
        # BuckBasketOps, when --basket ops deployed the two-mode shell.
        "mk": {
            "q": [last.get(f"mk_q{i}", 0) for i in (1, 2, 3, 4)],
            "ops": last.get("mk_ops", 0),
            "outstanding": last.get("mk_outstanding", 0),
            "held": last.get("mk_buck_held", 0),
            "offset": last.get("mk_offset", 0),
            "tokValue": last.get("mk_tok_value", 0),
            "idle": last.get("mk_idle", 0),
            "bound": last.get("mk_bound", 0),
            "noAdvice": last.get("mk_no_advice", 0),
            "slippage": last.get("mk_slippage", 0),
            "otherErr": last.get("mk_other_err", 0),
            "err": last.get("mk_err", ""),
        },
    }


def _row(label: str, off, on, fmt="{:>14,.0f}", delta=True) -> str:
    d = ""
    if delta and isinstance(off, (int, float)) and isinstance(on, (int, float)):
        d = f"   {on - off:+,.0f}"
    return (f"  {label:<22s}{fmt.format(off):>16s}{fmt.format(on):>16s}{d}")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print(__doc__)
        return 2
    off, on = summarize(_load(argv[0])), summarize(_load(argv[1]))
    if off["days"] != on["days"]:
        print(f"  WARNING: different horizons ({off['days']} vs {on['days']} "
              f"days) -- the comparison is not like-for-like\n")

    print(f"\n  MONETARY OPERATIONS A/B  ({off['days']} days)\n")
    print(f"  {'':<22s}{'ops OFF':>16s}{'ops ON':>16s}   delta")
    print(f"  {'-' * 60}")
    print("\n  THE EXCURSION  (log basketValueInBuck; >0 means BUCK cheap)")
    for k, lab in (("peakDevBp", "peak deviation bp"),
                   ("meanDevBp", "mean |deviation| bp"),
                   ("endDevBp", "terminal deviation bp"),
                   ("daysOff", "days beyond 2%")):
        print(_row(lab, off[k], on[k], "{:>14,.0f}"))

    print("\n  THE SYSTEM")
    print(_row("BUCK supply", off["supplyEnd"] / 1e6, on["supplyEnd"] / 1e6))
    # _basket_nav reduces algebraically to 2 x the pools' BUCK reserves: it
    # prices the TOKEN side at rb/rt and then multiplies back by rt, so the
    # TOKEN side always evaluates to rb.  A desk that buys BUCK OUT of the
    # pools and holds it therefore drops this measure by ~2x the float it
    # removed, with nothing destroyed -- so it must be read next to the
    # desk's own book, never alone.
    print(_row("pool NAV (=2x poolBUCK)", off["navPost"] / 1e6,
               on["navPost"] / 1e6))
    print(_row("treasury BUCK", off["treasuryBuck"] / 1e6,
               on["treasuryBuck"] / 1e6))
    print(_row("depositor P&L USD", off["dmProfitUsd"] / 1e6,
               on["dmProfitUsd"] / 1e6))
    print(_row("days buckK railed", off["kClampDays"], on["kClampDays"]))

    mk = on["mk"]
    if mk["ops"]:
        q = mk["q"]
        print("\n  THE BASKET DESK  (BuckBasketOps, on-chain policy)")
        print(f"    quadrants fired      Q1 absorb {q[0]:<6d} Q2 retire {q[1]:<6d}"
              f" Q3 supply {q[2]:<6d} Q4 issue {q[3]}")
        print(_row("net issued (BUCK)", 0, mk["outstanding"] / 1e6))
        print(_row("inventory held", 0, mk["held"] / 1e6))
        print(_row("TOKEN reserves left", 0, mk["tokValue"] / 1e6))
        book = mk["held"] + mk["tokValue"]
        print(_row("desk book (BUCK+TOKEN)", 0, book / 1e6))
        # The founding grant, if the run recorded one.  BUCK and USD are
        # 1:1 by construction at t0, so this is comparable to the book.
        cap = int(os.environ.get("SIM_OPS_CAPITAL_USD", "0")) * 10 ** 6 * 3
        if cap:
            retired = -mk["outstanding"] if mk["outstanding"] < 0 else 0
            print(_row("desk P&L vs capital", 0, (book - cap) / 1e6))
            print(f"""
    The desk's P&L and the float it retired are the SAME budget spent two
    ways, and they trade off exactly as the article's measure-fast/extract
    tension predicts.  Q1 and Q3 are the extraction path -- buy the dump,
    sell it back, keep the spread.  Q2 forgoes that: burning BUCK bought at
    a discount gives up the recovery profit to buy a permanent supply
    reduction instead, which accrues to every BUCK holder rather than to the
    desk.  {retired/1e6:,.0f} BUCK of float retired is what this desk bought
    with its loss.  A desk tuned to extract would hold and sell instead, and
    would stabilize less.""")
        print(f"""
    Read the pool-NAV delta against that book.  Pool NAV is 2 x the pools'
    BUCK reserves, so BUCK the desk buys out of the pools and holds leaves
    that measure by construction -- roughly 2x the float removed -- without
    being destroyed.  Off-pool assets ({book/1e6:,.0f} BUCK here) are the
    other half of the picture.""")
        print("\n  DOES THE DESK SUPPRESS K'S FORCING?")
        print(f"    float the desk removed  {mk['offset']/1e6:>14,.0f} BUCK")
        print(f"    buckK OFF -> ON         {off['kClampDays']:>6d} -> "
              f"{on['kClampDays']:<6d} days railed")
        print(f"""
    The desk damps the excursion by absorbing it, and basketValueInBuck is
    read from the very pools it absorbs into -- so a successful operation
    SHRINKS the error K sees.  If K then stops tightening, the long-term
    forcing the desk's position is a bet on never arrives and the desk is
    left holding inventory with nothing behind it.  Compare the K paths in
    the two vectors before trusting any P&L here.""")
        print("\n  WHY IT DID NOT ACT  (the quiet failures)")
        print(f"    out of TOKEN         {mk['idle']:>14,d}  "
              f"(Q1/Q2 are TOKEN-funded: this is the desk out of ammunition)")
        print(f"    bound bit            {mk['bound']:>14,d}  "
              f"(inventory or cumulative ceiling)")
        print(f"    inside deadband      {mk['noAdvice']:>14,d}")
        if mk["slippage"]:
            print(f"    OWN TWAP GUARD       {mk['slippage']:>14,d}  "
                  f"(the desk tripped the basket's redemption guard)")
        if mk["otherErr"]:
            print(f"    unclassified         {mk['otherErr']:>14,d}  "
                  f"{str(mk['err'])[:60]}")

    d = on["desk"]
    if d is None:
        if mk["ops"]:
            return 0
        print("\n  NO DESK IN THE 'ON' VECTOR -- was SIM_MONETARY_OPS or "
              "--basket ops set?")
        return 1
    print("\n  THE DESK")
    q = on["q"]
    print(f"    quadrants fired      Q1 absorb {q[0]:<6d} Q2 retire {q[1]:<6d}"
          f" Q3 supply {q[2]:<6d} Q4 issue {q[3]}")
    print(f"    BUCK bought/sold     {on['bought']/1e6:>14,.0f} "
          f"/ {on['sold']/1e6:,.0f}")
    print(f"    issued / retired     {on['issued']/1e6:>14,.0f} "
          f"/ {on['retired']/1e6:,.0f}   burned {on['burned']/1e6:,.0f}")
    print(f"    desk P&L (USD)       {d['pnl']/1e6:>14,.0f}")
    print(f"    terminal inventory   {d['inventory']/1e6:>14,.0f} BUCK held, "
          f"{d['drawn']/1e6:,.0f} drawn")
    print(f"    ... cash {d['cash']/1e6:,.0f} USDC, TOKEN {d['tok']/1e6:,.0f}")

    print("\n  THE BOUNDS  (each one stopped a runaway the model produced)")
    print(f"    position limit hit   {on['posLimit']:>14,d}  "
          f"(inventory past 10% of pool depth)")
    print(f"    cumulative cap hit   {on['cumLimit']:>14,d}  "
          f"(net outright past 10% of supply)")
    print(f"    NO BOOK TO RETIRE    {on['noBook']:>14,d}  "
          f"(wanted Q2, had no drawn line -- see below)")
    print(f"    mint throttled       {on['throttled']:>14,d}")
    if on["why"]:
        print("    refusals:")
        for k, n in sorted(on["why"].items(), key=lambda kv: -kv[1])[:4]:
            print(f"      {n:>5d}x {k[:90]}")
    if on["err"]:
        print(f"    last error: {str(on['err'])[:120]}")

    if on["noBook"]:
        print(f"""
  'NO BOOK TO RETIRE' is the agent/basket gap, measured.  Supply is
  sum_a max(0, signedRaw(a)), so this desk removes float only while its own
  signed balance is negative; past that a purchase is inventory, not
  retirement.  The basket's burnFromBasket has no such limit, and it is
  callable only by the basket.  {on['noBook']} days is how often the policy
  asked for an outright retirement that no agent can perform.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
