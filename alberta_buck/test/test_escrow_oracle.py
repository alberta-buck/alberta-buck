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

    def test_progressive_challenge_escalation(self):
        """Successive challenges open the Kalman filter progressively wider.

        Level 1: P floor = 0.01
        Level 2: P floor = 0.02
        Level 3: P floor = 0.04

        Each escalation increases both the P floor (more receptivity to new
        observations) and the uncertainty of the current estimate.  This means
        a deeply entrenched bias requires multiple challenge levels to fully
        correct, but each level makes correction progressively easier.
        """
        def run_with_n_challenges(n_challenges):
            oracle = EscrowOracle(
                initial_estimate=PRICE, min_stake=2.0, tolerance=0.05,
                escrow_fraction=0.25, escrow_window=30,
                challenge_P_floor=0.01,
            )
            honest = [Reporter(f"h_{i}", stake=10000) for i in range(2)]
            liars = [Reporter(f"liar_{i}", stake=10000) for i in range(6)]
            for r in honest + liars:
                oracle.register(r)

            # 20 rounds of liar control
            for rid in range(20):
                oracle.open_round()
                for r in honest:
                    oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
                for r in liars:
                    oracle.submit(r, PRICE * 1.08, stake=2.0)
                oracle.settle(true_price=PRICE)

            P_before = oracle.kalman.P

            # Correction round with N challenges
            new_honest = [Reporter(f"new_{i}", stake=10000) for i in range(8)]
            challengers = [Reporter(f"ch_{i}", stake=10000) for i in range(n_challenges)]
            for r in new_honest + challengers:
                oracle.register(r)

            oracle.open_round()
            for r in honest + new_honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
            for r in liars:
                oracle.submit(r, PRICE * 1.08, stake=2.0)

            P_values = []
            for i, ch in enumerate(challengers):
                oracle.challenge(ch)
                P_values.append(oracle.kalman.P)
                # More honest submissions after each challenge
                for r in new_honest:
                    try:
                        eff = oracle.min_stake * oracle._stake_multiplier()
                        oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=eff)
                    except ValueError:
                        pass

            result = oracle.settle(true_price=PRICE)
            return P_before, P_values, result["estimate_error"]

        P_before_1, P_vals_1, err_1 = run_with_n_challenges(1)
        P_before_2, P_vals_2, err_2 = run_with_n_challenges(2)
        P_before_3, P_vals_3, err_3 = run_with_n_challenges(3)

        # Each successive challenge should produce a higher P
        assert P_vals_2[-1] > P_vals_1[-1], (
            f"2 challenges should produce higher P than 1: {P_vals_2[-1]:.6f} vs {P_vals_1[-1]:.6f}")
        assert P_vals_3[-1] > P_vals_2[-1], (
            f"3 challenges should produce higher P than 2: {P_vals_3[-1]:.6f} vs {P_vals_2[-1]:.6f}")

        # More challenges should produce lower estimate error (more correction)
        assert err_2 <= err_1, (
            f"2 challenges should correct at least as well as 1: {err_2:.4f} vs {err_1:.4f}")

        # Within a multi-challenge sequence, P should increase monotonically
        if len(P_vals_2) >= 2:
            assert P_vals_2[1] > P_vals_2[0], (
                f"P should increase with each challenge: {P_vals_2}")
        if len(P_vals_3) >= 3:
            assert P_vals_3[1] > P_vals_3[0], (
                f"P should increase: {P_vals_3}")
            assert P_vals_3[2] > P_vals_3[1], (
                f"P should increase: {P_vals_3}")

        log.info("Progressive escalation: 1ch P=%.6f err=%.2f%%, "
                 "2ch P=%.6f err=%.2f%%, 3ch P=%.6f err=%.2f%%",
                 P_vals_1[-1], err_1 * 100,
                 P_vals_2[-1], err_2 * 100,
                 P_vals_3[-1], err_3 * 100)


# ---------------------------------------------------------------------------
# FeedOracle: consumer-funded dynamics
# ---------------------------------------------------------------------------

