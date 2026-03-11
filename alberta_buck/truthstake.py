"""TruthStake: decentralized oracle via Kalman filtering with stake-weighted reputation.

Reporters submit observations of an externally visible value (commodity price,
exchange rate, temperature -- anything truthful people can agree on).  A scalar
Kalman filter extracts the true signal, weighting each reporter by the inverse of
their historical error variance.  Reporters stake tokens on their submissions;
after settlement, honest reporters harvest the stakes of liars.

The Kalman filter operates on normalized values (value / scale) so that the
filter parameters (P, Q, R) are scale-independent and work identically for
Gold at $2900 or Silver at $32.

Key mechanisms:
  - Reputation-weighted Kalman gain: proven-accurate reporters move the estimate;
    noisy or adversarial reporters are suppressed.
  - Early submission bonus: the first accurate reporters earn the most, rewarding
    speed and conviction.  The entire reward pool is redistributed weighted by
    submission order, so early honest reporters always earn more than late ones.
  - Challenge escalation: any participant can pay to extend the settlement window,
    increase the quorum, and amplify stakes -- making sustained manipulation
    exponentially expensive.
"""

import logging
import math
import random
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kalman filter (scalar)
# ---------------------------------------------------------------------------

class KalmanOracle:
    """1-D Kalman filter for price estimation.

    Operates on normalized values (near 1.0).  Each observation is weighted by
    its measurement noise R, which is derived from the reporter's reputation.
    """

    def __init__(self, estimate=1.0, P=0.01, Q=0.0001):
        self.x = estimate   # current estimate (normalized)
        self.P = P           # estimate uncertainty (variance)
        self.Q = Q           # process noise per round

    def predict(self):
        """Time-propagation: uncertainty grows by Q each round."""
        self.P += self.Q

    def update(self, z, R):
        """Incorporate observation *z* with measurement noise *R*.

        Returns the Kalman gain K (0..1) indicating how much this observation
        moved the estimate.
        """
        K = self.P / (self.P + R)
        self.x += K * (z - self.x)
        self.P *= (1.0 - K)
        return K


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------

class Reporter:
    """A participant who submits price observations."""

    def __init__(self, name, stake=100.0):
        self.name = name
        self.stake = stake           # available balance
        self.ema_sq_error = 1.0      # exponential moving avg of squared relative error
        self.n_settled = 0           # rounds settled (for EMA warmup)
        self.earnings = 0.0          # cumulative winnings
        self.losses = 0.0            # cumulative losses

    @property
    def R(self):
        """Measurement noise derived from track record.

        New reporters (n_settled < 3) get high R (low influence).
        Proven reporters get R proportional to their historical error variance.
        R is in normalized units (relative to price), matching the Kalman filter.
        """
        if self.n_settled < 3:
            return 1.0               # untrusted: sigma ~100% (barely moves estimate)
        return max(0.0001, self.ema_sq_error)

    def observe(self, true_price, noise_std=0.0, bias=0.0):
        """Generate a price observation.  Simulation helper -- not part of the
        on-chain mechanism (reporters bring their own observations)."""
        noise = random.gauss(0, noise_std) if noise_std > 0 else 0.0
        return true_price * (1.0 + noise + bias)

    def __repr__(self):
        return (f"Reporter({self.name!r}, stake={self.stake:.1f}, R={self.R:.6f}, "
                f"earnings={self.earnings:.1f}, losses={self.losses:.1f})")


# ---------------------------------------------------------------------------
# Submission, Challenge, Round
# ---------------------------------------------------------------------------

@dataclass
class Submission:
    reporter: Reporter
    value: float           # original (non-normalized) submitted value
    stake: float
    index: int             # submission order within round (0 = first)


@dataclass
class Challenge:
    challenger: Reporter
    bond: float
    level: int = 1          # escalation level (doubles per escalation)

    @property
    def quorum_increase(self):
        return 2 * self.level

    @property
    def stake_multiplier(self):
        return 2.0 ** self.level

    @property
    def window_extension(self):
        return self.level    # additional settlement rounds


@dataclass
class Round:
    round_id: int
    submissions: list = field(default_factory=list)
    challenges: list = field(default_factory=list)
    settled: bool = False
    settled_value: float = None
    reward_pool: float = 0.0


# ---------------------------------------------------------------------------
# Oracle (simulation driver)
# ---------------------------------------------------------------------------

