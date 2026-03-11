"""Tests for EscrowOracle and FeedOracle: non-ergodic dynamics.

Models how double-down, retroactive escrow, and consumer feed fees
create conditions where honesty eventually prevails even when liars
temporarily hold a majority -- as long as enough honest participants
exist to *eventually* establish a majority in some future round.
"""

import logging
import math
import random as _rand

import pytest

from alberta_buck.truthstake import (
    EscrowOracle,
    FeedOracle,
    Oracle,
    Reporter,
    run_simulation,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

PRICE = 2900.0


# ---------------------------------------------------------------------------
# Escrow basics: withholding, release, confiscation
# ---------------------------------------------------------------------------

class TestEscrowBasics:

    def test_escrow_withheld_from_payouts(self):
        """20% of each honest payout is held in escrow."""
        oracle = EscrowOracle(
            initial_estimate=PRICE, min_stake=1.0,
            escrow_fraction=0.20, escrow_window=10,
        )
        reporters = []
        for i in range(4):
            r = Reporter(f"h_{i}", stake=1000)
            oracle.register(r)
            reporters.append(r)

        oracle.open_round()
        for r in reporters:
            oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=1.0)
        result = oracle.settle(true_price=PRICE)

        assert result["escrow_withheld"] > 0, "Should withhold escrow"
        assert oracle.total_escrowed > 0, "Escrow should accumulate"
        assert result["total_escrowed"] == pytest.approx(result["escrow_withheld"])

    def test_escrow_released_after_window(self):
        """Escrow is returned to reporter after W rounds without challenge."""
        oracle = EscrowOracle(
            initial_estimate=PRICE, min_stake=1.0,
            escrow_fraction=0.20, escrow_window=5,
        )
        r = Reporter("alice", stake=1000)
        oracle.register(r)

        initial_stake = r.stake
        results = []
        for rid in range(8):
            oracle.open_round()
            oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=1.0)
            result = oracle.settle(true_price=PRICE)
            results.append(result)

        # After 8 rounds with window=5, rounds 0-2 should have been released
        assert results[-1]["escrow_released"] > 0, "Old escrow should be released"

        # Total escrowed should be less than if nothing were released
        cumulative_withheld = sum(r["escrow_withheld"] for r in results)
        assert oracle.total_escrowed < cumulative_withheld, (
            "Released escrow should reduce total")

    def test_escrow_zero_fraction_is_noop(self):
        """EscrowOracle with escrow_fraction=0 behaves like base Oracle."""
        oracle = EscrowOracle(
            initial_estimate=PRICE, min_stake=1.0,
            escrow_fraction=0.0, escrow_window=10,
        )
        reporters = []
        for i in range(4):
            r = Reporter(f"h_{i}", stake=1000)
            oracle.register(r)
            reporters.append(r)

        oracle.open_round()
        for r in reporters:
            oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=1.0)
        result = oracle.settle(true_price=PRICE)

        assert result["escrow_withheld"] == 0
        assert oracle.total_escrowed == 0


# ---------------------------------------------------------------------------
# Double-down mechanics
# ---------------------------------------------------------------------------

