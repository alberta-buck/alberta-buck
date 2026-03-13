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
from collections import OrderedDict
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
        # Consumer challenges (challenger=None) have no reporter to penalize.
        for ch in rnd.challenges:
            if ch.challenger is not None and ch.challenger.name not in shares:
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
# EscrowOracle: double-down + retroactive escrow for non-ergodic dynamics
# ---------------------------------------------------------------------------

@dataclass
class EscrowEntry:
    """Per-round escrow record: holds back a fraction of payouts for W rounds."""
    round_id: int
    true_price: float
    settled_value: float
    holdings: dict = field(default_factory=dict)  # name -> {amount, submission_value}
    released: bool = False


class EscrowOracle(Oracle):
    """Oracle with double-down and retroactive escrow.

    Extends Oracle with two mechanisms that create non-ergodic dynamics:

    1. Double-down: on challenge, all current-round submitters must double their
       stake or withdraw.  Honest reporters double without hesitation; liars face
       compounding exposure.

    2. Retroactive escrow: a fraction of each payout is held for W rounds.  When
       a challenge triggers, escrowed payouts from the past W rounds are
       re-evaluated against ground truth.  Liars who were falsely classified as
       "honest" (because they controlled the settled value) have their escrow
       confiscated.

    The combination creates a "honey pot": the longer liars maintain control, the
    more escrow they accumulate, the larger the windfall when truth prevails.
    This makes sustained manipulation non-ergodic -- eventually the accumulated
    pot attracts enough honest participation to overwhelm the liars.
    """

    def __init__(
        self,
        *args,
        escrow_fraction=0.20,
        escrow_window=10,
        doubledown_fn=None,
        challenge_P_floor=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.escrow_fraction = escrow_fraction
        self.escrow_window = escrow_window
        self.doubledown_fn = doubledown_fn  # callable(reporter, stake, round_id) -> bool
        # On challenge, reset P to at least this value.  When None, uses the
        # standard 2x doubling.  Setting this to initial_P forces the filter
        # to re-establish trust from scratch after a challenge -- breaking the
        # reputation circular dependency where entrenched liars have R << newcomers.
        self.challenge_P_floor = challenge_P_floor
        self.escrow_ledger = []             # list of EscrowEntry
        self.total_escrowed = 0.0

    @property
    def honey_pot(self):
        """Total unreleased escrow -- the non-ergodic trap for sustained liars."""
        return self.total_escrowed

    def challenge(self, challenger, bond=None):
        """Override: standard challenge + progressive P escalation + double-down.

        Each successive challenge opens the Kalman filter wider:
        - Level 1: P floor = challenge_P_floor
        - Level 2: P floor = challenge_P_floor * 2
        - Level N: P floor = challenge_P_floor * 2^(N-1)

        This progressive escalation means persistent challenges force the filter
        to increasingly distrust the current estimate.  Newcomers gain more
        influence with each challenge level, and the settled value reflects
        greater uncertainty about past consensus.
        """
        ch = super().challenge(challenger, bond=bond)

        # Progressive P floor: escalates with each challenge level.
        # The base Oracle already does P *= 2^level, but from a potentially
        # tiny base (e.g., P=0.00006 * 2 = 0.00012 -- still negligible).
        # The floor ensures P reaches at least challenge_P_floor * 2^(level-1),
        # so each successive challenge forces the filter progressively wider.
        if self.challenge_P_floor is not None:
            escalated_floor = self.challenge_P_floor * (2.0 ** (ch.level - 1))
            self.kalman.P = max(self.kalman.P, escalated_floor)

        if self.doubledown_fn is None:
            return ch

        rnd = self.current_round
        surviving = []
        for sub in rnd.submissions:
            # Challenger is exempt from doubledown
            if sub.reporter.name == challenger.name:
                surviving.append(sub)
                continue

            doubles_down = self.doubledown_fn(sub.reporter, sub.stake, rnd.round_id)
            if doubles_down:
                additional = sub.stake
                if sub.reporter.stake < additional:
                    # Cannot afford -- forced withdrawal
                    sub.reporter.stake += sub.stake
                    rnd.reward_pool -= sub.stake
                    log.info("Double-down: %s forced withdrawal (insufficient funds)",
                             sub.reporter.name)
                else:
                    sub.reporter.stake -= additional
                    rnd.reward_pool += additional
                    sub.stake += additional  # now 2x
                    surviving.append(sub)
                    log.debug("Double-down: %s doubled to %.1f", sub.reporter.name, sub.stake)
            else:
                # Voluntary withdrawal: stake returned, submission removed
                sub.reporter.stake += sub.stake
                rnd.reward_pool -= sub.stake
                log.info("Double-down: %s withdrew", sub.reporter.name)

        rnd.submissions = surviving
        return ch

    def settle(self, true_price=None):
        """Override: standard settlement + escrow withholding + retroactive confiscation."""
        rnd = self.current_round
        was_challenged = bool(rnd.challenges) if rnd else False

        result = super().settle(true_price=true_price)

        # The round is now in self.rounds[-1]; rnd still references it
        if self.escrow_fraction <= 0:
            result.update(escrow_withheld=0.0, escrow_confiscated=0.0,
                          escrow_released=0.0, total_escrowed=self.total_escrowed)
            return result

        # --- Escrow withholding: hold back fraction of each payout ---
        holdings = {}
        for sub in rnd.submissions:
            name = sub.reporter.name
            payout = result["payouts"].get(name, 0)
            if payout <= 0:
                continue
            withheld = payout * self.escrow_fraction
            reporter = self.reporters[name]
            reporter.stake -= withheld  # claw back from just-credited balance
            holdings[name] = {"amount": withheld, "submission_value": sub.value}
            self.total_escrowed += withheld

        self.escrow_ledger.append(EscrowEntry(
            round_id=rnd.round_id,
            true_price=true_price,
            settled_value=rnd.settled_value,
            holdings=holdings,
        ))

        # --- Retroactive confiscation (only on challenged rounds) ---
        confiscated = 0.0
        if was_challenged and true_price is not None:
            confiscated = self._retroactive_confiscation()

        # --- Release aged escrow ---
        released = self._release_aged_escrow()

        result["escrow_withheld"] = sum(h["amount"] for h in holdings.values())
        result["escrow_confiscated"] = confiscated
        result["escrow_released"] = released
        result["total_escrowed"] = self.total_escrowed

        return result

    def _retroactive_confiscation(self):
        """Re-evaluate unreleased escrow using stored ground truth.

        Compares each escrowed submission against the *true price* at the time
        of that submission.  Submissions that were actually inaccurate (but
        classified "honest" because liars controlled the settled value) have
        their escrow confiscated and redistributed to the current round's
        honest reporters.
        """
        confiscated = 0.0
        for entry in self.escrow_ledger:
            if entry.released or not entry.holdings:
                continue
            stored_true = entry.true_price
            if stored_true is None or stored_true == 0:
                continue
            to_remove = []
            for name, esc in entry.holdings.items():
                sub_val = esc["submission_value"]
                if sub_val is None:
                    continue
                rel_error = abs(sub_val - stored_true) / abs(stored_true)
                if rel_error > self.tolerance:
                    confiscated += esc["amount"]
                    self.total_escrowed -= esc["amount"]
                    reporter = self.reporters.get(name)
                    if reporter:
                        reporter.losses += esc["amount"]
                    to_remove.append(name)
            for name in to_remove:
                del entry.holdings[name]

        if confiscated > 0:
            self._distribute_confiscation(confiscated)

        return confiscated

    def _distribute_confiscation(self, amount):
        """Redistribute confiscated escrow to current round's honest reporters."""
        latest = self.rounds[-1]
        settled_value = latest.settled_value
        honest_subs = []
        for sub in latest.submissions:
            if settled_value and settled_value != 0:
                rel_error = abs(sub.value - settled_value) / abs(settled_value)
            else:
                rel_error = 0
            if rel_error <= self.tolerance:
                early_bonus = 2.0 ** (-sub.index / self.early_halflife)
                accuracy_bonus = 1.0 - (rel_error / self.tolerance) if self.tolerance > 0 else 1.0
                share = (1.0 + early_bonus) * accuracy_bonus
                honest_subs.append((sub, share))

        if not honest_subs:
            return

        total_shares = sum(s for _, s in honest_subs)
        for sub, share in honest_subs:
            payout = (share / total_shares) * amount
            sub.reporter.stake += payout
            sub.reporter.earnings += payout

    def _release_aged_escrow(self):
        """Return escrow entries that have aged past the escrow window."""
        current_rid = len(self.rounds) - 1
        released = 0.0
        for entry in self.escrow_ledger:
            if entry.released:
                continue
            if current_rid - entry.round_id >= self.escrow_window:
                for name, esc in entry.holdings.items():
                    reporter = self.reporters.get(name)
                    if reporter:
                        reporter.stake += esc["amount"]
                        released += esc["amount"]
                        self.total_escrowed -= esc["amount"]
                entry.holdings = {}
                entry.released = True
        return released


# ---------------------------------------------------------------------------
# FeedOracle: consumer-funded oracle with paid feed access
# ---------------------------------------------------------------------------

class FeedOracle(EscrowOracle):
    """Oracle where consumers pay to read the price feed.

    Extends EscrowOracle with a demand-side revenue model: consumers (smart
    contracts, DeFi protocols, anyone who reads the oracle) pay a per-read fee
    that flows into the next round's reward pool.  This creates a positive
    externality: more demand for accurate prices -> larger reward pool -> more
    reporters attracted -> more accurate prices -> more demand.

    Consumers can also pay for *consumer challenges*: when a consumer doubts
    the current estimate (e.g., it diverges from their own reference), they can
    post a consumer challenge bond that triggers the same escalation as a
    reporter challenge.  This lets the demand side directly fund oracle
    integrity during uncertain periods.

    Revenue model:
      - Base feed fee: paid per read, flows to next round's pool
      - Consumer challenge: escalation bond from a non-reporter, added to pool
      - Uncertainty premium: optional multiplier on feed fee when Kalman P
        exceeds a threshold (consumers pay more when the oracle is uncertain,
        funding the increased reporting effort needed to resolve uncertainty)
    """

    def __init__(
        self,
        *args,
        feed_fee=1.0,
        uncertainty_premium_threshold=0.005,
        uncertainty_premium_multiplier=3.0,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.feed_fee = feed_fee
        self.uncertainty_premium_threshold = uncertainty_premium_threshold
        self.uncertainty_premium_multiplier = uncertainty_premium_multiplier
        self.accumulated_fees = 0.0       # fees waiting to enter next round's pool
        self.total_fees_collected = 0.0   # lifetime fee revenue
        self.n_reads = 0                  # lifetime read count

    def read_price(self, n_reads=1):
        """Consumer reads the current oracle estimate, paying a fee per read.

        The fee is adjusted by an uncertainty premium when Kalman P exceeds
        the threshold -- consumers pay more for uncertain data, which funds
        the additional reporting effort needed to reduce uncertainty.

        Returns (estimate, fee_paid).
        """
        P = self.kalman.P
        if P > self.uncertainty_premium_threshold:
            effective_fee = self.feed_fee * self.uncertainty_premium_multiplier
        else:
            effective_fee = self.feed_fee

        total_fee = effective_fee * n_reads
        self.accumulated_fees += total_fee
        self.total_fees_collected += total_fee
        self.n_reads += n_reads

        estimate = self._denormalize(self.kalman.x)
        return estimate, total_fee

    def consumer_challenge(self, bond):
        """A consumer (non-reporter) posts a challenge bond.

        Unlike a reporter challenge, the consumer doesn't submit a value --
        they just put money on the line to demand better accuracy.  The bond
        enters the reward pool and triggers escalation (doubles P).

        Returns the Challenge object.
        """
        rnd = self.current_round
        if rnd is None or rnd.settled:
            raise RuntimeError("No open round to challenge")

        level = len(rnd.challenges) + 1
        ch = Challenge(challenger=None, bond=bond, level=level)
        rnd.challenges.append(ch)
        rnd.reward_pool += bond

        # Increase Kalman uncertainty
        self.kalman.P *= ch.stake_multiplier

        log.info("Consumer challenge level %d: bond=%.1f, P reset to %.6f",
                 level, bond, self.kalman.P)
        return ch

    def open_round(self):
        """Override: flush accumulated feed fees into the new round's pool."""
        rnd = super().open_round()
        if self.accumulated_fees > 0:
            rnd.reward_pool += self.accumulated_fees
            log.debug("Flushed %.1f in feed fees to round %d pool",
                      self.accumulated_fees, rnd.round_id)
            self.accumulated_fees = 0.0
        return rnd


# ---------------------------------------------------------------------------
# TimeSeriesOracle: V2 time-indexed oracle with RTS smoother
# ---------------------------------------------------------------------------

@dataclass
class TimeSlot:
    """A single slot in the time-series oracle, indexed by block number.

    Each slot represents one resolution-interval of the oracle (e.g., 300
    blocks ~= 1 hour on post-merge Ethereum at 12s/block).  The slot key is
    the *first* block number of the interval; all blocks in [key, key+resolution)
    map to this slot.

    On-chain, only the most recent buffer_size slots are stored.  Older finalized
    slots are evicted but remain queryable via archive-node historical state
    (eth_getStorageAt with a past block number).

    Phases:
      1. Open: reporters submit observations within the submission window.
      2. Challenge: anyone can challenge the slot's estimate.
      3. Finalized: challenge window expired; estimate is frozen, reporters are paid.

    The Kalman forward pass stores (x_pred, P_pred, estimate, P) per slot.
    The RTS backward pass stores (x_smooth, P_smooth) per slot.
    """
    slot_key: int                                      # block number (quantized)
    estimate: float = None                             # forward-pass estimate (norm)
    P: float = None                                    # forward-pass uncertainty
    x_pred: float = None                               # predicted x before slot updates
    P_pred: float = None                               # predicted P before slot updates
    x_smooth: float = None                             # RTS-smoothed estimate (norm)
    P_smooth: float = None                             # RTS-smoothed uncertainty
    submissions: list = field(default_factory=list)     # Submission objects
    challenges: list = field(default_factory=list)      # Challenge objects
    finalized: bool = False
    finalized_at: int = None                            # block number of finalization
    settled: bool = False
    settled_value: float = None                         # denormalized finalized estimate
    reward_pool: float = 0.0
    escrow_holdings: dict = field(default_factory=dict)  # name -> {amount, value}
    reopened: bool = False                              # True if reopened by challenge
    P_floor: float = 0.0                                # minimum P from challenges


class TimeSeriesOracle:
    """V2 time-series oracle with deferred settlement and RTS smoother.

    Manages a time-indexed curve x(block) for a tracked variable.  Slots are
    keyed by Ethereum block number (quantized to resolution boundaries), so the
    on-chain time reference is the block number itself.  Each block carries a
    timestamp, but the block number is the canonical index.

    Reporters submit observations targeting specific (asset, block) slots.
    Slots progress through submission -> challenge -> finalization phases.
    The RTS smoother propagates corrections both forward and backward.

    Only the most recent *buffer_size* slots are stored on-chain (a circular
    buffer).  Older finalized slots are evicted; their state remains queryable
    via archive-node historical state (eth_getStorageAt at a past block).
    The forward pass resumes from a stored anchor state rather than replaying
    the full history.

    Settlement is deferred: reporters are paid against the *finalized* smoothed
    estimate, not the instantaneous forward estimate at submission time.  This
    eliminates the circular dependency where liars could control the reference
    value they are judged against.

    Usage (simulation)::

        oracle = TimeSeriesOracle(initial_estimate=2900.0, resolution=300)
        alice = Reporter("alice", stake=100)
        oracle.register(alice)
        oracle.submit(alice, block=0, value=2900, stake=5, now=1)
        oracle.smooth()
        finalized = oracle.finalize(now=600)

    Parameters:
        resolution:       blocks per slot (e.g., 300 blocks ~= 1 hour)
        submission_window: blocks after slot start for submissions
        challenge_window:  blocks after submission window for challenges
        buffer_size:       max slots in the on-chain circular buffer (None = unbounded)
    """

    def __init__(
        self,
        initial_estimate=0.0,
        initial_P=0.01,
        Q=0.0001,
        resolution=300,
        submission_window=150,
        challenge_window=300,
        buffer_size=None,
        min_stake=DEFAULT_MIN_STAKE,
        tolerance=DEFAULT_TOLERANCE,
        early_halflife=DEFAULT_EARLY_HALFLIFE,
        ema_alpha=DEFAULT_EMA_ALPHA,
        gate_sigma=DEFAULT_GATE_SIGMA,
        base_challenge_bond=2.0,
        challenge_P_floor=None,
        escrow_fraction=0.20,
        escrow_window=10,
        feed_fee=1.0,
        uncertainty_premium_threshold=0.005,
        uncertainty_premium_multiplier=3.0,
    ):
        self.scale = abs(initial_estimate) if initial_estimate != 0 else 1.0
        self.initial_x = 1.0
        self.initial_P = initial_P
        self.Q = Q
        self.resolution = resolution
        self.submission_window = submission_window
        self.challenge_window = challenge_window
        self.buffer_size = buffer_size
        self.min_stake = min_stake
        self.tolerance = tolerance
        self.early_halflife = early_halflife
        self.ema_alpha = ema_alpha
        self.gate_sigma = gate_sigma
        self.base_challenge_bond = base_challenge_bond
        self.challenge_P_floor = challenge_P_floor
        self.escrow_fraction = escrow_fraction
        self.escrow_window = escrow_window
        self.feed_fee = feed_fee
        self.uncertainty_premium_threshold = uncertainty_premium_threshold
        self.uncertainty_premium_multiplier = uncertainty_premium_multiplier

        self.slots = OrderedDict()       # slot_key (block number) -> TimeSlot
        self.reporters = {}              # name -> Reporter
        self.total_escrowed = 0.0
        self.total_fees_collected = 0.0
        self.n_reads = 0

        # Anchor state: Kalman (x, P) at the boundary of evicted history.
        # The forward pass starts from here instead of replaying all history.
        # On-chain, the anchor is written when old slots are evicted.
        self.anchor_x = self.initial_x
        self.anchor_P = self.initial_P
        self.anchor_key = None           # block number of last evicted slot

    # -- Normalization -------------------------------------------------------

    def _normalize(self, value):
        return value / self.scale

    def _denormalize(self, normalized):
        return normalized * self.scale

    # -- Slot management -----------------------------------------------------

    def _quantize(self, block):
        """Round block number down to the nearest slot boundary."""
        return block - (block % self.resolution)

    def _get_or_create_slot(self, block):
        """Lazy slot creation.  Inserts in block-number order."""
        key = self._quantize(block)
        if key not in self.slots:
            self.slots[key] = TimeSlot(slot_key=key)
            # Maintain insertion order by block number
            self.slots = OrderedDict(sorted(self.slots.items()))
        return self.slots[key]

    def _sorted_slots(self):
        """Return all buffered slots sorted by block number."""
        return list(self.slots.values())

    # -- Registration -------------------------------------------------------

    def register(self, reporter):
        self.reporters[reporter.name] = reporter

    # -- Submission ----------------------------------------------------------

    def _slot_submission_deadline(self, slot):
        """Last block to submit to this slot (may be extended by challenges)."""
        deadline = slot.slot_key + self.submission_window
        if slot.reopened:
            for ch in slot.challenges:
                deadline += ch.window_extension * self.resolution
        return deadline

    def _slot_challenge_deadline(self, slot):
        """Last block to challenge this slot."""
        deadline = self._slot_submission_deadline(slot) + self.challenge_window
        for ch in slot.challenges:
            deadline += ch.window_extension * self.resolution
        return deadline

    def submit(self, reporter, block, value, stake=None, now=None):
        """Reporter submits an observation for a specific time slot.

        Args:
            reporter:   Reporter instance
            block:      target block number (quantized to slot boundary)
            value:      observed value (non-normalized)
            stake:      amount to stake (defaults to min_stake)
            now:        current block number (defaults to block + 1)

        Returns:
            Submission or None (if gated)
        """
        if now is None:
            now = block + 1

        slot = self._get_or_create_slot(block)

        if slot.finalized:
            raise RuntimeError(f"Slot {slot.slot_key} is finalized")

        # Validate within submission window
        deadline = self._slot_submission_deadline(slot)
        if now > deadline:
            raise RuntimeError(
                f"Submission window closed for slot {slot.slot_key} "
                f"(deadline {deadline}, now {now})"
            )

        if stake is None:
            stake = self.min_stake
        # Challenge multiplier for this slot
        mult = 1.0
        for ch in slot.challenges:
            mult *= ch.stake_multiplier
        effective_min = self.min_stake * mult
        if stake < effective_min:
            raise ValueError(f"Stake {stake} below minimum {effective_min}")
        if reporter.stake < stake:
            raise ValueError(
                f"{reporter.name} has insufficient balance "
                f"({reporter.stake} < {stake})"
            )

        # Gating: reject extreme outliers if we have an estimate
        if slot.estimate is not None and slot.P is not None and slot.P > 0:
            z = self._normalize(value)
            sigma = math.sqrt(slot.P)
            if sigma > 0 and abs(z - slot.estimate) > self.gate_sigma * sigma:
                log.debug("Gated submission from %s for slot %d",
                          reporter.name, slot.slot_key)
                return None

        idx = len(slot.submissions)
        sub = Submission(reporter=reporter, value=value, stake=stake, index=idx)
        slot.submissions.append(sub)

        reporter.stake -= stake
        slot.reward_pool += stake

        log.debug("Slot %d sub %d: %s=%.2f stake=%.1f",
                  slot.slot_key, idx, reporter.name, value, stake)
        return sub

    # -- Challenge -----------------------------------------------------------

    def challenge(self, challenger, block, bond=None, now=None):
        """Challenge a specific time slot.

        Bond is age-scaled: base_bond * 2^level * (1 + slot_age_in_slots).
        On success, the challenger earns confiscated escrow from affected slots.
        """
        slot = self._get_or_create_slot(block)

        if now is None:
            now = block + self.submission_window + 1

        if slot.finalized:
            raise RuntimeError(f"Slot {slot.slot_key} is already finalized")

        # Age scaling: older slots cost more to challenge
        sorted_keys = sorted(self.slots.keys())
        latest_key = sorted_keys[-1] if sorted_keys else slot.slot_key
        slot_age = max(0, (latest_key - slot.slot_key)) // self.resolution

        level = len(slot.challenges) + 1
        min_bond = self.base_challenge_bond * (2.0 ** level) * (1 + slot_age)

        if bond is None:
            bond = min_bond
        if bond < min_bond:
            raise ValueError(
                f"Bond {bond} below age-scaled minimum {min_bond} "
                f"(level={level}, age={slot_age} slots)"
            )
        if challenger.stake < bond:
            raise ValueError(
                f"{challenger.name} cannot afford bond {bond} "
                f"(balance {challenger.stake})"
            )

        ch = Challenge(challenger=challenger, bond=bond, level=level)
        slot.challenges.append(ch)

        challenger.stake -= bond
        slot.reward_pool += bond

        # Progressive P floor: store on the slot so forward_pass respects it
        if self.challenge_P_floor is not None:
            escalated_floor = self.challenge_P_floor * (2.0 ** (ch.level - 1))
            slot.P_floor = max(slot.P_floor, escalated_floor)
            if slot.P is not None:
                slot.P = max(slot.P, escalated_floor)
            else:
                slot.P = escalated_floor

        # Reopen slot for new submissions
        slot.reopened = True

        log.info("Challenge slot %d level %d by %s: bond=%.1f age=%d slots",
                 slot.slot_key, level, challenger.name, bond, slot_age)
        return ch

    # -- Kalman smoother -----------------------------------------------------

    def forward_pass(self, from_key=None):
        """Run the Kalman filter forward over non-finalized slots.

        Starts from the anchor state (last evicted slot's estimate) or the
        earliest buffered slot.  Processes all submissions for each slot
        sequentially (by index), storing (x_pred, P_pred, estimate, P).
        """
        sorted_slots = self._sorted_slots()
        if not sorted_slots:
            return

        # Find starting point
        start_idx = 0
        if from_key is not None:
            for i, slot in enumerate(sorted_slots):
                if slot.slot_key >= from_key:
                    start_idx = i
                    break

        # Initialize from previous slot, anchor, or initial state
        if start_idx > 0:
            prev = sorted_slots[start_idx - 1]
            x_prev = prev.x_smooth if prev.x_smooth is not None else prev.estimate
            P_prev = prev.P_smooth if prev.P_smooth is not None else prev.P
            if x_prev is None:
                x_prev = self.anchor_x
            if P_prev is None:
                P_prev = self.anchor_P
            prev_key = prev.slot_key
        elif self.anchor_key is not None:
            # Resume from anchor (evicted slot boundary)
            x_prev = self.anchor_x
            P_prev = self.anchor_P
            prev_key = self.anchor_key
        else:
            x_prev = self.initial_x
            P_prev = self.initial_P
            prev_key = sorted_slots[0].slot_key - self.resolution

        for slot in sorted_slots[start_idx:]:
            if slot.finalized:
                # Use finalized estimate as basis for next slot
                x_prev = self._normalize(slot.settled_value) if slot.settled_value else slot.x_smooth or slot.estimate or x_prev
                P_prev = slot.P_smooth if slot.P_smooth is not None else slot.P if slot.P is not None else P_prev
                prev_key = slot.slot_key
                continue

            # Gap in blocks: uncertainty grows proportionally
            gap = max(1, (slot.slot_key - prev_key) // self.resolution)
            x_pred = x_prev
            P_pred = P_prev + self.Q * gap

            slot.x_pred = x_pred
            slot.P_pred = P_pred

            # Process submissions sequentially
            x = x_pred
            P = P_pred
            for sub in sorted(slot.submissions, key=lambda s: s.index):
                z = self._normalize(sub.value)
                R = sub.reporter.R
                K = P / (P + R)
                x = x + K * (z - x)
                P = P * (1.0 - K)

            # Apply challenge P floor: ensures challenged slots retain
            # uncertainty despite confident (potentially dishonest) reporters
            if slot.P_floor > 0:
                P = max(P, slot.P_floor)

            slot.estimate = x
            slot.P = P

            x_prev = x
            P_prev = P
            prev_key = slot.slot_key

    def backward_pass(self):
        """RTS smoother backward pass over non-finalized slots.

        Propagates future evidence backward, giving the maximum-likelihood
        estimate at every timestamp given *all* observations.
        """
        sorted_slots = self._sorted_slots()
        non_finalized = [s for s in sorted_slots if not s.finalized
                         and s.estimate is not None]
        if not non_finalized:
            return

        # Last slot: smoothed = forward estimate
        last = non_finalized[-1]
        last.x_smooth = last.estimate
        last.P_smooth = last.P

        # Backward pass
        for i in range(len(non_finalized) - 2, -1, -1):
            slot = non_finalized[i]
            next_slot = non_finalized[i + 1]

            if next_slot.P_pred is None or next_slot.P_pred == 0:
                slot.x_smooth = slot.estimate
                slot.P_smooth = slot.P
                continue

            G = slot.P / next_slot.P_pred
            slot.x_smooth = slot.estimate + G * (next_slot.x_smooth - next_slot.x_pred)
            slot.P_smooth = slot.P + G * (next_slot.P_smooth - next_slot.P_pred) * G

    def smooth(self, from_key=None):
        """Run forward + backward passes (convenience wrapper)."""
        self.forward_pass(from_key=from_key)
        self.backward_pass()

    # -- Finalization and settlement -----------------------------------------

    def finalize(self, now):
        """Finalize all slots whose challenge windows have expired by *now*.

        After settlement, evicts oldest finalized slots that exceed buffer_size,
        updating the anchor state for future forward passes.

        Returns list of newly finalized TimeSlot objects.
        """
        newly_finalized = []
        for slot in self._sorted_slots():
            if slot.finalized or slot.settled:
                continue
            if slot.estimate is None:
                continue  # no submissions yet

            deadline = self._slot_challenge_deadline(slot)
            if now >= deadline:
                slot.finalized = True
                slot.finalized_at = now
                est = slot.x_smooth if slot.x_smooth is not None else slot.estimate
                slot.settled_value = self._denormalize(est)
                newly_finalized.append(slot)

        for slot in newly_finalized:
            self._settle_slot(slot)

        # Evict oldest finalized slots beyond buffer_size
        if self.buffer_size is not None:
            self._evict_oldest()

        return newly_finalized

    def _evict_oldest(self):
        """Remove oldest finalized slots that exceed buffer_size.

        Saves the evicted slot's smoothed state as the anchor for future
        forward passes.  On-chain, the evicted state remains queryable via
        archive-node eth_getStorageAt at the finalization block.
        """
        while len(self.slots) > self.buffer_size:
            # Pop the oldest (first) slot
            key, slot = next(iter(self.slots.items()))
            if not slot.finalized:
                break  # never evict non-finalized slots
            # Save anchor state from the evicted slot
            self.anchor_x = (
                slot.x_smooth if slot.x_smooth is not None
                else self._normalize(slot.settled_value) if slot.settled_value
                else slot.estimate if slot.estimate is not None
                else self.anchor_x
            )
            self.anchor_P = (
                slot.P_smooth if slot.P_smooth is not None
                else slot.P if slot.P is not None
                else self.anchor_P
            )
            self.anchor_key = slot.slot_key
            del self.slots[key]

    def _settle_slot(self, slot):
        """Redistribute stakes for a finalized slot.

        Mirrors Oracle.settle() logic but operates on a TimeSlot and uses
        the finalized smoothed estimate as the accuracy reference.
        """
        slot.settled = True
        settled_value = slot.settled_value

        if not slot.submissions:
            return {
                "slot_key": slot.slot_key, "settled_value": settled_value,
                "honest": 0, "dishonest": 0, "reward_pool": 0, "payouts": {},
            }

        # Classify honest vs dishonest against *finalized* estimate
        honest = []
        dishonest = []
        for sub in slot.submissions:
            if settled_value and settled_value != 0:
                rel_error = abs(sub.value - settled_value) / abs(settled_value)
            else:
                rel_error = 0
            if rel_error <= self.tolerance:
                honest.append((sub, rel_error))
            else:
                dishonest.append((sub, rel_error))

        # Weighted shares: early bonus * accuracy bonus
        shares = {}
        for sub, rel_error in honest:
            early_bonus = 2.0 ** (-sub.index / self.early_halflife)
            accuracy_bonus = (
                1.0 - (rel_error / self.tolerance) if self.tolerance > 0 else 1.0
            )
            share = (1.0 + early_bonus) * accuracy_bonus
            shares[sub.reporter.name] = (sub, share)

        total_shares = sum(s for _, s in shares.values()) if shares else 1.0

        # Redistribute reward pool
        payouts = {}
        for name, (sub, share) in shares.items():
            payout = (share / total_shares) * slot.reward_pool
            sub.reporter.stake += payout
            sub.reporter.earnings += max(0, payout - sub.stake)
            if payout < sub.stake:
                sub.reporter.losses += sub.stake - payout
            payouts[name] = payout

        for sub, rel_error in dishonest:
            sub.reporter.losses += sub.stake
            payouts[sub.reporter.name] = 0.0

        # Challenge settlement
        for ch in slot.challenges:
            if ch.challenger is not None and ch.challenger.name not in shares:
                ch.challenger.losses += ch.bond

        # Update reputations
        for sub in slot.submissions:
            if settled_value and settled_value != 0:
                rel_error = abs(sub.value - settled_value) / abs(settled_value)
            else:
                rel_error = 0
            sq_err = rel_error ** 2
            r = sub.reporter
            alpha = self.ema_alpha if r.n_settled >= 3 else 0.5
            r.ema_sq_error = alpha * sq_err + (1.0 - alpha) * r.ema_sq_error
            r.n_settled += 1

        # Escrow withholding
        escrow_withheld = 0.0
        if self.escrow_fraction > 0:
            for sub in slot.submissions:
                name = sub.reporter.name
                payout = payouts.get(name, 0)
                if payout <= 0:
                    continue
                withheld = payout * self.escrow_fraction
                reporter = self.reporters[name]
                reporter.stake -= withheld
                slot.escrow_holdings[name] = {
                    "amount": withheld, "value": sub.value,
                }
                self.total_escrowed += withheld
                escrow_withheld += withheld

        # Release aged escrow
        released = self._release_aged_escrow(slot)

        result = {
            "slot_key": slot.slot_key,
            "settled_value": settled_value,
            "honest": len(honest),
            "dishonest": len(dishonest),
            "reward_pool": slot.reward_pool,
            "payouts": payouts,
            "escrow_withheld": escrow_withheld,
            "escrow_released": released,
            "total_escrowed": self.total_escrowed,
        }
        log.info("Slot %d settled: est=%.2f honest=%d dishonest=%d pool=%.1f",
                 slot.slot_key, settled_value, len(honest), len(dishonest),
                 slot.reward_pool)
        return result

    def _release_aged_escrow(self, current_slot):
        """Release escrow from slots that have aged past escrow_window."""
        released = 0.0
        current_key = current_slot.slot_key
        for slot in self._sorted_slots():
            if not slot.settled or not slot.escrow_holdings:
                continue
            age_in_slots = (current_key - slot.slot_key) // self.resolution
            if age_in_slots >= self.escrow_window:
                for name, esc in list(slot.escrow_holdings.items()):
                    reporter = self.reporters.get(name)
                    if reporter:
                        reporter.stake += esc["amount"]
                        released += esc["amount"]
                        self.total_escrowed -= esc["amount"]
                slot.escrow_holdings = {}
        return released

    def retroactive_confiscation(self, from_key, true_price_fn=None):
        """Re-evaluate escrow in slots from from_key onward.

        After a challenge triggers re-smoothing and changes settled values,
        submissions that are now dishonest (vs the new smoothed estimate) have
        their escrow confiscated.

        Args:
            from_key:        earliest slot_key (block number) to re-evaluate
            true_price_fn:   callable(slot_key) -> true_price (for simulation)

        Returns:
            total amount confiscated
        """
        confiscated = 0.0
        for slot in self._sorted_slots():
            if slot.slot_key < from_key:
                continue
            if not slot.escrow_holdings:
                continue

            # Reference value: use true price if available, else smoothed
            if true_price_fn is not None:
                ref = true_price_fn(slot.slot_key)
            elif slot.x_smooth is not None:
                ref = self._denormalize(slot.x_smooth)
            elif slot.settled_value is not None:
                ref = slot.settled_value
            else:
                continue

            to_remove = []
            for name, esc in slot.escrow_holdings.items():
                sub_val = esc["value"]
                if ref and ref != 0:
                    rel_error = abs(sub_val - ref) / abs(ref)
                else:
                    rel_error = 0
                if rel_error > self.tolerance:
                    confiscated += esc["amount"]
                    self.total_escrowed -= esc["amount"]
                    reporter = self.reporters.get(name)
                    if reporter:
                        reporter.losses += esc["amount"]
                    to_remove.append(name)
            for name in to_remove:
                del slot.escrow_holdings[name]

        return confiscated

    # -- Consumer reads ------------------------------------------------------

    def read_price(self, block, now=None, n_reads=1):
        """Consumer reads the estimate at a specific slot.

        Fee is scaled by uncertainty at that slot: higher P -> higher fee.

        Returns (estimate, fee_paid).
        """
        key = self._quantize(block)
        slot = self.slots.get(key)

        if slot is None:
            # No data for this slot; return initial estimate
            est = self._denormalize(self.initial_x)
            P = self.initial_P
        elif slot.finalized:
            est = slot.settled_value
            P = slot.P_smooth if slot.P_smooth is not None else slot.P or self.initial_P
        else:
            x = slot.x_smooth if slot.x_smooth is not None else slot.estimate
            est = self._denormalize(x) if x is not None else self._denormalize(self.initial_x)
            P = slot.P_smooth if slot.P_smooth is not None else slot.P or self.initial_P

        # Fee with uncertainty premium
        if P > self.uncertainty_premium_threshold:
            effective_fee = self.feed_fee * self.uncertainty_premium_multiplier
        else:
            effective_fee = self.feed_fee

        total_fee = effective_fee * n_reads
        self.total_fees_collected += total_fee
        self.n_reads += n_reads

        # Accumulate fee into the next open slot's reward pool
        # (or the current slot if it's still open)
        if slot is not None and not slot.finalized:
            slot.reward_pool += total_fee
        else:
            # Find next non-finalized slot
            for s in self._sorted_slots():
                if not s.finalized and not s.settled:
                    s.reward_pool += total_fee
                    break

        return est, total_fee

    # -- Honey pot -----------------------------------------------------------

    @property
    def honey_pot(self):
        """Total unreleased escrow across all slots."""
        return self.total_escrowed


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


def run_timeseries_simulation(
    oracle,
    reporters,
    n_slots=20,
    true_price_fn=None,
    initial_price=2900.0,
    price_drift=0.002,
    start_block=0,
    strategies=None,
    challenge_fn=None,
    consumer_reads_per_slot=0,
    seed=42,
):
    """Drive a TimeSeriesOracle through *n_slots* of reporting and settlement.

    Args:
        oracle:                TimeSeriesOracle instance
        reporters:             list of (Reporter, noise_std, bias) tuples
        n_slots:               number of time slots to simulate
        true_price_fn:         callable(slot_index) -> float
        initial_price:         starting price (if no true_price_fn)
        price_drift:           std of random walk per slot
        start_block:           block number of first slot
        strategies:            dict: reporter name -> callable(slot_index) -> (noise, bias)
        challenge_fn:          callable(oracle, slot_index, block, slot) -> Reporter|None
        consumer_reads_per_slot: number of consumer reads per slot
        seed:                  random seed

    Returns:
        list of dicts, one per slot (settlement results for finalized slots)
    """
    rng = random.Random(seed)
    _orig_gauss = random.gauss
    random.gauss = rng.gauss

    try:
        price = initial_price
        results = []

        for sid in range(n_slots):
            blk = start_block + sid * oracle.resolution
            now = blk + 1  # just inside the submission window

            if true_price_fn is not None:
                price = true_price_fn(sid)
            else:
                price *= (1.0 + rng.gauss(0, price_drift))

            # Shuffle submission order
            order = list(reporters)
            rng.shuffle(order)

            for reporter, noise_std, bias in order:
                if strategies and reporter.name in strategies:
                    noise_std, bias = strategies[reporter.name](sid)
                value = reporter.observe(price, noise_std=noise_std, bias=bias)
                try:
                    oracle.submit(reporter, blk, value, stake=oracle.min_stake, now=now)
                except (ValueError, RuntimeError) as e:
                    log.warning("Slot %d: %s cannot submit: %s", sid, reporter.name, e)

            # Challenge logic
            if challenge_fn is not None:
                slot = oracle.slots.get(oracle._quantize(blk))
                challenger = challenge_fn(oracle, sid, blk, slot)
                if challenger is not None:
                    try:
                        oracle.challenge(challenger, blk, now=now)
                        # After challenge, let honest reporters re-submit
                        for reporter, noise_std, bias_val in reporters:
                            if bias_val == 0.0 and reporter.name != challenger.name:
                                value = reporter.observe(price, noise_std=noise_std, bias=0.0)
                                try:
                                    oracle.submit(reporter, blk, value,
                                                  stake=oracle.min_stake, now=now)
                                except (ValueError, RuntimeError):
                                    pass
                    except (ValueError, RuntimeError) as e:
                        log.warning("Slot %d: challenge failed: %s", sid, e)

            # Smooth after all submissions for this slot
            oracle.smooth(from_key=blk)

            # Consumer reads
            if consumer_reads_per_slot > 0:
                oracle.read_price(blk, now=now, n_reads=consumer_reads_per_slot)

            # Finalize expired slots (use a block past the challenge window)
            finalize_block = blk + oracle.submission_window + oracle.challenge_window + 1
            finalized = oracle.finalize(now=finalize_block)

            # Collect results for finalized slots
            for slot in finalized:
                results.append({
                    "slot_index": (slot.slot_key - start_block) // oracle.resolution,
                    "slot_key": slot.slot_key,
                    "settled_value": slot.settled_value,
                    "true_price": true_price_fn(
                        (slot.slot_key - start_block) // oracle.resolution
                    ) if true_price_fn else None,
                    "honest": sum(1 for s in slot.submissions
                                  if slot.settled_value and abs(s.value - slot.settled_value) / abs(slot.settled_value) <= oracle.tolerance),
                    "dishonest": sum(1 for s in slot.submissions
                                     if slot.settled_value and abs(s.value - slot.settled_value) / abs(slot.settled_value) > oracle.tolerance),
                    "reward_pool": slot.reward_pool,
                })

        return results

    finally:
        random.gauss = _orig_gauss
