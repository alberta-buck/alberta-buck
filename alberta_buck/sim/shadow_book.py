"""WP-13 / WP-14: booking the agent stand-ins' inventory into the observer
(CARRY-CONVEXITY.org 6.4 "What changes in wave 3"; WAVE3.org WP-3a / WP-13;
decision 17 and WP-14 for the per-class stabilizers).

The Python stand-ins for the undertakings (WP-2), the facility (WP-6) and
the seeder (WP-8) hold positions the on-chain observer cannot read: the
UndertakingAgent's open books (absorbed_open, BUCK bought and held;
issued_open, BUCK minted and sold), the FacilityAgent population's drawn
lines (issued) and the SeederAgent's converted range.  Before K's daily
cycle (and at each tick start) the loop books them into the observer, in
one of two ways:

PER CLASS (WP-14, decision 17; the default, `SIM_SHADOW_PERCLASS=1`).
deploy.py registers one sim-only `SimStabilizer` per agent class present
in the cell -- uts (the undertakings' strong side), utw (their weak side),
fac (the facility population), sd (the seeder) -- at decision 11's lambdas
and cost weights, and `book_classes` sends each class's (book, cap) through
`setBookAndCap` whenever either changes:

    uts   q = -ut_issued_open     cap = ut_cap  (reserve_frac x NAV at the
                                              strike: the strong side's
                                              symmetric notional)
    utw   q =  ut_absorbed_open   cap = ut_cap  (the ladder's R0, the same
                                              number)
    fac   q = -fac_drawn          cap = fac_cap (sum_i max_frac_i x limit_i)
    sd    q =  sd_q               cap = sd_cap  (the seeder's budget)

so each class's fill q / cap means the same thing at every depth (WP-16's
finding: the lumped cap below is target_buck_m, not any real reserve).

LUMPED (WP-13; `SIM_SHADOW_PERCLASS=0`, or a tree without the per-class
contracts).  The sum

    q_offset = ut_absorbed_open - ut_issued_open - fac_drawn     (BUCK, 6-dec)

goes to `ShadowObserver.setShadowOffset`, the sim-only pseudo-stabilizer
WP-3a created, at one lambda, one weight and one cap; the seeder is not
booked.  Kept for comparisons.

Signs per WAVE3.org "Signs and units": absorbed POSITIVE, issued NEGATIVE.
A transaction is sent only when the value CHANGES, so a cell without these
agents (every book 0) keeps a chain history identical to today's, and under
S the per-class sum reproduces the lumped number exactly (the same integer
over the same D; test/SimStabilizer.t.sol).

Counters (ctr -> snapshot frame): lumped -- sh_offset (the booked value),
sh_offset_txs, sh_offset_err (WP-13 block); per class -- sh_class_txs,
sh_class_err and, per registered class, sh_<cls>_q / _cap / _stale /
_excluded / _w read from chain (WP-14 block).
"""

from __future__ import annotations

# The per-class stabilizers, in registration order (deploy.py, WP-14).
CLASSES = ("uts", "utw", "fac", "sd")


def net_inventory(ctr) -> int:
    """The stand-ins' aggregate position from the counters the agents keep
    (BUCK, 6-dec; absorbed positive, issued negative) -- the lumped book."""
    return (int(ctr.get("ut_absorbed_open", 0))
            - int(ctr.get("ut_issued_open", 0))
            - int(ctr.get("fac_drawn", 0)))


def class_books(ctr) -> dict:
    """{cls: (q, cap)} per agent class from the counters (module doc)."""
    ut_cap = int(ctr.get("ut_cap", 0))
    return {"uts": (-int(ctr.get("ut_issued_open", 0)), ut_cap),
            "utw": (int(ctr.get("ut_absorbed_open", 0)), ut_cap),
            "fac": (-int(ctr.get("fac_drawn", 0)), int(ctr.get("fac_cap", 0))),
            "sd": (int(ctr.get("sd_q", 0)), int(ctr.get("sd_cap", 0)))}


def book_classes(d, ctr) -> None:
    """WP-14: book each class's (q, cap) into its own SimStabilizer when
    either changed.  A class deployed but not yet registered with the
    observer (added to the cell by an intervention) is registered on its
    first non-zero cap, at the deploy's lambda / weight for that class."""
    stabs = getattr(d, "sim_stabs", None) or {}
    if not stabs:
        return
    obs = getattr(d, "observer", None)
    reg = getattr(d, "sim_stab_reg", None)
    gains = getattr(d, "sim_stab_gains", None) or {}
    books = class_books(ctr)
    for cls, st in stabs.items():
        q, cap = books.get(cls, (0, 0))
        key = f"shb_{cls}"
        if (q, cap) == tuple(ctr.get(key, (0, 0))):
            continue
        try:
            d.chain.send(st.functions.setBookAndCap(q, cap), sender=d.gov)
            if (reg is not None and cls not in reg and cap > 0
                    and obs is not None):
                lam, w = gains.get(cls, (0, 10 ** 18))
                d.chain.send(obs.functions.addStabilizer(st.address, lam),
                             sender=d.gov)
                if w != 10 ** 18:
                    d.chain.send(obs.functions.setStabilizerWeight(
                        st.address, w), sender=d.gov)
                reg.add(cls)
        except Exception as e:                     # noqa: BLE001
            ctr["sh_class_err"] = f"{cls}: {e!r}"[:160]
            continue
        ctr[key] = (q, cap)
        ctr["sh_class_txs"] = ctr.get("sh_class_txs", 0) + 1


def book(d, ctr) -> None:
    """Book the stand-ins' position into the observer when it changed.  A
    no-op without an observer (Direct, or a non-ops basket); per class
    when the per-class stabilizers are deployed (WP-14), lumped otherwise."""
    obs = getattr(d, "observer", None)
    if obs is None:
        return
    if getattr(d, "sim_stabs", None):
        book_classes(d, ctr)
        return
    q = net_inventory(ctr)
    if q == int(ctr.get("sh_offset", 0)):
        return
    try:
        d.chain.send(obs.functions.setShadowOffset(q), sender=d.gov)
    except Exception as e:                         # noqa: BLE001
        ctr["sh_offset_err"] = repr(e)[:160]
        return
    ctr["sh_offset"] = q
    ctr["sh_offset_txs"] = ctr.get("sh_offset_txs", 0) + 1
