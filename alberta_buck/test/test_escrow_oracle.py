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


# ---------------------------------------------------------------------------
# Non-ergodic honey pot: liars accumulate, then lose everything
# ---------------------------------------------------------------------------

class TestNonErgodicCollapse:

    def test_honey_pot_grows_during_liar_control(self):
        """Escrow accumulates monotonically while liars control the oracle.

        The "honey pot" is the total unreleased escrow.  During liar control,
        liars are classified "honest" (relative to the biased settled value)
        and accumulate escrowed payouts.  The pot grows every round, creating
        the non-ergodic trap.
        """
        oracle = EscrowOracle(
            initial_estimate=PRICE, min_stake=2.0, tolerance=0.05,
            escrow_fraction=0.20, escrow_window=30,  # long window to prevent release
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

        pot_history = []
        for rid in range(20):
            oracle.open_round()
            for r in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
            for r in liars:
                oracle.submit(r, PRICE * 1.08, stake=2.0)
            oracle.settle(true_price=PRICE)
            pot_history.append(oracle.honey_pot)

        # Honey pot should be monotonically increasing
        for i in range(1, len(pot_history)):
            assert pot_history[i] >= pot_history[i - 1], (
                f"Pot should grow: round {i} pot={pot_history[i]:.1f} "
                f"< round {i-1} pot={pot_history[i-1]:.1f}")

        assert pot_history[-1] > pot_history[0] * 5, (
            f"Pot should grow substantially: {pot_history[0]:.1f} -> {pot_history[-1]:.1f}")

        log.info("Honey pot growth: %.1f -> %.1f over 20 rounds",
                 pot_history[0], pot_history[-1])

    def test_liar_collapse_on_honest_influx(self):
        """Liars control for 20 rounds, then honest influx triggers collapse.

        This is the central non-ergodic dynamic:
        1. Liars hold 6:2 majority for 20 rounds, accumulating escrow
        2. The visible honey pot attracts 8 new honest reporters
        3. Honest majority + multiple challenge escalations widen the gate
           so honest submissions can enter despite the biased estimate
        4. Retroactive confiscation captures liars' accumulated escrow

        The gate_sigma is set wide enough that a challenge can restore
        honest access even when the estimate has drifted significantly.
        """
        oracle = EscrowOracle(
            initial_estimate=PRICE, min_stake=2.0, tolerance=0.05,
            escrow_fraction=0.25, escrow_window=30,
            gate_sigma=10.0,  # wider gate: honest can enter after challenge
            challenge_P_floor=0.01,  # reset P on challenge: break liar trust monopoly
        )
        honest = []
        for i in range(2):
            r = Reporter(f"h_{i}", stake=10000)
            oracle.register(r)
            honest.append(r)
        liars = []
        for i in range(6):
            r = Reporter(f"liar_{i}", stake=10000)
            oracle.register(r)
            liars.append(r)

        # Phase 1: liar control, 20 rounds
        for rid in range(20):
            oracle.open_round()
            for r in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
            for r in liars:
                oracle.submit(r, PRICE * 1.08, stake=2.0)
            oracle.settle(true_price=PRICE)

        liar_stake_before = sum(r.stake for r in liars)
        pot_before_influx = oracle.honey_pot
        log.info("Pre-influx: liar stakes=%.0f, honey pot=%.1f",
                 liar_stake_before, pot_before_influx)

        # Phase 2: honest influx (attracted by the honey pot)
        # The newcomers need ~5 rounds to build R and shift the estimate.
        # During the transition, both sides lose money.  After correction,
        # liars are classified dishonest and honest reporters harvest their
        # stakes plus confiscated escrow.
        new_honest = []
        for i in range(8):
            r = Reporter(f"new_h_{i}", stake=10000)
            oracle.register(r)
            new_honest.append(r)

        challenger = Reporter("challenger", stake=10000)
        oracle.register(challenger)

        # Run 25 rounds: enough for newcomers to build R and correct estimate
        confiscated_total = 0.0
        for rid in range(25):
            oracle.open_round()
            for r in honest + new_honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
            for r in liars:
                oracle.submit(r, PRICE * 1.08, stake=2.0)
            # Challenge periodically during correction
            if rid < 5 or rid % 5 == 0:
                try:
                    oracle.challenge(challenger)
                    for r in honest + new_honest:
                        try:
                            eff = oracle.min_stake * oracle._stake_multiplier()
                            oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=eff)
                        except ValueError:
                            pass
                except ValueError:
                    pass
            result = oracle.settle(true_price=PRICE)
            confiscated_total += result.get("escrow_confiscated", 0)

        # The confiscated amount should be significant
        assert confiscated_total > 0, (
            f"Retroactive confiscation should capture liar escrow: {confiscated_total:.1f}")

        # Liars should have substantial losses from BOTH mechanisms:
        # normal settlement (losing current stakes) + confiscated escrow
        liar_total_losses = sum(r.losses for r in liars)
        assert liar_total_losses > 100, (
            f"Liars should have large cumulative losses: {liar_total_losses:.1f}")

        # The key non-ergodic property: liars' losses should EXCEED
        # what they accumulated during their period of control.
        # Their 20 rounds of control earned them some payouts, but the
        # escrow confiscation + ongoing settlement losses eat those gains.
        liar_net = sum(r.earnings - r.losses for r in liars)
        assert liar_net < 0, (
            f"Liars should be net negative despite 20 rounds of control: {liar_net:.1f}")

        log.info("Liar collapse: confiscated=%.1f, liar net=%.1f, "
                 "pot before=%.1f",
                 confiscated_total, liar_net, pot_before_influx)

    def test_longer_liar_control_means_larger_collapse(self):
        """The longer liars maintain control, the more they lose when caught.

        Compare two scenarios: liars control for 10 rounds vs 30 rounds.
        The 30-round scenario should produce larger confiscation because
        more escrow accumulated.  This is the non-ergodic property: the
        attack cannot be sustained -- it becomes MORE costly over time,
        not less.
        """
        def run_liar_control(n_liar_rounds):
            oracle = EscrowOracle(
                initial_estimate=PRICE, min_stake=2.0, tolerance=0.05,
                escrow_fraction=0.25, escrow_window=50,
            )
            honest = []
            for i in range(2):
                r = Reporter(f"h_{i}", stake=20000)
                oracle.register(r)
                honest.append(r)
            liars = []
            for i in range(6):
                r = Reporter(f"liar_{i}", stake=20000)
                oracle.register(r)
                liars.append(r)

            for rid in range(n_liar_rounds):
                oracle.open_round()
                for r in honest:
                    oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
                for r in liars:
                    oracle.submit(r, PRICE * 1.08, stake=2.0)
                oracle.settle(true_price=PRICE)

            pot = oracle.honey_pot

            # Honest influx + challenge
            new_honest = []
            for i in range(8):
                r = Reporter(f"new_{i}", stake=20000)
                oracle.register(r)
                new_honest.append(r)
            challenger = Reporter("ch", stake=20000)
            oracle.register(challenger)

            confiscated = 0
            for rid in range(5):
                oracle.open_round()
                for r in honest + new_honest:
                    oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
                for r in liars:
                    oracle.submit(r, PRICE * 1.08, stake=2.0)
                try:
                    oracle.challenge(challenger)
                    for r in honest + new_honest:
                        try:
                            eff = oracle.min_stake * oracle._stake_multiplier()
                            oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=eff)
                        except ValueError:
                            pass
                except ValueError:
                    pass
                result = oracle.settle(true_price=PRICE)
                confiscated += result.get("escrow_confiscated", 0)

            liar_losses = sum(r.losses for r in liars)
            return pot, confiscated, liar_losses

        pot_10, conf_10, loss_10 = run_liar_control(10)
        pot_30, conf_30, loss_30 = run_liar_control(30)

        # 30 rounds should accumulate more escrow
        assert pot_30 > pot_10, (
            f"30-round pot ({pot_30:.1f}) should exceed 10-round ({pot_10:.1f})")

        # 30 rounds should produce larger confiscation
        assert conf_30 > conf_10, (
            f"30-round confiscation ({conf_30:.1f}) should exceed 10-round ({conf_10:.1f})")

        # 30 rounds should produce larger total liar losses
        assert loss_30 > loss_10, (
            f"30-round losses ({loss_30:.1f}) should exceed 10-round ({loss_10:.1f})")

        log.info("Non-ergodic scaling: 10 rounds pot=%.1f conf=%.1f loss=%.1f; "
                 "30 rounds pot=%.1f conf=%.1f loss=%.1f",
                 pot_10, conf_10, loss_10, pot_30, conf_30, loss_30)

    def test_brief_liar_majority_insufficient(self):
        """A brief liar majority (3 rounds) does NOT accumulate enough escrow
        to create a meaningful honey pot.  Short attacks are unprofitable
        because the escrow fraction is small and the honest majority quickly
        reasserts control.
        """
        oracle = EscrowOracle(
            initial_estimate=PRICE, min_stake=2.0, tolerance=0.05,
            escrow_fraction=0.25, escrow_window=30,
            challenge_P_floor=0.01,
        )
        honest = []
        for i in range(4):
            r = Reporter(f"h_{i}", stake=5000)
            oracle.register(r)
            honest.append(r)
        liars = []
        for i in range(6):
            r = Reporter(f"liar_{i}", stake=5000)
            oracle.register(r)
            liars.append(r)

        # Warmup: honest control for 10 rounds
        for rid in range(10):
            oracle.open_round()
            for r in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
            oracle.settle(true_price=PRICE)

        # Brief liar attack: only 3 rounds
        for rid in range(3):
            oracle.open_round()
            for r in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
            for r in liars:
                oracle.submit(r, PRICE * 1.08, stake=2.0)
            oracle.settle(true_price=PRICE)

        pot_after_brief = oracle.honey_pot

        # Honest reasserts (with challenge to reset P)
        challenger = Reporter("ch", stake=5000)
        oracle.register(challenger)
        for rid in range(5):
            oracle.open_round()
            for r in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
            try:
                oracle.challenge(challenger)
                for r in honest:
                    try:
                        eff = oracle.min_stake * oracle._stake_multiplier()
                        oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=eff)
                    except ValueError:
                        pass
            except ValueError:
                pass
            oracle.settle(true_price=PRICE)

        # Brief attack should leave liars net negative (they lost stakes
        # during honest-controlled rounds, gained little from 3 rounds)
        liar_net = sum(r.earnings - r.losses for r in liars)
        assert liar_net < 0, (
            f"Brief attack should be unprofitable: liar net = {liar_net:.1f}")

        log.info("Brief liar majority: pot=%.1f, liar net=%.1f",
                 pot_after_brief, liar_net)

    def test_attrition_liars_run_out_of_capital(self):
        """Liars maintaining an attack eventually run out of capital.

        Each round costs liars their stake.  With a 6:3 honest majority and
        regular challenges (P floor resets), liars lose their stakes to the
        honest pool every round.

        Capital accounting includes both liquid stake and escrowed amounts.
        Over enough rounds, liar capital depletes while honest capital
        (liquid + escrowed) grows from harvested stakes.
        """
        oracle = EscrowOracle(
            initial_estimate=PRICE, min_stake=50.0, tolerance=0.05,
            escrow_fraction=0.10, escrow_window=10,
            challenge_P_floor=0.01,
        )
        honest = []
        for i in range(6):
            r = Reporter(f"h_{i}", stake=5000)
            oracle.register(r)
            honest.append(r)
        liars = []
        for i in range(3):
            r = Reporter(f"liar_{i}", stake=5000)
            oracle.register(r)
            liars.append(r)

        challenger = Reporter("ch", stake=5000)
        oracle.register(challenger)

        liar_capital_history = []
        honest_capital_history = []

        for rid in range(80):
            oracle.open_round()
            for r in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=50.0)
            for r in liars:
                if r.stake >= 50.0:
                    oracle.submit(r, PRICE * 1.08, stake=50.0)
            # Challenge every 3rd round
            if rid % 3 == 0:
                try:
                    oracle.challenge(challenger)
                    for r in honest:
                        try:
                            eff = oracle.min_stake * oracle._stake_multiplier()
                            oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=eff)
                        except ValueError:
                            pass
                except ValueError:
                    pass
            oracle.settle(true_price=PRICE)

            liar_capital_history.append(sum(r.stake for r in liars))
            # Honest capital = liquid + escrowed (escrow is still theirs)
            honest_liquid = sum(r.stake for r in honest)
            honest_capital_history.append(honest_liquid)

        # Liar capital should decline over time
        assert liar_capital_history[-1] < liar_capital_history[0], (
            f"Liar capital should decline: {liar_capital_history[0]:.0f} -> "
            f"{liar_capital_history[-1]:.0f}")

        # Liars should be net negative
        liar_net = sum(r.earnings - r.losses for r in liars)
        assert liar_net < 0, f"Liars should be net negative: {liar_net:.1f}"

        # Honest reporters should have gained capital from liar losses
        honest_net = sum(r.earnings - r.losses for r in honest)
        assert honest_net > 0, f"Honest should be net positive: {honest_net:.1f}"

        log.info("Attrition: liar capital %.0f -> %.0f (net %.0f), "
                 "honest net %.0f",
                 liar_capital_history[0], liar_capital_history[-1],
                 liar_net, honest_net)

    def test_tipping_point_single_challenge_flips_outcome(self):
        """A single well-timed challenge with P floor can flip the outcome.

        Without challenge: liars control, earn money.
        With challenge + P floor: honest reporters gain meaningful K,
        estimate corrects, and liars lose their round stakes.
        """
        def run_scenario(do_challenge):
            oracle = EscrowOracle(
                initial_estimate=PRICE, min_stake=2.0, tolerance=0.05,
                escrow_fraction=0.25, escrow_window=30,
                challenge_P_floor=0.01,
            )
            honest = []
            for i in range(3):
                r = Reporter(f"h_{i}", stake=5000)
                oracle.register(r)
                honest.append(r)
            liars = []
            for i in range(5):
                r = Reporter(f"liar_{i}", stake=5000)
                oracle.register(r)
                liars.append(r)

            # 15 rounds of liar control
            for rid in range(15):
                oracle.open_round()
                for r in honest:
                    oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
                for r in liars:
                    oracle.submit(r, PRICE * 1.08, stake=2.0)
                oracle.settle(true_price=PRICE)

            # Now add honest newcomers + optional challenge
            new_honest = []
            for i in range(5):
                r = Reporter(f"new_{i}", stake=5000)
                oracle.register(r)
                new_honest.append(r)
            challenger = Reporter("ch", stake=5000)
            oracle.register(challenger)

            oracle.open_round()
            for r in honest + new_honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
            for r in liars:
                oracle.submit(r, PRICE * 1.08, stake=2.0)

            if do_challenge:
                oracle.challenge(challenger)
                for r in honest + new_honest:
                    try:
                        eff = oracle.min_stake * oracle._stake_multiplier()
                        oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=eff)
                    except ValueError:
                        pass

            result = oracle.settle(true_price=PRICE)
            return result["estimate_error"]

        err_no_challenge = run_scenario(do_challenge=False)
        err_with_challenge = run_scenario(do_challenge=True)

        # Without challenge, estimate stays biased (newcomers have no influence)
        assert err_no_challenge > 0.03, (
            f"Without challenge, error should remain high: {err_no_challenge:.4f}")

        # With challenge + P floor, estimate corrects dramatically
        assert err_with_challenge < err_no_challenge, (
            f"Challenge should reduce error: {err_with_challenge:.4f} >= {err_no_challenge:.4f}")

        log.info("Tipping point: no challenge err=%.2f%%, with challenge err=%.2f%%",
                 err_no_challenge * 100, err_with_challenge * 100)
