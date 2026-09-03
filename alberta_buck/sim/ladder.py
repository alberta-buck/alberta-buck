"""The weak-side LADDER -- CARRY-CONVEXITY.org D3 ("The weak side is a
LADDER, not an edge") and 7.4 ("The weak-side ladder"); WAVE3.org WP-2.

A pure schedule with no chain access: the basket's standing bid for
BUCK, paid in BUNDLES (par baskets: 1 bundle == 1 BUCK == 1 USDC at
parity), struck as tranches whose price worsens and whose size shrinks
with the reserve remaining.

  tranche k (0-based) bids   b_k = 1 - p*eps - k*delta   bundles per BUCK
  tranche k is sized         phi * R_k                    bundles
  where R_k is the reserve remaining when tranche k is struck, so after
  n completed tranches

      R_n = R_0 (1 - phi)^n          (never zero at finite n)
      b_n = 1 - p*eps - n delta      (the next bid)
      bvib <= 1 / b_n                (the deviation the ladder permits)

The pool arbitrage runs when the pools price BUCK below the current
tranche's bid, i.e. bvib > 1 / b_k (`edge_bvib`).  The deviation an
attacker can force grows only as the logarithm of the reserve fraction
spent, while the average price they receive worsens linearly in n:
there is no cliff to time, and the reserve is exhausted only at a
finite PRICE (b_k reaching zero), never at a finite size.

Units: `R0`, `fill()` amounts and the returned `spent` are bundles
(floats); `buck` is BUCK (the same par unit); `bid()` is bundles per
BUCK; `p` is the convenience factor of the single-token path (p = 1 is
the bundle path of D3).

Limits (CARRY-CONVEXITY.org 7.4):

  * delta = 0 is the FLAT EDGE: every tranche bids the same price, so
    the whole reserve is available at one price (`capacity()` is the
    entire remaining reserve) -- the finite wall with the cliff.
  * delta, phi -> 0 at a fixed ratio gamma = delta / phi is the CPMM
    limit: after spending a fraction 1 - rho of the reserve the number of
    tranches struck is n ~ -ln(rho) / phi, so the marginal bid becomes
    the continuous curve

        b(rho) = 1 - p*eps + gamma * ln(rho),

    a smooth, cliff-less bid that -- with gamma = 2 b_0 -- matches the
    constant-product marginal bid b_0 rho^2 to first order in the
    depletion (ln(rho^2) = 2 ln rho).  The two curves part beyond ~10%
    depletion: the ladder's bid falls as the LOG of the reserve fraction
    where the CPMM's falls as its SQUARE, so the ladder concedes less
    price per unit of reserve spent deep in the book.  Curve's
    stableswap amplification interpolates between the flat edge and the
    CPMM; the ladder is the piecewise-constant discretization of that
    family the basket can publish as standing tranches.
"""

from __future__ import annotations

_TINY = 1e-12
_MAX_STEPS = 100_000            # guard on the geometric tail of fill()