class TestDoubleDown:

    def test_liars_withdraw_on_doubledown(self):
        """Liars who won't double down are removed from the round.

        With doubledown_fn that returns True for honest and False for liars,
        the liars' submissions are removed and stakes returned.  The Kalman
        estimate should shift toward the honest reporters' values.
        """
        honest_names = {f"h_{i}" for i in range(3)}

        def dd_fn(reporter, stake, round_id):
            return reporter.name in honest_names

        oracle = EscrowOracle(
            initial_estimate=PRICE, min_stake=2.0,
            escrow_fraction=0.20, escrow_window=10,
            doubledown_fn=dd_fn,
        )
        honest = []
        for i in range(3):
            r = Reporter(f"h_{i}", stake=1000)
            oracle.register(r)
            honest.append(r)
        liars = []
        for i in range(3):
            r = Reporter(f"liar_{i}", stake=1000)
            oracle.register(r)
            liars.append(r)

        challenger = Reporter("ch", stake=1000)
        oracle.register(challenger)

        oracle.open_round()
        for r in honest:
            oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
        for r in liars:
            oracle.submit(r, PRICE * 1.08, stake=2.0)

        # Before challenge: 6 submissions
        assert len(oracle.current_round.submissions) == 6

        oracle.challenge(challenger)

        # After challenge with doubledown: liars withdrew, only honest + challenger remain
        assert len(oracle.current_round.submissions) == 3, (
            f"Expected 3 surviving submissions, got {len(oracle.current_round.submissions)}")

        # Liars' stakes should be returned
        for r in liars:
            assert r.stake == 1000, f"{r.name} stake should be returned: {r.stake}"

        # Honest reporters doubled their stakes
        for r in honest:
            assert r.stake == 1000 - 2 - 2, (  # original 2 + doubled 2
                f"{r.name} stake should reflect doubledown: {r.stake}")

        result = oracle.settle(true_price=PRICE)
        # With liars removed, estimate should be accurate
        assert result["estimate_error"] < 0.02

    def test_doubledown_reveals_conviction(self):
        """Track withdrawal rate as a manipulation signal.

        Run 20 rounds with challenge on every round.  Honest reporters
        always double down; liars withdraw.  The fraction of withdrawals
        predicts the fraction of liars.
        """
        honest_names = {f"h_{i}" for i in range(4)}

        def dd_fn(reporter, stake, round_id):
            return reporter.name in honest_names

        oracle = EscrowOracle(
            initial_estimate=PRICE, min_stake=1.0,
            escrow_fraction=0.20, escrow_window=10,
            doubledown_fn=dd_fn,
        )
        honest = []
        for i in range(4):
            r = Reporter(f"h_{i}", stake=2000)
            oracle.register(r)
            honest.append(r)
        liars = []
        for i in range(3):
            r = Reporter(f"liar_{i}", stake=2000)
            oracle.register(r)
            liars.append(r)

        challenger = Reporter("ch", stake=2000)
        oracle.register(challenger)

        rng = _rand.Random(42)
        results = []
        for rid in range(20):
            oracle.open_round()
            for r in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=1.0)
            for r in liars:
                oracle.submit(r, PRICE * 1.06, stake=1.0)
            oracle.challenge(challenger)
            # Honest re-submit after challenge
            for r in honest:
                try:
                    oracle.submit(r, r.observe(PRICE, noise_std=0.003),
                                  stake=oracle.min_stake * oracle._stake_multiplier())
                except ValueError:
                    pass
            result = oracle.settle(true_price=PRICE)
            results.append(result)

        # Estimate should be very accurate (liars removed every round)
        late_errors = [r["estimate_error"] for r in results[-5:]]
        avg_err = sum(late_errors) / len(late_errors)
        assert avg_err < 0.01, f"With doubledown filtering, error should be <1%: {avg_err:.4f}"

        # Liars should have no losses (they withdrew each round, stake returned)
        # But they also earned nothing
        liar_net = sum(r.earnings - r.losses for r in liars)
        assert liar_net == pytest.approx(0, abs=1.0), (
            f"Liars who withdraw should be ~neutral: {liar_net:.1f}")

        log.info("Doubledown conviction: avg err=%.2f%%, liar net=%.1f",
                 avg_err * 100, liar_net)


# ---------------------------------------------------------------------------
# Retroactive confiscation: liars' escrow confiscated when truth prevails
# ---------------------------------------------------------------------------

