"""WP-13: booking the agent stand-ins' inventory into the observer's
pseudo-stabilizer (CARRY-CONVEXITY.org 6.4 "What changes in wave 3";
WAVE3.org WP-3a / WP-13).

The Python stand-ins for the undertakings (WP-2) and the facility (WP-6)
hold positions the on-chain observer cannot read: the UndertakingAgent's
open books (absorbed_open, BUCK bought and held; issued_open, BUCK minted
and sold) and the FacilityAgent population's drawn lines (issued).  Before
K's daily cycle the loop books their sum into
`ShadowObserver.setShadowOffset` -- the sim-only pseudo-stabilizer WP-3a
created for exactly this -- so under either aggregation (S: lambda_off *
q / D; V: w_off * q / cap_off) the level's whole position reaches K:

    q_offset = ut_absorbed_open - ut_issued_open - fac_drawn     (BUCK, 6-dec)

Signs per WAVE3.org "Signs and units": absorbed POSITIVE, issued NEGATIVE.
A transaction is sent only when the value CHANGES, so a cell without these
agents (q stays 0) keeps a chain history identical to today's.  The
seeder's range is not an inventory in this sense and is not booked
(decision 11 lists its cap; a later package may register it).

Counters (ctr -> snapshot frame, WP-13 block): sh_offset (the booked value),
sh_offset_txs (bookings sent), sh_offset_err (the last failure).
"""

from __future__ import annotations


def net_inventory(ctr) -> int:
    """The stand-ins' aggregate position from the counters the agents keep
    (BUCK, 6-dec; absorbed positive, issued negative)."""
    return (int(ctr.get("ut_absorbed_open", 0))
            - int(ctr.get("ut_issued_open", 0))
            - int(ctr.get("fac_drawn", 0)))


def book(d, ctr) -> None:
    """Book the stand-ins' position into the observer when it changed.  A
    no-op without an observer (Direct, or a non-ops basket)."""
    obs = getattr(d, "observer", None)
    if obs is None:
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