class Ladder:
    """The tranche schedule and its fill accounting.

    Ladder(eps, delta, phi, R0, p=1.0)

      eps    the undertakings' bundle discount (band half-width)
      delta  discount step per tranche (0 = flat edge)
      phi    tranche size as a fraction of the reserve remaining, (0, 1]
      R0     the weak-side reserve, bundles
      p      convenience factor on eps (1.0 = the bundle path)

    State: `R` (reserve remaining), `tranche` / `k` (current tranche,
    0-based), `spent` / `bought` (cumulative bundles out / BUCK in).
    `rho` is R / R0.  `reset()` returns the ladder to its struck state
    (the agent calls it when the absorbed position is fully unwound and
    the reserve is back in the book).
    """

    def __init__(self, eps: float, delta: float, phi: float, R0: float,
                 p: float = 1.0):
        if eps < 0:
            raise ValueError("eps must be >= 0")
        if delta < 0:
            raise ValueError("delta must be >= 0")
        if not 0.0 < phi <= 1.0:
            raise ValueError("phi must be in (0, 1]")
        if R0 <= 0:
            raise ValueError("R0 must be > 0")
        if p <= 0:
            raise ValueError("p must be > 0")
        if 1.0 - p * eps <= 0:
            raise ValueError("p * eps must be < 1 (the first bid must be positive)")
        self.eps = float(eps)
        self.delta = float(delta)
        self.phi = float(phi)
        self.R0 = float(R0)
        self.p = float(p)
        self.reset()

    def reset(self) -> None:
        self.R = self.R0            # reserve remaining, bundles
        self.k = 0                  # current tranche, 0-based
        self._struck = self.R0      # R_k: reserve when tranche k was struck
        self._filled = 0.0          # bundles filled in the current tranche
        self.spent = 0.0            # cumulative bundles paid out
        self.bought = 0.0           # cumulative BUCK bought

    # -- schedule ------------------------------------------------------- #

    @property
    def tranche(self) -> int:
        return self.k

    @property
    def rho(self) -> float:
        return self.R / self.R0

    def bid(self, k: int | None = None) -> float:
        """Bundles per BUCK the ladder pays in tranche k (default: the
        current tranche).  <= 0 means the schedule is exhausted."""
        kk = self.k if k is None else int(k)
        return 1.0 - self.p * self.eps - kk * self.delta

    @property
    def exhausted(self) -> bool:
        """True when the current bid is no longer positive: the price
        schedule has run out (only possible with delta > 0); the reserve
        itself is never exhausted at finite n."""
        return self.bid() <= _TINY

    def edge_bvib(self) -> float:
        """The bvib at which the current tranche is hit: the pools price
        BUCK at 1 / b_k bundles or cheaper.  inf when exhausted."""
        b = self.bid()
        return float("inf") if b <= _TINY else 1.0 / b

    def tranche_size(self) -> float:
        """phi * R_k: the current tranche's full size, bundles."""
        return self.phi * self._struck

    def tranche_remaining(self) -> float:
        """Bundles still available in the current tranche."""
        return max(0.0, min(self.R, self.tranche_size() - self._filled))

    def capacity(self) -> float:
        """Reserve available at the CURRENT bid: the tranche's remainder,
        or the whole reserve when delta == 0 (every tranche bids the same
        price -- the flat edge has no tranche boundary a taker can see)."""
        if self.exhausted:
            return 0.0
        if self.delta == 0.0:
            return self.R
        return self.tranche_remaining()

    # -- fills ---------------------------------------------------------- #

    def _advance(self) -> None:
        self.k += 1
        self._struck = self.R
        self._filled = 0.0

    def fill(self, amount_bundles: float) -> tuple[float, float, int]:
        """Pay out up to `amount_bundles` of reserve for BUCK, consuming
        tranches in order at their own bids.  Returns (buck_bought,
        avg_price in bundles per BUCK, tranches_completed).  A request
        larger than what the schedule offers is truncated: the fill stops
        when the bid reaches zero (delta > 0) or the reserve's geometric
        tail is spent to numerical zero."""
        amount = float(amount_bundles)
        if amount <= 0.0:
            return 0.0, 0.0, 0
        buck = spent = 0.0
        done = 0
        steps = 0
        while amount > _TINY and self.R > _TINY and steps < _MAX_STEPS:
            steps += 1
            b = self.bid()
            if b <= _TINY:
                break                       # schedule exhausted at a finite price
            size = self.tranche_size()
            rem = self.tranche_remaining()
            if rem <= _TINY:
                self._advance()
                done += 1
                continue
            take = min(amount, rem)
            buck += take / b
            spent += take
            amount -= take
            self.R -= take
            self._filled += take
            if self._filled >= size * (1.0 - 1e-12):
                self._advance()
                done += 1
        self.spent += spent
        self.bought += buck
        avg = spent / buck if buck > 0.0 else 0.0
        return buck, avg, done

    def __repr__(self) -> str:            # pragma: no cover - debugging aid
        return (f"Ladder(eps={self.eps}, delta={self.delta}, phi={self.phi}, "
                f"R0={self.R0}, p={self.p}; k={self.k}, rho={self.rho:.4f}, "
                f"bid={self.bid():.4f})")