class TestRetroactiveConfiscation:

    def test_liar_escrow_confiscated_on_challenge(self):
        """Liars control oracle for 10 rounds, accumulating escrow.

        Then a challenge in round 11 triggers retroactive confiscation:
        the liars' escrowed payouts from the past 10 rounds are re-evaluated
        against ground truth and confiscated because the submissions were
        actually inaccurate (despite being classified "honest" under the
        corrupted settled value).
        """
        oracle = EscrowOracle(
            initial_estimate=PRICE, min_stake=2.0, tolerance=0.05,
            escrow_fraction=0.20, escrow_window=15,
        )
        honest = []
        for i in range(2):
            r = Reporter(f"h_{i}", stake=5000)
            oracle.register(r)
            honest.append(r)
        liars = []
        for i in range(6):
            r = Reporter(f"liar_{i}", stake=5000)
            oracle.register(r)
            liars.append(r)

        # Phase 1: liars control oracle for 10 rounds, escrow accumulates
        rng = _rand.Random(42)
        for rid in range(10):
            oracle.open_round()
            for r in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
            for r in liars:
                oracle.submit(r, PRICE * 1.08, stake=2.0)
            oracle.settle(true_price=PRICE)

        # Record escrow before challenge
        pre_challenge_escrow = oracle.total_escrowed
        assert pre_challenge_escrow > 0, "Escrow should have accumulated"
        log.info("Pre-challenge escrow: %.1f", pre_challenge_escrow)

        # Phase 2: honest challenger with honest influx
        challenger = Reporter("challenger", stake=5000)
        oracle.register(challenger)
        # Add more honest reporters to establish majority
        new_honest = []
        for i in range(6):
            r = Reporter(f"new_h_{i}", stake=5000)
            oracle.register(r)
            new_honest.append(r)

        oracle.open_round()
        for r in honest + new_honest:
            oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
        for r in liars:
            oracle.submit(r, PRICE * 1.08, stake=2.0)
        oracle.challenge(challenger)
        # Honest re-submit post-challenge
        for r in honest + new_honest:
            try:
                eff = oracle.min_stake * oracle._stake_multiplier()
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=eff)
            except ValueError:
                pass

        result = oracle.settle(true_price=PRICE)

        # Retroactive confiscation should have captured liar escrow
        assert result["escrow_confiscated"] > 0, (
            f"Challenge should trigger confiscation: {result['escrow_confiscated']}")

        post_challenge_escrow = oracle.total_escrowed
        assert post_challenge_escrow < pre_challenge_escrow, (
            f"Confiscation should reduce escrow: {post_challenge_escrow:.1f} >= {pre_challenge_escrow:.1f}")

        log.info("Confiscated: %.1f, escrow %.1f -> %.1f",
                 result["escrow_confiscated"], pre_challenge_escrow, post_challenge_escrow)

    def test_honest_escrow_survives_challenge(self):
        """Honest reporters' escrow is NOT confiscated during challenge.

        When a challenge triggers retroactive confiscation, only submissions
        that were actually inaccurate (relative to ground truth) lose escrow.
        Honest submissions retain their escrow.
        """
        oracle = EscrowOracle(
            initial_estimate=PRICE, min_stake=1.0, tolerance=0.05,
            escrow_fraction=0.20, escrow_window=15,
        )
        honest = []
        for i in range(4):
            r = Reporter(f"h_{i}", stake=5000)
            oracle.register(r)
            honest.append(r)
        liar = Reporter("liar", stake=5000)
        oracle.register(liar)

        # 10 rounds: liar submits +8% bias, honest reporters submit truth
        for rid in range(10):
            oracle.open_round()
            for r in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=1.0)
            oracle.submit(liar, PRICE * 1.08, stake=1.0)
            oracle.settle(true_price=PRICE)

        # Challenge round
        challenger = Reporter("ch", stake=5000)
        oracle.register(challenger)
        oracle.open_round()
        for r in honest:
            oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=1.0)
        oracle.submit(liar, PRICE * 1.08, stake=1.0)
        oracle.challenge(challenger)
        for r in honest:
            try:
                eff = oracle.min_stake * oracle._stake_multiplier()
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=eff)
            except ValueError:
                pass
        result = oracle.settle(true_price=PRICE)

        # Check that honest escrow entries are still present (not confiscated)
        honest_escrow_remaining = 0
        for entry in oracle.escrow_ledger:
            if entry.released:
                continue
            for name, esc in entry.holdings.items():
                if name.startswith("h_"):
                    honest_escrow_remaining += esc["amount"]

        assert honest_escrow_remaining > 0, (
            f"Honest reporters' escrow should survive: {honest_escrow_remaining:.1f}")

        log.info("Honest escrow preserved: %.1f, confiscated: %.1f",
                 honest_escrow_remaining, result["escrow_confiscated"])