# Early-bonus decay: bonus = 2^(-index / halflife).
# First submitter gets bonus=1.0, later submitters get exponentially less.
DEFAULT_EARLY_HALFLIFE = 3.0

# EMA smoothing for reporter error tracking
DEFAULT_EMA_ALPHA = 0.3

# Minimum stake per submission (before challenge multiplier)
DEFAULT_MIN_STAKE = 1.0

# Relative error tolerance: submissions within this fraction of the settled
# value are considered "honest" and share the reward pool.
DEFAULT_TOLERANCE = 0.05   # 5%

# Gating: submissions beyond this many sigma from the current estimate are
# rejected outright (returns stake, no penalty -- just ignored).
DEFAULT_GATE_SIGMA = 5.0


class Oracle:
    """TruthStake oracle: Kalman filter + reputation + challenge escalation.

    All values are normalized internally (value / scale) so that filter
    parameters are scale-independent.

    Usage (simulation)::

        oracle = Oracle(initial_estimate=2900.0)
        alice = Reporter("alice", stake=100)
        bob   = Reporter("bob",   stake=100)
        oracle.register(alice)
        oracle.register(bob)

        oracle.open_round()
        oracle.submit(alice, alice.observe(true_price=2900, noise_std=0.005), stake=5)
        oracle.submit(bob,   bob.observe(true_price=2900, noise_std=0.005),   stake=5)
        result = oracle.settle(true_price=2900)
    """

    def __init__(
        self,
        initial_estimate=0.0,
        initial_P=0.01,
        Q=0.0001,
        min_stake=DEFAULT_MIN_STAKE,
        tolerance=DEFAULT_TOLERANCE,
        early_halflife=DEFAULT_EARLY_HALFLIFE,
        ema_alpha=DEFAULT_EMA_ALPHA,
        gate_sigma=DEFAULT_GATE_SIGMA,
    ):
        # Normalization scale: all values are divided by this before entering
        # the Kalman filter, keeping the filter operating near 1.0.
        self.scale = abs(initial_estimate) if initial_estimate != 0 else 1.0
        self.kalman = KalmanOracle(estimate=1.0, P=initial_P, Q=Q)
        self.reporters = {}          # name -> Reporter
        self.rounds = []             # completed rounds
        self.current_round = None
        self.min_stake = min_stake
        self.tolerance = tolerance
        self.early_halflife = early_halflife
        self.ema_alpha = ema_alpha
        self.gate_sigma = gate_sigma

    def _normalize(self, value):
        return value / self.scale

    def _denormalize(self, normalized):
        return normalized * self.scale

    # -- Registration -------------------------------------------------------

    def register(self, reporter):
        self.reporters[reporter.name] = reporter

    # -- Round lifecycle ----------------------------------------------------

    def open_round(self):
        """Start a new reporting round."""
        if self.current_round is not None and not self.current_round.settled:
            raise RuntimeError("Previous round not yet settled")
        rid = len(self.rounds)
        self.current_round = Round(round_id=rid)
        # Kalman time-propagation: uncertainty grows between rounds
        self.kalman.predict()
        return self.current_round

    def submit(self, reporter, value, stake=None):
        """Reporter submits an observation with stake."""
        rnd = self.current_round
        if rnd is None or rnd.settled:
            raise RuntimeError("No open round")
        if stake is None:
            stake = self.min_stake
        effective_min = self.min_stake * self._stake_multiplier()
        if stake < effective_min:
            raise ValueError(f"Stake {stake} below minimum {effective_min}")
        if reporter.stake < stake:
            raise ValueError(f"{reporter.name} has insufficient balance ({reporter.stake} < {stake})")

        z = self._normalize(value)

        # Gating: reject extreme outliers (no penalty, stake returned)
        if self.kalman.P > 0:
            sigma = math.sqrt(self.kalman.P)
            if sigma > 0 and abs(z - self.kalman.x) > self.gate_sigma * sigma:
                log.debug("Gated submission from %s: value %.4f (norm %.4f) too far from estimate %.4f",
                          reporter.name, value, z, self.kalman.x)
                return None

        idx = len(rnd.submissions)
        sub = Submission(reporter=reporter, value=value, stake=stake, index=idx)
        rnd.submissions.append(sub)

        # Deduct stake from reporter balance (held in escrow)
        reporter.stake -= stake
        rnd.reward_pool += stake

        # Feed the Kalman filter with normalized value
        K = self.kalman.update(z, reporter.R)
        log.debug("Round %d sub %d: %s=%.2f (norm=%.6f) stake=%.1f R=%.6f K=%.4f -> est=%.6f P=%.6f",
                  rnd.round_id, idx, reporter.name, value, z, stake,
                  reporter.R, K, self.kalman.x, self.kalman.P)
        return sub

    def challenge(self, challenger, bond=None):
        """Challenger escalates the current round.

        Increases quorum, amplifies stakes, extends settlement.  The challenger
        puts up a bond that enters the reward pool -- they profit if the estimate
        shifts (proving manipulation), lose the bond if it doesn't.
        """
        rnd = self.current_round
        if rnd is None or rnd.settled:
            raise RuntimeError("No open round to challenge")

        level = len(rnd.challenges) + 1
        if bond is None:
            bond = self.min_stake * (2.0 ** level)
        if challenger.stake < bond:
            raise ValueError(f"{challenger.name} cannot afford bond {bond}")

        ch = Challenge(challenger=challenger, bond=bond, level=level)
        rnd.challenges.append(ch)

        # Deduct bond
        challenger.stake -= bond
        rnd.reward_pool += bond

        # Increase Kalman uncertainty: "demand re-establishment of truth"
        self.kalman.P *= ch.stake_multiplier

        log.info("Challenge level %d by %s: bond=%.1f, P reset to %.6f",
                 level, challenger.name, bond, self.kalman.P)
        return ch

    def settle(self, true_price=None):
        """Settle the current round: redistribute stakes based on accuracy.

        *true_price* is the simulation ground truth.  In production, the settled
        value is the Kalman estimate itself -- there is no external ground truth.
        Returns a dict summarizing the settlement.
        """
        rnd = self.current_round
        if rnd is None or rnd.settled:
            raise RuntimeError("No open round to settle")

        settled_value = self._denormalize(self.kalman.x)
        rnd.settled_value = settled_value
        rnd.settled = True

        estimate_error = (
            abs(settled_value - true_price) / abs(true_price)
            if true_price else None
        )

        if not rnd.submissions:
            self.rounds.append(rnd)
            self.current_round = None
            return {
                "round": rnd.round_id, "settled_value": settled_value,
                "true_price": true_price, "estimate_error": estimate_error,
                "honest": 0, "dishonest": 0, "reward_pool": 0, "payouts": {},
            }

        # Classify submissions as honest or dishonest based on relative error
        # to the settled value
        honest = []
        dishonest = []
        for sub in rnd.submissions:
            rel_error = abs(sub.value - settled_value) / abs(settled_value) if settled_value else 0
            if rel_error <= self.tolerance:
                honest.append((sub, rel_error))
            else:
                dishonest.append((sub, rel_error))

        # Compute weighted shares for honest reporters.
        # Share = (1 + early_bonus) * accuracy_bonus
        # The base component (1.0) ensures late reporters still get most of their
        # stake back; the early_bonus rewards speed.
        shares = {}
        for sub, rel_error in honest:
            early_bonus = 2.0 ** (-sub.index / self.early_halflife)
            accuracy_bonus = 1.0 - (rel_error / self.tolerance) if self.tolerance > 0 else 1.0
            share = (1.0 + early_bonus) * accuracy_bonus
            shares[sub.reporter.name] = (sub, share)

        total_shares = sum(s for _, s in shares.values()) if shares else 1.0

        # Redistribute the ENTIRE reward pool weighted by shares.
        # - Honest reporters share the pool (their own stakes + dishonest stakes).
        # - Dishonest reporters get nothing back.
        # - Among honest reporters, early + accurate ones get a larger share.
        payouts = {}
        for name, (sub, share) in shares.items():
            payout = (share / total_shares) * rnd.reward_pool
            sub.reporter.stake += payout
            sub.reporter.earnings += max(0, payout - sub.stake)  # net gain
            if payout < sub.stake:
                sub.reporter.losses += sub.stake - payout         # net loss
            payouts[name] = payout

        # Dishonest reporters lose their entire stake (already deducted)
        for sub, rel_error in dishonest:
            sub.reporter.losses += sub.stake
            payouts[sub.reporter.name] = 0.0

        # Challenge settlement: challenger who submitted honestly already
        # got paid above.  Challenger who didn't submit loses their bond.
        for ch in rnd.challenges:
            if ch.challenger.name not in shares:
                ch.challenger.losses += ch.bond

        # Update reporter reputations (EMA of squared relative error)
        for sub in rnd.submissions:
            rel_error = abs(sub.value - settled_value) / abs(settled_value) if settled_value else 0
            sq_err = rel_error ** 2
            r = sub.reporter
            alpha = self.ema_alpha if r.n_settled >= 3 else 0.5  # faster warmup
            r.ema_sq_error = alpha * sq_err + (1.0 - alpha) * r.ema_sq_error
            r.n_settled += 1

        self.rounds.append(rnd)
        self.current_round = None

        result = {
            "round": rnd.round_id,
            "settled_value": settled_value,
            "true_price": true_price,
            "estimate_error": estimate_error,
            "honest": len(honest),
            "dishonest": len(dishonest),
            "reward_pool": rnd.reward_pool,
            "payouts": payouts,
        }
        log.info("Round %d settled: est=%.2f true=%.2f err=%.4f%% honest=%d dishonest=%d pool=%.1f",
                 rnd.round_id, settled_value, true_price or 0,
                 (estimate_error or 0) * 100,
                 len(honest), len(dishonest), rnd.reward_pool)
        return result

    def _stake_multiplier(self):
        """Current effective stake multiplier (increases with active challenges)."""
        if self.current_round is None:
            return 1.0
        mult = 1.0
        for ch in self.current_round.challenges:
            mult *= ch.stake_multiplier
        return mult