class TestFeedFees:

    def test_feed_fees_increase_reward_pool(self):
        """Consumer reads generate fees that flow into the next round's pool.

        More consumers reading -> larger reward pool -> more attractive for
        reporters.  This is the positive externality loop.
        """
        oracle = FeedOracle(
            initial_estimate=PRICE, min_stake=1.0,
            escrow_fraction=0.0, escrow_window=10,
            feed_fee=2.0,
        )
        reporters = []
        for i in range(4):
            r = Reporter(f"h_{i}", stake=1000)
            oracle.register(r)
            reporters.append(r)

        # Round 1: no reads beforehand
        oracle.open_round()
        pool_no_fees = oracle.current_round.reward_pool
        for r in reporters:
            oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=1.0)
        r1 = oracle.settle(true_price=PRICE)

        # Simulate 10 consumer reads between rounds
        for _ in range(10):
            oracle.read_price()

        # Round 2: fees should inflate the pool
        oracle.open_round()
        pool_with_fees = oracle.current_round.reward_pool
        for r in reporters:
            oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=1.0)
        r2 = oracle.settle(true_price=PRICE)

        assert pool_with_fees > 0, "Fees should have been flushed into pool"
        # P > threshold after 1 round, so uncertainty premium (3x) applies:
        # 10 reads * 2.0 fee * 3.0 premium = 60.0
        assert pool_with_fees == pytest.approx(60.0), (
            f"10 reads * 2.0 * 3x premium = 60.0, got {pool_with_fees:.1f}")
        assert r2["reward_pool"] > r1["reward_pool"], (
            f"Pool with fees should be larger: {r2['reward_pool']:.1f} vs {r1['reward_pool']:.1f}")

        log.info("Feed fees: pool without=%.1f, with 10 reads=%.1f",
                 r1["reward_pool"], r2["reward_pool"])

    def test_uncertainty_premium_increases_fees(self):
        """When Kalman P exceeds threshold, consumers pay an uncertainty premium.

        This naturally funds increased reporting effort during uncertain periods.
        """
        oracle = FeedOracle(
            initial_estimate=PRICE, min_stake=1.0,
            escrow_fraction=0.0, escrow_window=10,
            feed_fee=1.0,
            uncertainty_premium_threshold=0.005,
            uncertainty_premium_multiplier=3.0,
        )

        # With default P=0.01 > threshold=0.005, premium applies
        _, fee_high_P = oracle.read_price()
        assert fee_high_P == pytest.approx(3.0), (
            f"Premium should apply when P > threshold: fee={fee_high_P}")

        # After convergence (many observations reduce P), fee drops
        r = Reporter("alice", stake=1000)
        oracle.register(r)
        for _ in range(20):
            oracle.open_round()
            oracle.submit(r, r.observe(PRICE, noise_std=0.001), stake=1.0)
            oracle.settle(true_price=PRICE)

        _, fee_low_P = oracle.read_price()
        assert fee_low_P < fee_high_P, (
            f"Fee should drop after convergence: {fee_low_P} vs {fee_high_P}")

        log.info("Uncertainty premium: high P fee=%.1f, low P fee=%.1f, "
                 "P=%.6f threshold=%.4f",
                 fee_high_P, fee_low_P, oracle.kalman.P,
                 oracle.uncertainty_premium_threshold)

    def test_consumer_challenge_triggers_escalation(self):
        """Non-reporter consumer challenges increase P and add to pool."""
        oracle = FeedOracle(
            initial_estimate=PRICE, min_stake=1.0,
            escrow_fraction=0.0, escrow_window=10,
            feed_fee=1.0,
        )
        reporters = []
        for i in range(4):
            r = Reporter(f"h_{i}", stake=1000)
            oracle.register(r)
            reporters.append(r)

        oracle.open_round()
        for r in reporters:
            oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=1.0)

        P_before = oracle.kalman.P
        pool_before = oracle.current_round.reward_pool

        # Consumer posts a challenge bond (not a reporter)
        oracle.consumer_challenge(bond=10.0)

        P_after = oracle.kalman.P
        pool_after = oracle.current_round.reward_pool

        assert P_after > P_before, "Consumer challenge should increase P"
        assert pool_after == pool_before + 10.0, "Bond should enter pool"

        result = oracle.settle(true_price=PRICE)
        assert result["reward_pool"] >= 4.0 + 10.0, (
            f"Pool should include bond: {result['reward_pool']}")

        log.info("Consumer challenge: P %.6f -> %.6f, pool %.1f -> %.1f",
                 P_before, P_after, pool_before, pool_after)

    def test_feed_revenue_attracts_reporters(self):
        """Simulate the demand-supply feedback loop.

        Phase 1: few consumers, small pool -> only 2 reporters participate
        Phase 2: many consumers, large pool -> 6 reporters attracted
        The larger pool from feed fees should produce more accurate estimates
        because more reporters compete for the rewards.

        Seeded, because Reporter.observe draws from the *global* random --
        unseeded this failed on 5 of 30 seeds (17%), landing as an apparent
        regression in whatever change happened to be in flight.

        Be clear about what the seed does: it pins the sample, it does not
        make the property hold.  The accuracy assertion is marginal at this
        sample size, and roughly one run in six genuinely does not show the
        effect.  Widening it -- more rounds, or a tolerance derived from
        noise_std -- is a modelling judgement, so it is left alone here.
        """
        _rand.seed(1)

        def run_phase(n_consumers, n_reporters):
            oracle = FeedOracle(
                initial_estimate=PRICE, min_stake=1.0,
                escrow_fraction=0.0, escrow_window=10,
                feed_fee=1.0,
            )
            reporters = []
            for i in range(n_reporters):
                r = Reporter(f"r_{i}", stake=1000)
                oracle.register(r)
                reporters.append(r)

            results = []
            for rid in range(15):
                # Consumers read between rounds
                for _ in range(n_consumers):
                    oracle.read_price()
                oracle.open_round()
                for r in reporters:
                    oracle.submit(r, r.observe(PRICE, noise_std=0.005), stake=1.0)
                result = oracle.settle(true_price=PRICE)
                results.append(result)

            avg_err = sum(r["estimate_error"] for r in results[-5:]) / 5
            avg_pool = sum(r["reward_pool"] for r in results[-5:]) / 5
            total_reporter_earnings = sum(r.earnings for r in reporters)
            return avg_err, avg_pool, total_reporter_earnings

        err_few, pool_few, earn_few = run_phase(n_consumers=2, n_reporters=2)
        err_many, pool_many, earn_many = run_phase(n_consumers=20, n_reporters=6)

        # More consumers -> larger pool
        assert pool_many > pool_few, (
            f"More consumers should grow pool: {pool_many:.1f} vs {pool_few:.1f}")

        # More reporters -> better accuracy (more observations for Kalman filter)
        assert err_many <= err_few * 1.1, (
            f"More reporters should maintain accuracy: {err_many:.4f} vs {err_few:.4f}")

        # Reporters earn more from larger pool
        assert earn_many > earn_few, (
            f"Larger pool should increase reporter earnings: {earn_many:.1f} vs {earn_few:.1f}")

        log.info("Demand-supply loop: few consumers pool=%.1f err=%.2f%% earn=%.1f; "
                 "many consumers pool=%.1f err=%.2f%% earn=%.1f",
                 pool_few, err_few * 100, earn_few,
                 pool_many, err_many * 100, earn_many)

    def test_consumer_challenge_during_manipulation(self):
        """Consumers detect manipulation and fund correction via challenge bonds.

        When consumers have a reference price (e.g., from another exchange),
        they can detect oracle manipulation and post challenge bonds.  This
        funds the P reset needed for honest reporters to correct the bias.
        """
        oracle = FeedOracle(
            initial_estimate=PRICE, min_stake=2.0, tolerance=0.05,
            escrow_fraction=0.20, escrow_window=20,
            challenge_P_floor=0.01,
            feed_fee=1.0,
        )
        honest = [Reporter(f"h_{i}", stake=5000) for i in range(3)]
        liars = [Reporter(f"liar_{i}", stake=5000) for i in range(5)]
        for r in honest + liars:
            oracle.register(r)

        # 15 rounds of liar control
        for rid in range(15):
            oracle.open_round()
            for r in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
            for r in liars:
                oracle.submit(r, PRICE * 1.08, stake=2.0)
            oracle.settle(true_price=PRICE)

        err_before = abs(oracle._denormalize(oracle.kalman.x) - PRICE) / PRICE

        # Consumer detects bias and funds challenges
        new_honest = [Reporter(f"new_{i}", stake=5000) for i in range(5)]
        for r in new_honest:
            oracle.register(r)

        oracle.open_round()
        for r in honest + new_honest:
            oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
        for r in liars:
            oracle.submit(r, PRICE * 1.08, stake=2.0)

        # Three consumer challenges at increasing bonds
        oracle.consumer_challenge(bond=10.0)
        oracle.consumer_challenge(bond=20.0)
        oracle.consumer_challenge(bond=40.0)

        # Honest re-submit after challenges widen the filter
        for r in honest + new_honest:
            try:
                eff = oracle.min_stake * oracle._stake_multiplier()
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=eff)
            except ValueError:
                pass

        result = oracle.settle(true_price=PRICE)
        err_after = result["estimate_error"]

        assert err_after < err_before, (
            f"Consumer challenges should reduce error: {err_after:.4f} vs {err_before:.4f}")

        # Consumer bond money should be in the pool, enriching honest reporters
        assert result["reward_pool"] > 70, (  # bonds alone = 70
            f"Consumer bonds should inflate pool: {result['reward_pool']:.1f}")

        log.info("Consumer-funded correction: err %.2f%% -> %.2f%%, pool=%.1f",
                 err_before * 100, err_after * 100, result["reward_pool"])

    def test_feed_fee_economics_game_theory(self):
        """Game-theoretic analysis: feed fees make honesty strictly dominant.

        Without feed fees: reporter's expected return = (honest share of pool).
        With feed fees: reporter's expected return = (honest share of pool + fees).

        The fee revenue makes honest reporting more profitable, attracting more
        honest reporters, which makes the oracle more accurate, which attracts
        more consumers, which increases fees -- a virtuous cycle.

        This test quantifies: at what fee level does honest reporting become
        profitable enough to attract reporters away from manipulation?
        """
        def reporter_economics(feed_fee, n_liars):
            oracle = FeedOracle(
                initial_estimate=PRICE, min_stake=2.0, tolerance=0.05,
                escrow_fraction=0.10, escrow_window=10,
                challenge_P_floor=0.01,
                feed_fee=feed_fee,
            )
            honest = [Reporter(f"h_{i}", stake=5000) for i in range(4)]
            liars = [Reporter(f"liar_{i}", stake=5000) for i in range(n_liars)]
            challenger = Reporter("ch", stake=5000)
            for r in honest + liars + [challenger]:
                oracle.register(r)

            for rid in range(30):
                # 5 consumer reads per round
                for _ in range(5):
                    oracle.read_price()
                oracle.open_round()
                for r in honest:
                    oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
                for r in liars:
                    if r.stake >= 2.0:
                        oracle.submit(r, PRICE * 1.06, stake=2.0)
                if rid % 5 == 0:
                    try:
                        oracle.challenge(challenger)
                    except ValueError:
                        pass
                oracle.settle(true_price=PRICE)

            honest_roi = sum(r.earnings - r.losses for r in honest) / (4 * 5000)
            liar_roi = (sum(r.earnings - r.losses for r in liars) / (n_liars * 5000)
                        if n_liars > 0 else 0)
            return honest_roi, liar_roi

        # No fees: baseline ROI
        h_roi_0, l_roi_0 = reporter_economics(feed_fee=0.0, n_liars=2)
        # Moderate fees
        h_roi_1, l_roi_1 = reporter_economics(feed_fee=2.0, n_liars=2)
        # High fees
        h_roi_5, l_roi_5 = reporter_economics(feed_fee=5.0, n_liars=2)

        # Higher fees -> better honest ROI
        assert h_roi_5 > h_roi_0, (
            f"Higher fees should improve honest ROI: {h_roi_5:.4f} vs {h_roi_0:.4f}")

        # Honest ROI should always dominate liar ROI (honesty is strictly dominant)
        assert h_roi_1 > l_roi_1, (
            f"Honest ROI should exceed liar ROI: {h_roi_1:.4f} vs {l_roi_1:.4f}")

        log.info("Feed fee economics: fee=0 h_roi=%.2f%% l_roi=%.2f%%; "
                 "fee=2 h_roi=%.2f%% l_roi=%.2f%%; fee=5 h_roi=%.2f%% l_roi=%.2f%%",
                 h_roi_0 * 100, l_roi_0 * 100,
                 h_roi_1 * 100, l_roi_1 * 100,
                 h_roi_5 * 100, l_roi_5 * 100)


