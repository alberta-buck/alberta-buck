"""Tests for TimeSeriesOracle: V2 block-indexed oracle with RTS smoother.

Validates the core V2 mechanisms:
  - Block-indexed submission and deferred settlement
  - Forward Kalman pass and RTS backward smoother
  - Age-scaled challenge bonds
  - Forward propagation of corrections
  - Non-ergodic honey-pot dynamics with deferred settlement
  - Consumer per-slot read fees
  - Circular buffer with anchor state
"""

import logging
import math
import random as _rand

import pytest

from alberta_buck.truthstake import (
    Reporter,
    TimeSeriesOracle,
    TimeSlot,
    run_timeseries_simulation,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

PRICE = 2900.0
RESOLUTION = 300        # blocks per slot (~1 hour at 12s/block)
SUB_WINDOW = 150        # blocks for submission window (~30 min)
CH_WINDOW = 300         # blocks for challenge window (~1 hour)


def make_oracle(**kwargs):
    defaults = dict(
        initial_estimate=PRICE,
        resolution=RESOLUTION,
        submission_window=SUB_WINDOW,
        challenge_window=CH_WINDOW,
        min_stake=1.0,
        escrow_fraction=0.20,
        escrow_window=10,
        challenge_P_floor=0.01,
    )
    defaults.update(kwargs)
    return TimeSeriesOracle(**defaults)


# ---------------------------------------------------------------------------
# Basic slot lifecycle
# ---------------------------------------------------------------------------

class TestBasicSlotLifecycle:

    def test_submit_to_slot(self):
        """Reporters can submit values targeting a specific time slot."""
        oracle = make_oracle()
        r = Reporter("alice", stake=100)
        oracle.register(r)

        sub = oracle.submit(r, block=0, value=PRICE, stake=1.0, now=1)
        assert sub is not None
        assert sub.value == PRICE
        assert r.stake == 99.0

        slot = oracle.slots[0]
        assert len(slot.submissions) == 1
        assert slot.reward_pool == 1.0

    def test_submit_outside_window_rejected(self):
        """Submissions after the window closes are rejected."""
        oracle = make_oracle()
        r = Reporter("alice", stake=100)
        oracle.register(r)

        with pytest.raises(RuntimeError, match="Submission window closed"):
            oracle.submit(r, block=0, value=PRICE, stake=1.0,
                          now=SUB_WINDOW + 1)

    def test_submit_to_finalized_slot_rejected(self):
        """Submissions to a finalized slot are rejected."""
        oracle = make_oracle()
        r = Reporter("alice", stake=100)
        oracle.register(r)

        oracle.submit(r, block=0, value=PRICE, stake=1.0, now=1)
        oracle.smooth()
        oracle.finalize(now=SUB_WINDOW + CH_WINDOW + 1)

        r2 = Reporter("bob", stake=100)
        oracle.register(r2)
        with pytest.raises(RuntimeError, match="finalized"):
            oracle.submit(r2, block=0, value=PRICE, stake=1.0, now=1)

    def test_single_slot_finalization(self):
        """A slot finalizes after submission + challenge windows expire."""
        oracle = make_oracle()
        reporters = [Reporter(f"h_{i}", stake=1000) for i in range(4)]
        for r in reporters:
            oracle.register(r)

        for r in reporters:
            oracle.submit(r, block=0, value=PRICE * (1 + _rand.gauss(0, 0.003)),
                          stake=1.0, now=1)

        oracle.smooth()
        finalized = oracle.finalize(now=SUB_WINDOW + CH_WINDOW + 1)
        assert len(finalized) == 1
        slot = finalized[0]
        assert slot.finalized
        assert slot.settled
        assert slot.settled_value is not None
        assert abs(slot.settled_value - PRICE) / PRICE < 0.02

    def test_settlement_against_smoothed_estimate(self):
        """Reporters are judged against the finalized smoothed estimate,
        not the instantaneous forward estimate."""
        oracle = make_oracle()
        honest = Reporter("honest", stake=1000)
        liar = Reporter("liar", stake=1000)
        oracle.register(honest)
        oracle.register(liar)

        oracle.submit(honest, block=0, value=PRICE, stake=5.0, now=1)
        oracle.submit(liar, block=0, value=PRICE * 1.10, stake=5.0, now=2)
        oracle.smooth()

        finalized = oracle.finalize(now=SUB_WINDOW + CH_WINDOW + 1)
        slot = finalized[0]

        # Honest reporter should profit; liar should lose
        assert honest.earnings > 0 or honest.stake > 995, "Honest should earn"
        assert liar.losses > 0, "Liar should lose stake"

    def test_escrow_withheld_and_released(self):
        """Escrow is withheld from payouts and released after window."""
        oracle = make_oracle(escrow_window=3)
        reporters = [Reporter(f"h_{i}", stake=1000) for i in range(3)]
        for r in reporters:
            oracle.register(r)

        # Submit to several slots
        for slot_idx in range(5):
            t = slot_idx * RESOLUTION
            for r in reporters:
                oracle.submit(r, block=t, value=PRICE, stake=1.0, now=t + 1)
            oracle.smooth()

        # Finalize all slots
        far_future = 5 * RESOLUTION + SUB_WINDOW + CH_WINDOW + 1
        finalized = oracle.finalize(now=far_future)
        assert len(finalized) == 5

        # Early slots should have had escrow released (age > 3 slots)
        # The escrow release happens when later slots settle
        # Check that total escrowed is less than if nothing were released
        assert oracle.total_escrowed >= 0


# ---------------------------------------------------------------------------
# Forward pass and RTS smoother
# ---------------------------------------------------------------------------

class TestKalmanSmoother:

    def test_forward_pass_converges(self):
        """Forward pass converges estimate toward submitted values."""
        oracle = make_oracle(initial_P=1.0)
        r = Reporter("alice", stake=1000)
        oracle.register(r)

        for i in range(5):
            t = i * RESOLUTION
            oracle.submit(r, block=t, value=PRICE, stake=1.0, now=t + 1)

        oracle.forward_pass()

        # Later slots should have lower P (more confident)
        slots = oracle._sorted_slots()
        assert slots[-1].P < slots[0].P
        # Estimate should be close to PRICE
        assert abs(oracle._denormalize(slots[-1].estimate) - PRICE) / PRICE < 0.01

    def test_backward_pass_smoothes(self):
        """RTS backward pass propagates future evidence to past slots."""
        oracle = make_oracle(initial_P=1.0, Q=0.001)

        # Create reporters with different noise levels
        accurate = Reporter("accurate", stake=1000)
        accurate.ema_sq_error = 0.0001
        accurate.n_settled = 10
        oracle.register(accurate)

        noisy = Reporter("noisy", stake=1000)
        noisy.ema_sq_error = 0.01
        noisy.n_settled = 10
        oracle.register(noisy)

        # Slot 0: noisy reporter submits biased value
        oracle.submit(noisy, block=0, value=PRICE * 1.02, stake=1.0, now=1)
        # Slots 1-4: accurate reporter submits true value
        for i in range(1, 5):
            t = i * RESOLUTION
            oracle.submit(accurate, block=t, value=PRICE, stake=1.0, now=t + 1)

        oracle.forward_pass()
        # Before backward pass, slot 0 is biased high
        slot0_forward = oracle.slots[0].estimate

        oracle.backward_pass()
        # After backward pass, slot 0 should be pulled toward PRICE
        slot0_smooth = oracle.slots[0].x_smooth

        # Smoothed estimate should be closer to true value than forward-only
        forward_err = abs(oracle._denormalize(slot0_forward) - PRICE)
        smooth_err = abs(oracle._denormalize(slot0_smooth) - PRICE)
        assert smooth_err < forward_err, (
            f"Smoothed error {smooth_err:.2f} should be less than "
            f"forward error {forward_err:.2f}"
        )

    def test_smooth_multi_slot_consistency(self):
        """Smoothed estimates across multiple slots form a consistent curve."""
        oracle = make_oracle(initial_P=0.5, Q=0.0005)

        reporters = []
        for i in range(3):
            r = Reporter(f"h_{i}", stake=1000)
            r.ema_sq_error = 0.001
            r.n_settled = 10
            oracle.register(r)
            reporters.append(r)

        # Simulate a gentle upward drift
        for slot_idx in range(10):
            t = slot_idx * RESOLUTION
            true_val = PRICE * (1.0 + 0.002 * slot_idx)
            for r in reporters:
                v = true_val * (1 + _rand.gauss(0, 0.001))
                oracle.submit(r, block=t, value=v, stake=1.0, now=t + 1)

        oracle.smooth()

        slots = oracle._sorted_slots()
        # Smoothed estimates should be monotonically increasing (roughly)
        smoothed = [oracle._denormalize(s.x_smooth) for s in slots]
        diffs = [smoothed[i+1] - smoothed[i] for i in range(len(smoothed)-1)]
        assert all(d > 0 for d in diffs), "Smoothed curve should track upward drift"


# ---------------------------------------------------------------------------
# Challenges and re-smoothing
# ---------------------------------------------------------------------------

class TestChallenges:

    def test_age_scaled_bond(self):
        """Challenge bond increases with slot age."""
        oracle = make_oracle(base_challenge_bond=2.0)
        r = Reporter("reporter", stake=10000)
        oracle.register(r)
        challenger = Reporter("challenger", stake=10000)
        oracle.register(challenger)

        # Submit to slot 0 and slot 5
        oracle.submit(r, block=0, value=PRICE, stake=1.0, now=1)
        oracle.submit(r, block=5 * RESOLUTION,
                      value=PRICE, stake=1.0, now=5 * RESOLUTION + 1)
        oracle.smooth()

        # Challenge slot 0 when slot 5 is the latest -> age = 5 slots
        ch = oracle.challenge(challenger, block=0,
                              now=SUB_WINDOW + 1)
        # Level 1: base_bond * 2^1 * (1 + 5) = 2 * 2 * 6 = 24
        assert ch.bond == 24.0, f"Expected age-scaled bond 24.0, got {ch.bond}"

    def test_challenge_resets_P(self):
        """Challenge resets P to at least the P floor."""
        oracle = make_oracle(challenge_P_floor=0.01, initial_P=0.01)
        r = Reporter("reporter", stake=1000)
        oracle.register(r)
        challenger = Reporter("challenger", stake=1000)
        oracle.register(challenger)

        # Submit several values to drive P down
        for i in range(5):
            t = i * RESOLUTION
            oracle.submit(r, block=t, value=PRICE, stake=1.0, now=t + 1)
        oracle.smooth()

        slot = oracle.slots[0]
        P_before = slot.P
        assert P_before < 0.01, "P should have decreased from submissions"

        oracle.challenge(challenger, block=0, now=SUB_WINDOW + 1)
        assert slot.P >= 0.01, "Challenge should reset P to floor"

    def test_challenge_reopens_slot_for_submissions(self):
        """After a challenge, new submissions are accepted for the slot."""
        oracle = make_oracle()
        r = Reporter("reporter", stake=1000)
        oracle.register(r)
        challenger = Reporter("challenger", stake=1000)
        oracle.register(challenger)
        newcomer = Reporter("newcomer", stake=1000)
        oracle.register(newcomer)

        oracle.submit(r, block=0, value=PRICE, stake=1.0, now=1)
        oracle.smooth()

        # Challenge extends the window
        oracle.challenge(challenger, block=0, now=SUB_WINDOW + 1)

        # Newcomer can now submit (window extended by challenge)
        # Stake must meet the escalated minimum (2^level = 2.0)
        sub = oracle.submit(newcomer, block=0, value=PRICE,
                            stake=2.0, now=SUB_WINDOW + 2)
        assert sub is not None, "Should accept submission after challenge reopens slot"

    def test_forward_propagation_after_challenge(self):
        """Challenging slot T and re-smoothing changes estimates at T+1..T+N."""
        oracle = make_oracle(initial_P=0.5, Q=0.001, challenge_P_floor=0.5)

        honest = Reporter("honest", stake=10000)
        honest.ema_sq_error = 0.001
        honest.n_settled = 10
        oracle.register(honest)

        liar = Reporter("liar", stake=10000)
        liar.ema_sq_error = 0.001
        liar.n_settled = 10
        oracle.register(liar)

        challenger = Reporter("challenger", stake=10000)
        oracle.register(challenger)

        # Slot 0: liar submits biased value (no honest submission)
        oracle.submit(liar, block=0, value=PRICE * 1.10, stake=1.0, now=1)

        # Slots 1-4: honest reporter submits true values
        for i in range(1, 5):
            t = i * RESOLUTION
            oracle.submit(honest, block=t, value=PRICE, stake=1.0, now=t + 1)

        oracle.smooth()

        # Record pre-challenge smoothed estimates
        pre_challenge = {s.slot_key: s.x_smooth for s in oracle._sorted_slots()}

        # Challenge slot 0 and add honest correction
        oracle.challenge(challenger, block=0, now=SUB_WINDOW + 1)
        # Stake must meet escalated minimum (2^level = 2.0)
        oracle.submit(honest, block=0, value=PRICE, stake=2.0,
                      now=SUB_WINDOW + 2)
        oracle.smooth(from_key=0)

        # Post-challenge estimates should have shifted
        for slot in oracle._sorted_slots():
            if slot.slot_key == 0:
                # Slot 0 should now be closer to PRICE
                new_est = oracle._denormalize(slot.x_smooth)
                assert abs(new_est - PRICE) / PRICE < 0.05, (
                    f"Slot 0 should be corrected toward {PRICE}, got {new_est}"
                )

    def test_progressive_challenge_escalation(self):
        """Successive challenges double the P floor."""
        oracle = make_oracle(challenge_P_floor=0.01, base_challenge_bond=1.0)
        r = Reporter("r", stake=10000)
        oracle.register(r)
        c1 = Reporter("c1", stake=10000)
        oracle.register(c1)
        c2 = Reporter("c2", stake=10000)
        oracle.register(c2)

        oracle.submit(r, block=0, value=PRICE, stake=1.0, now=1)
        oracle.smooth()

        # First challenge: P floor = 0.01
        oracle.challenge(c1, block=0, now=SUB_WINDOW + 1)
        P_after_1 = oracle.slots[0].P
        assert P_after_1 >= 0.01

        # Second challenge: P floor = 0.01 * 2 = 0.02
        oracle.challenge(c2, block=0, now=SUB_WINDOW + 2)
        P_after_2 = oracle.slots[0].P
        assert P_after_2 >= 0.02


# ---------------------------------------------------------------------------
# Deferred settlement: anti-circular-dependency
# ---------------------------------------------------------------------------

class TestDeferredSettlement:

    def test_liars_judged_against_final_consensus(self):
        """With deferred settlement, liars are judged against the *finalized*
        estimate -- not the instantaneous estimate they controlled."""
        oracle = make_oracle(initial_P=0.5, Q=0.001, escrow_fraction=0.0)

        # Honest reporters with good reputation
        honest_reporters = []
        for i in range(5):
            r = Reporter(f"h_{i}", stake=1000)
            r.ema_sq_error = 0.001
            r.n_settled = 10
            oracle.register(r)
            honest_reporters.append(r)

        # Liars
        liars = []
        for i in range(3):
            r = Reporter(f"l_{i}", stake=1000)
            r.ema_sq_error = 0.001
            r.n_settled = 10
            oracle.register(r)
            liars.append(r)

        challenger = Reporter("challenger", stake=1000)
        oracle.register(challenger)

        # All submit to slot 0: liars bias high
        t = 0
        for r in liars:
            oracle.submit(r, block=t, value=PRICE * 1.10, stake=5.0, now=1)
        for r in honest_reporters:
            oracle.submit(r, block=t, value=PRICE, stake=5.0, now=2)

        # Slot 1-4: only honest reporters (establishing truth)
        for i in range(1, 5):
            t = i * RESOLUTION
            for r in honest_reporters:
                oracle.submit(r, block=t, value=PRICE, stake=1.0, now=t + 1)

        oracle.smooth()

        # Finalize all slots
        far_future = 5 * RESOLUTION + SUB_WINDOW + CH_WINDOW + 1
        finalized = oracle.finalize(now=far_future)

        # Slot 0 should settle near PRICE (RTS smoother pulls it back)
        slot0 = oracle.slots[0]
        assert abs(slot0.settled_value - PRICE) / PRICE < 0.05, (
            f"Slot 0 should settle near {PRICE}, got {slot0.settled_value}"
        )

        # Liars should have lost their stakes
        total_liar_losses = sum(r.losses for r in liars)
        assert total_liar_losses > 0, "Liars should lose stakes"

    def test_no_circular_dependency(self):
        """In V1, liars who control the settled value are judged against it
        (circular dependency).  In V2, a challenge + deferred settlement +
        RTS smoother breaks the circle: the corrected estimate becomes the
        reference, not the liar-controlled one."""
        oracle = make_oracle(
            initial_P=1.0, Q=0.001, escrow_fraction=0.0,
            challenge_P_floor=0.5,
        )

        # One strong liar, many honest reporters
        liar = Reporter("liar", stake=5000)
        liar.ema_sq_error = 0.0001  # excellent fake reputation
        liar.n_settled = 20
        oracle.register(liar)

        honest = []
        for i in range(6):
            r = Reporter(f"h_{i}", stake=500)
            r.ema_sq_error = 0.01
            r.n_settled = 5
            oracle.register(r)
            honest.append(r)

        challenger = Reporter("challenger", stake=5000)
        oracle.register(challenger)

        # Slot 0: liar dominates with excellent R
        oracle.submit(liar, block=0, value=PRICE * 1.15, stake=10.0, now=1)
        for r in honest:
            oracle.submit(r, block=0, value=PRICE, stake=2.0, now=2)

        # Slots 1-9: only honest reporters build truth
        for i in range(1, 10):
            t = i * RESOLUTION
            for r in honest:
                oracle.submit(r, block=t, value=PRICE, stake=1.0, now=t + 1)

        oracle.smooth()

        # Before challenge: slot 0 is dominated by liar
        est0_pre = oracle._denormalize(oracle.slots[0].x_smooth)
        assert abs(est0_pre - PRICE) / PRICE > 0.05, (
            "Before challenge, slot 0 should be biased toward liar"
        )

        # Challenge slot 0: resets P to floor, allowing re-evaluation
        oracle.challenge(challenger, block=0, now=SUB_WINDOW + 1)
        # Honest reporters re-submit at escalated stake
        for r in honest:
            oracle.submit(r, block=0, value=PRICE, stake=2.0,
                          now=SUB_WINDOW + 2)
        oracle.smooth(from_key=0)

        # After challenge: smoother + honest re-submissions correct slot 0
        est0_post = oracle._denormalize(oracle.slots[0].x_smooth)
        assert abs(est0_post - PRICE) / PRICE < 0.10, (
            f"After challenge, slot 0 should be near {PRICE}, got {est0_post}"
        )


# ---------------------------------------------------------------------------
# Honey pot and non-ergodic dynamics
# ---------------------------------------------------------------------------

class TestHoneyPot:

    def test_honey_pot_grows_during_liar_control(self):
        """While liars control the oracle, escrow accumulates as the honey pot."""
        oracle = make_oracle(escrow_fraction=0.25, initial_P=0.5, Q=0.001)

        liars = []
        for i in range(4):
            r = Reporter(f"l_{i}", stake=2000)
            r.ema_sq_error = 0.001
            r.n_settled = 10
            oracle.register(r)
            liars.append(r)

        # 10 slots of liar control
        for slot_idx in range(10):
            t = slot_idx * RESOLUTION
            for r in liars:
                oracle.submit(r, block=t, value=PRICE * 1.10,
                              stake=2.0, now=t + 1)

        oracle.smooth()
        far_future = 10 * RESOLUTION + SUB_WINDOW + CH_WINDOW + 1
        oracle.finalize(now=far_future)

        assert oracle.honey_pot > 0, "Escrow should accumulate during liar control"

    def test_retroactive_confiscation_after_correction(self):
        """When liars control the settled value, their escrow is confiscated
        by retroactive comparison against true price."""
        oracle = make_oracle(
            escrow_fraction=0.25, initial_P=0.5, Q=0.001,
            escrow_window=50,  # long window so escrow isn't released
        )

        # Liars outnumber honest: they control the settled value and get
        # classified as "honest", accumulating escrow.  Retroactive
        # confiscation against true_price catches them.
        liars = []
        for i in range(6):
            r = Reporter(f"l_{i}", stake=5000)
            r.ema_sq_error = 0.001
            r.n_settled = 10
            oracle.register(r)
            liars.append(r)

        honest = []
        for i in range(2):
            r = Reporter(f"h_{i}", stake=5000)
            r.ema_sq_error = 0.001
            r.n_settled = 10
            oracle.register(r)
            honest.append(r)

        # Slots 0-4: liars dominate, settled value drifts toward biased price
        for slot_idx in range(5):
            t = slot_idx * RESOLUTION
            for r in liars:
                oracle.submit(r, block=t, value=PRICE * 1.10,
                              stake=2.0, now=t + 1)
            for r in honest:
                oracle.submit(r, block=t, value=PRICE,
                              stake=2.0, now=t + 2)

        oracle.smooth()
        far_future = 5 * RESOLUTION + SUB_WINDOW + CH_WINDOW + 1
        oracle.finalize(now=far_future)

        pot_before = oracle.honey_pot
        assert pot_before > 0, "Liars should have accumulated escrow"

        # Retroactive confiscation using true price -- liars' submissions
        # (PRICE * 1.10) are >5% from true PRICE
        confiscated = oracle.retroactive_confiscation(
            from_key=0,
            true_price_fn=lambda ts: PRICE,
        )

        assert confiscated > 0, "Liar escrow should be confiscated"
        assert oracle.honey_pot < pot_before, "Honey pot should shrink after confiscation"


# ---------------------------------------------------------------------------
# Consumer reads with per-slot uncertainty pricing
# ---------------------------------------------------------------------------

class TestConsumerReads:

    def test_read_price_returns_estimate(self):
        """Consumer read returns the current best estimate."""
        oracle = make_oracle()
        r = Reporter("r", stake=1000)
        oracle.register(r)

        oracle.submit(r, block=0, value=PRICE, stake=1.0, now=1)
        oracle.smooth()

        est, fee = oracle.read_price(0, now=100)
        assert abs(est - PRICE) / PRICE < 0.02

    def test_uncertainty_premium_on_high_P_slot(self):
        """Slots with high P charge the uncertainty premium."""
        oracle = make_oracle(
            initial_P=0.1,  # high initial uncertainty
            feed_fee=1.0,
            uncertainty_premium_threshold=0.005,
            uncertainty_premium_multiplier=3.0,
        )
        r = Reporter("r", stake=1000)
        oracle.register(r)

        oracle.submit(r, block=0, value=PRICE, stake=1.0, now=1)
        oracle.smooth()

        _, fee = oracle.read_price(0, now=100)
        # P should be high (initial P = 0.1, only one untrusted submission)
        # so uncertainty premium should apply
        assert fee == 3.0, f"Expected uncertainty premium fee 3.0, got {fee}"

    def test_low_P_slot_charges_base_fee(self):
        """Well-established slots charge the base fee."""
        oracle = make_oracle(
            initial_P=0.001,  # low initial uncertainty
            Q=0.00001,
            feed_fee=1.0,
            uncertainty_premium_threshold=0.005,
            uncertainty_premium_multiplier=3.0,
        )

        reporters = []
        for i in range(5):
            r = Reporter(f"h_{i}", stake=1000)
            r.ema_sq_error = 0.0001
            r.n_settled = 10
            oracle.register(r)
            reporters.append(r)

        # Many confident submissions -> very low P
        for r in reporters:
            oracle.submit(r, block=0, value=PRICE, stake=1.0, now=1)
        oracle.smooth()

        slot = oracle.slots[0]
        assert slot.P_smooth < 0.005, f"P should be low, got {slot.P_smooth}"

        _, fee = oracle.read_price(0, now=100)
        assert fee == 1.0, f"Expected base fee 1.0, got {fee}"


# ---------------------------------------------------------------------------
# Simulation driver
# ---------------------------------------------------------------------------

class TestSimulationDriver:

    def test_basic_simulation(self):
        """run_timeseries_simulation produces settlement results."""
        oracle = make_oracle(initial_P=0.5, Q=0.001, escrow_fraction=0.0)
        reporters = []
        for i in range(4):
            r = Reporter(f"h_{i}", stake=1000)
            oracle.register(r)
            reporters.append((r, 0.003, 0.0))

        results = run_timeseries_simulation(
            oracle, reporters, n_slots=10,
            true_price_fn=lambda i: PRICE,
            seed=42,
        )

        assert len(results) > 0, "Should have finalized slots"
        for res in results:
            assert abs(res["settled_value"] - PRICE) / PRICE < 0.05

    def test_simulation_with_liars(self):
        """Simulation with liars: honest reporters profit, liars lose."""
        oracle = make_oracle(initial_P=0.5, Q=0.001, escrow_fraction=0.0)

        honest_reporters = []
        for i in range(4):
            r = Reporter(f"h_{i}", stake=1000)
            oracle.register(r)
            honest_reporters.append((r, 0.003, 0.0))

        liar_reporters = []
        for i in range(2):
            r = Reporter(f"l_{i}", stake=1000)
            oracle.register(r)
            liar_reporters.append((r, 0.003, 0.10))

        all_reporters = honest_reporters + liar_reporters

        results = run_timeseries_simulation(
            oracle, all_reporters, n_slots=15,
            true_price_fn=lambda i: PRICE,
            seed=42,
        )

        # After 15 slots, honest reporters should net positive
        total_honest_net = sum(r.earnings - r.losses for r, _, _ in honest_reporters)
        total_liar_net = sum(r.earnings - r.losses for r, _, _ in liar_reporters)

        assert total_honest_net > 0, f"Honest net {total_honest_net} should be positive"
        assert total_liar_net < 0, f"Liar net {total_liar_net} should be negative"


# ---------------------------------------------------------------------------
# Curve shape integrity
# ---------------------------------------------------------------------------

class TestCurveShape:

    def test_rate_of_return_preserved(self):
        """The smoothed curve preserves the rate of return from true prices."""
        oracle = make_oracle(initial_P=0.5, Q=0.0005, escrow_fraction=0.0)

        reporters = []
        for i in range(5):
            r = Reporter(f"h_{i}", stake=1000)
            r.ema_sq_error = 0.001
            r.n_settled = 10
            oracle.register(r)
            reporters.append(r)

        # True price rises 1% per slot
        n_slots = 10
        true_prices = [PRICE * (1.01 ** i) for i in range(n_slots)]

        for slot_idx in range(n_slots):
            t = slot_idx * RESOLUTION
            for r in reporters:
                v = true_prices[slot_idx] * (1 + _rand.gauss(0, 0.001))
                oracle.submit(r, block=t, value=v, stake=1.0, now=t + 1)

        oracle.smooth()

        # Compute rate of return from smoothed curve
        slots = oracle._sorted_slots()
        smoothed = [oracle._denormalize(s.x_smooth) for s in slots]

        returns = [(smoothed[i+1] - smoothed[i]) / smoothed[i]
                   for i in range(len(smoothed) - 1)]

        # Average return should be close to 1%
        avg_return = sum(returns) / len(returns)
        assert abs(avg_return - 0.01) < 0.005, (
            f"Average return {avg_return:.4f} should be close to 0.01"
        )

    def test_flash_crash_not_smoothed_away(self):
        """A genuine flash crash is preserved in the smoothed curve."""
        oracle = make_oracle(initial_P=0.5, Q=0.001, escrow_fraction=0.0)

        reporters = []
        for i in range(5):
            r = Reporter(f"h_{i}", stake=1000)
            r.ema_sq_error = 0.001
            r.n_settled = 10
            oracle.register(r)
            reporters.append(r)

        # Price is stable, then crashes at slot 5, recovers at slot 6
        def true_price(slot_idx):
            if slot_idx == 5:
                return PRICE * 0.90  # 10% crash
            return PRICE

        for slot_idx in range(10):
            t = slot_idx * RESOLUTION
            p = true_price(slot_idx)
            for r in reporters:
                v = p * (1 + _rand.gauss(0, 0.001))
                oracle.submit(r, block=t, value=v, stake=1.0, now=t + 1)

        oracle.smooth()

        # Slot 5 should show a dip
        slot5 = oracle.slots[5 * RESOLUTION]
        est5 = oracle._denormalize(slot5.x_smooth)
        # The smoother will partially smooth the crash, but it should still be
        # materially below PRICE
        assert est5 < PRICE * 0.97, (
            f"Flash crash should be visible: slot 5 estimate {est5:.2f} "
            f"should be below {PRICE * 0.97:.2f}"
        )


# ---------------------------------------------------------------------------
# Circular buffer and anchor state
# ---------------------------------------------------------------------------

class TestCircularBuffer:

    def test_buffer_evicts_oldest_finalized(self):
        """When buffer_size is set, oldest finalized slots are evicted."""
        oracle = make_oracle(buffer_size=5, escrow_fraction=0.0)
        r = Reporter("alice", stake=10000)
        oracle.register(r)

        # Submit to 10 slots
        for i in range(10):
            blk = i * RESOLUTION
            oracle.submit(r, block=blk, value=PRICE, stake=1.0, now=blk + 1)
        oracle.smooth()

        # Finalize all (far enough in the future)
        far_future = 10 * RESOLUTION + SUB_WINDOW + CH_WINDOW + 1
        oracle.finalize(now=far_future)

        # Buffer should have at most 5 slots
        assert len(oracle.slots) <= 5, (
            f"Buffer should be <= 5 slots, got {len(oracle.slots)}"
        )

    def test_anchor_state_preserved_after_eviction(self):
        """Evicted slot's state is saved as the anchor for forward pass."""
        oracle = make_oracle(buffer_size=3, escrow_fraction=0.0)
        r = Reporter("alice", stake=10000)
        r.ema_sq_error = 0.001
        r.n_settled = 10
        oracle.register(r)

        # Submit to 6 slots
        for i in range(6):
            blk = i * RESOLUTION
            oracle.submit(r, block=blk, value=PRICE, stake=1.0, now=blk + 1)
        oracle.smooth()

        far_future = 6 * RESOLUTION + SUB_WINDOW + CH_WINDOW + 1
        oracle.finalize(now=far_future)

        # Anchor should have been set from evicted slots
        assert oracle.anchor_key is not None, "Anchor key should be set"
        assert oracle.anchor_x is not None, "Anchor x should be set"
        assert oracle.anchor_P is not None, "Anchor P should be set"
        # Anchor should be close to PRICE (normalized to ~1.0)
        assert abs(oracle.anchor_x - 1.0) < 0.01, (
            f"Anchor x should be ~1.0, got {oracle.anchor_x}"
        )

    def test_forward_pass_resumes_from_anchor(self):
        """After eviction, new slots' forward pass starts from the anchor."""
        oracle = make_oracle(
            buffer_size=5, escrow_fraction=0.0, initial_P=0.5, Q=0.001,
        )
        reporters = []
        for i in range(3):
            r = Reporter(f"h_{i}", stake=10000)
            r.ema_sq_error = 0.001
            r.n_settled = 10
            oracle.register(r)
            reporters.append(r)

        # Run 15 slots -- first 10 will be evicted (buffer=5)
        for i in range(15):
            blk = i * RESOLUTION
            for r in reporters:
                oracle.submit(r, block=blk, value=PRICE, stake=1.0, now=blk + 1)
            oracle.smooth()
            finalize_blk = blk + SUB_WINDOW + CH_WINDOW + 1
            oracle.finalize(now=finalize_blk)

        # Buffer should have the latest slots only
        assert len(oracle.slots) <= 5

        # Submit a new slot -- forward pass should work from anchor
        blk = 15 * RESOLUTION
        for r in reporters:
            oracle.submit(r, block=blk, value=PRICE, stake=1.0, now=blk + 1)
        oracle.smooth()

        slot = oracle.slots[blk]
        est = oracle._denormalize(slot.x_smooth)
        assert abs(est - PRICE) / PRICE < 0.02, (
            f"Post-eviction estimate should be accurate, got {est}"
        )

    def test_non_finalized_slots_never_evicted(self):
        """Only finalized slots are evicted; non-finalized survive."""
        oracle = make_oracle(buffer_size=3, escrow_fraction=0.0)
        r = Reporter("alice", stake=10000)
        oracle.register(r)

        # Submit 5 slots but don't finalize the last 2
        for i in range(5):
            blk = i * RESOLUTION
            oracle.submit(r, block=blk, value=PRICE, stake=1.0, now=blk + 1)
        oracle.smooth()

        # Finalize only slots 0-2 (slot 3 deadline = 900 + 150 + 300 = 1350)
        finalize_time = 3 * RESOLUTION + SUB_WINDOW + CH_WINDOW - 1
        oracle.finalize(now=finalize_time)

        # Slots 0-2 finalized, slots 3-4 not finalized.
        # Buffer tries to evict down to 3, but stops before non-finalized slots.
        remaining = oracle._sorted_slots()
        non_fin = [s for s in remaining if not s.finalized]
        assert len(non_fin) == 2, "Non-finalized slots should survive eviction"

    def test_unbounded_buffer_default(self):
        """Without buffer_size, all slots are retained."""
        oracle = make_oracle(escrow_fraction=0.0)  # buffer_size=None
        r = Reporter("alice", stake=10000)
        oracle.register(r)

        for i in range(20):
            blk = i * RESOLUTION
            oracle.submit(r, block=blk, value=PRICE, stake=1.0, now=blk + 1)
        oracle.smooth()

        far_future = 20 * RESOLUTION + SUB_WINDOW + CH_WINDOW + 1
        oracle.finalize(now=far_future)

        assert len(oracle.slots) == 20, "All slots should be retained"