# ---------------------------------------------------------------------------
# Simulation helpers
# ---------------------------------------------------------------------------

def run_simulation(
    oracle,
    reporters,
    n_rounds=20,
    true_price_fn=None,
    initial_price=2900.0,
    price_drift=0.002,
    strategies=None,
    challenge_fn=None,
    seed=42,
):
    """Drive an oracle through *n_rounds* of reporting and settlement.

    Args:
        oracle:         Oracle instance (already configured)
        reporters:      list of (Reporter, noise_std, bias) tuples
        n_rounds:       number of rounds to simulate
        true_price_fn:  callable(round_id) -> float, overrides random walk
        initial_price:  starting price (if no true_price_fn)
        price_drift:    std of random walk per round
        strategies:     optional dict mapping reporter name to callable(round_id)
                        returning (noise_std, bias) -- for dynamic strategy changes
        challenge_fn:   callable(oracle, round_id, submissions) -> Reporter or None
                        if it returns a Reporter, that reporter challenges
        seed:           random seed for reproducibility

    Returns:
        list of settlement result dicts, one per round
    """
    rng = random.Random(seed)
    _orig_gauss = random.gauss
    random.gauss = rng.gauss

    try:
        price = initial_price
        results = []

        for rid in range(n_rounds):
            if true_price_fn is not None:
                price = true_price_fn(rid)
            else:
                price *= (1.0 + rng.gauss(0, price_drift))

            oracle.open_round()

            # Shuffle submission order each round
            order = list(reporters)
            rng.shuffle(order)

            for reporter, noise_std, bias in order:
                if strategies and reporter.name in strategies:
                    noise_std, bias = strategies[reporter.name](rid)
                value = reporter.observe(price, noise_std=noise_std, bias=bias)
                try:
                    oracle.submit(reporter, value, stake=oracle.min_stake)
                except ValueError as e:
                    log.warning("Round %d: %s cannot submit: %s", rid, reporter.name, e)

            # Challenge logic
            if challenge_fn is not None:
                challenger = challenge_fn(oracle, rid, oracle.current_round.submissions)
                if challenger is not None:
                    try:
                        oracle.challenge(challenger)
                        # After challenge, let honest reporters re-submit
                        for reporter, noise_std, bias_val in reporters:
                            if bias_val == 0.0 and reporter.name != challenger.name:
                                value = reporter.observe(price, noise_std=noise_std, bias=0.0)
                                try:
                                    oracle.submit(reporter, value, stake=oracle.min_stake)
                                except ValueError:
                                    pass
                    except ValueError as e:
                        log.warning("Round %d: challenge failed: %s", rid, e)

            result = oracle.settle(true_price=price)
            results.append(result)

        return results

    finally:
        random.gauss = _orig_gauss