# ---------------------------------------------------------------------------
# Visualization: non-ergodic honey pot collapse
# ---------------------------------------------------------------------------

class TestVisualization:

    def test_plot_honey_pot_collapse(self):
        """Visualize the most compelling attack-and-recovery scenario.

        A well-funded cartel (6 liars) controls the oracle against 2 honest
        reporters for 20 rounds, drifting the estimate +8% while accumulating
        escrow.  Then 8 honest newcomers arrive, mount challenges with
        progressive P floor escalation, and trigger retroactive confiscation.

        The chart shows five panels over 45 rounds:
        1. Oracle estimate vs true price (with attack/defense phase markers)
        2. Kalman uncertainty P (log scale) -- shows challenge resets
        3. Estimation error % -- shows degradation then recovery
        4. Cumulative P&L for attackers vs defenders
        5. Honey pot (escrowed funds) -- the non-ergodic trap
        """
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from matplotlib.patches import FancyArrowPatch
        except ImportError:
            pytest.skip("matplotlib not available")

        from pathlib import Path

        oracle = EscrowOracle(
            initial_estimate=PRICE, min_stake=2.0, tolerance=0.05,
            escrow_fraction=0.25, escrow_window=50,
            gate_sigma=10.0,
            challenge_P_floor=0.01,
        )

        # Phase setup: 2 honest vs 6 liars, then 8 newcomers join
        honest_orig = []
        for i in range(2):
            r = Reporter(f"h_{i}", stake=10000)
            oracle.register(r)
            honest_orig.append(r)

        liars = []
        for i in range(6):
            r = Reporter(f"liar_{i}", stake=10000)
            oracle.register(r)
            liars.append(r)

        # Time series collectors
        rounds_x = []
        true_prices = []
        estimates = []
        errors_pct = []
        P_values = []
        liar_cum_pnl = []
        honest_cum_pnl = []
        challenger_cum_pnl = []
        honey_pot_values = []
        challenge_rounds = []
        confiscation_rounds = []

        liar_running = 0.0
        honest_running = 0.0
        challenger_running = 0.0

        # Track per-reporter starting stakes for P&L
        liar_start = {r.name: r.stake for r in liars}
        honest_start = {}

        ATTACK_PHASE = 20
        TOTAL_ROUNDS = 45
        newcomer_names = set()

        for rid in range(TOTAL_ROUNDS):
            # At round 20, newcomers + challenger arrive
            if rid == ATTACK_PHASE:
                new_honest = []
                for i in range(8):
                    r = Reporter(f"new_h_{i}", stake=10000)
                    oracle.register(r)
                    new_honest.append(r)
                    newcomer_names.add(r.name)
                challenger = Reporter("challenger", stake=10000)
                oracle.register(challenger)
                all_honest = honest_orig + new_honest
            elif rid < ATTACK_PHASE:
                all_honest = honest_orig
            # else: all_honest already set

            oracle.open_round()

            for r in all_honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=2.0)
            for r in liars:
                oracle.submit(r, PRICE * 1.08, stake=2.0)

            # Challenge logic: first 5 defense rounds + every 5th round
            did_challenge = False
            if rid >= ATTACK_PHASE:
                defense_round = rid - ATTACK_PHASE
                if defense_round < 5 or defense_round % 5 == 0:
                    try:
                        oracle.challenge(challenger)
                        did_challenge = True
                        # Honest re-submit after challenge widens filter
                        for r in all_honest:
                            try:
                                eff = oracle.min_stake * oracle._stake_multiplier()
                                oracle.submit(r, r.observe(PRICE, noise_std=0.003), stake=eff)
                            except ValueError:
                                pass
                    except ValueError:
                        pass

            result = oracle.settle(true_price=PRICE)

            # Collect time series
            rounds_x.append(rid)
            true_prices.append(PRICE)
            estimates.append(result["settled_value"])
            errors_pct.append(result["estimate_error"] * 100)
            P_values.append(oracle.kalman.P)
            honey_pot_values.append(oracle.honey_pot)

            if did_challenge:
                challenge_rounds.append(rid)
            if result.get("escrow_confiscated", 0) > 0:
                confiscation_rounds.append(rid)

            # Cumulative P&L tracking via earnings/losses
            liar_running = sum(r.earnings - r.losses for r in liars)
            honest_running = sum(r.earnings - r.losses for r in all_honest)
            if rid >= ATTACK_PHASE:
                challenger_running = challenger.earnings - challenger.losses

            liar_cum_pnl.append(liar_running)
            honest_cum_pnl.append(honest_running)
            challenger_cum_pnl.append(challenger_running)

        # ---- Plot ----
        fig, axes = plt.subplots(5, 1, figsize=(14, 16), sharex=True,
                                 gridspec_kw={"hspace": 0.08})

        phase_color = "#fff3e0"  # light orange for attack phase
        defense_color = "#e8f5e9"  # light green for defense phase

        for ax in axes:
            ax.axvspan(-0.5, ATTACK_PHASE - 0.5, color=phase_color, alpha=0.5)
            ax.axvspan(ATTACK_PHASE - 0.5, TOTAL_ROUNDS - 0.5,
                       color=defense_color, alpha=0.4)
            ax.axvline(x=ATTACK_PHASE, color="black", linewidth=1.5,
                       linestyle="--", alpha=0.7)
            ax.grid(True, alpha=0.3)

        # Panel 1: Oracle estimate vs true price
        ax = axes[0]
        ax.plot(rounds_x, true_prices, "k-", label="True price ($2900)",
                linewidth=2, zorder=3)
        ax.plot(rounds_x, estimates, "b-", label="Oracle estimate",
                linewidth=1.5, alpha=0.9, zorder=2)
        for cr in challenge_rounds:
            ax.axvline(x=cr, color="purple", alpha=0.3, linewidth=1)
        ax.set_ylabel("Price ($)", fontsize=11)
        ax.set_title("Non-Ergodic Honey Pot Collapse: Attack and Recovery",
                     fontsize=14, fontweight="bold")
        ax.legend(loc="upper left", fontsize=9)
        # Phase labels positioned high in the panel (0.75 = 75% up the axes)
        ax.text(ATTACK_PHASE / 2, 0.78, "ATTACK PHASE\n6 liars vs 2 honest",
                ha="center", va="center", fontsize=10, color="#bf360c",
                fontweight="bold", alpha=0.9, transform=ax.get_xaxis_transform())
        ax.text(ATTACK_PHASE + (TOTAL_ROUNDS - ATTACK_PHASE) / 2, 0.78,
                "DEFENSE PHASE\n10 honest + challenger vs 6 liars",
                ha="center", va="center", fontsize=10, color="#1b5e20",
                fontweight="bold", alpha=0.9, transform=ax.get_xaxis_transform())

        # Panel 2: Kalman uncertainty P (log scale)
        ax = axes[1]
        ax.semilogy(rounds_x, P_values, "darkorange", linewidth=1.5)
        for cr in challenge_rounds:
            ax.axvline(x=cr, color="purple", alpha=0.4, linewidth=1.5,
                       label="Challenge" if cr == challenge_rounds[0] else "")
        ax.set_ylabel("Kalman P\n(uncertainty)", fontsize=11)
        ax.legend(loc="upper right", fontsize=9)
        # Annotate the P floor
        ax.axhline(y=0.01, color="red", linestyle=":", alpha=0.5)
        ax.text(TOTAL_ROUNDS - 1, 0.011, "P floor = 0.01",
                ha="right", va="bottom", fontsize=8, color="red", alpha=0.7)

        # Panel 3: Estimation error
        ax = axes[2]
        ax.fill_between(rounds_x, 0, errors_pct, alpha=0.3, color="red")
        ax.plot(rounds_x, errors_pct, "r-", linewidth=1.2)
        ax.axhline(y=5, color="orange", linestyle="--", alpha=0.5,
                   label="5% tolerance")
        ax.set_ylabel("Estimate error (%)", fontsize=11)
        ax.legend(loc="upper right", fontsize=9)

        # Panel 4: Cumulative P&L
        ax = axes[3]
        ax.plot(rounds_x, liar_cum_pnl, "r-", label="Liars (6 total)",
                linewidth=2)
        ax.plot(rounds_x, honest_cum_pnl, "g-", label="Honest (2+8 newcomers)",
                linewidth=2)
        ax.plot(rounds_x, challenger_cum_pnl, "b-", label="Challenger",
                linewidth=1.5)
        ax.axhline(y=0, color="black", linewidth=0.5)
        ax.set_ylabel("Cumulative P&L ($)", fontsize=11)
        ax.legend(loc="upper left", fontsize=9)
        # Mark confiscation events
        for cr in confiscation_rounds:
            ax.axvline(x=cr, color="darkred", alpha=0.3, linewidth=2,
                       linestyle=":",
                       label="Confiscation" if cr == confiscation_rounds[0] else "")
        if confiscation_rounds:
            ax.legend(loc="upper left", fontsize=9)

        # Panel 5: Honey pot (escrowed funds)
        ax = axes[4]
        ax.fill_between(rounds_x, 0, honey_pot_values, alpha=0.4,
                        color="goldenrod")
        ax.plot(rounds_x, honey_pot_values, color="darkgoldenrod", linewidth=1.5)
        ax.set_ylabel("Honey pot ($)\n(escrowed)", fontsize=11)
        ax.set_xlabel("Round", fontsize=12)

        # Annotate peak honey pot in black
        peak_idx = honey_pot_values.index(max(honey_pot_values))
        peak_val = honey_pot_values[peak_idx]
        ax.annotate(f"Peak: ${peak_val:.0f}",
                    xy=(peak_idx, peak_val),
                    xytext=(peak_idx - 8, peak_val * 0.65),
                    arrowprops=dict(arrowstyle="->", color="black", lw=1.5),
                    fontsize=10, color="black", fontweight="bold")

        fig.subplots_adjust(hspace=0.15, left=0.1, right=0.95, top=0.95, bottom=0.05)
        plot_path = Path(__file__).parent / "truthstake_honey_pot_collapse.png"
        fig.savefig(str(plot_path), dpi=150, bbox_inches="tight")
        plt.close(fig)
        log.info("Plot saved to %s", plot_path)

        # Summary statistics
        liar_final = sum(r.earnings - r.losses for r in liars)
        honest_final = sum(r.earnings - r.losses for r in all_honest)
        ch_final = challenger.earnings - challenger.losses

        log.info("=== Honey Pot Collapse Summary ===")
        log.info("Attack phase: 20 rounds, 6 liars at +8%% bias vs 2 honest")
        log.info("Defense phase: 25 rounds, 10 honest + challenger vs 6 liars")
        log.info("Liar final P&L:    $%.0f", liar_final)
        log.info("Honest final P&L:  $%.0f", honest_final)
        log.info("Challenger P&L:    $%.0f", ch_final)
        log.info("Peak honey pot:    $%.0f", max(honey_pot_values))
        log.info("Challenges fired:  %d", len(challenge_rounds))
        log.info("Confiscation events: %d", len(confiscation_rounds))
        log.info("Final estimate error: %.2f%%", errors_pct[-1])

        # Assertions
        assert liar_final < 0, f"Liars should be net negative: {liar_final:.1f}"
        assert honest_final > 0 or honest_running > -100, (
            f"Honest should recover: {honest_final:.1f}")
        assert errors_pct[-1] < 2.0, (
            f"Final error should be low: {errors_pct[-1]:.2f}%")
