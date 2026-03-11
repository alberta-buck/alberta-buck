"""TruthStake oracle simulation tests.

Validates the Kalman-filter oracle with reputation-weighted submissions,
early-reporter bonus, and challenge escalation.  Each test demonstrates a
specific game-theoretic property of the mechanism.
"""

import logging
import math

import pytest

from alberta_buck.truthstake import (
    KalmanOracle,
    Oracle,
    Reporter,
    run_simulation,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

PRICE = 2900.0   # Gold-ish reference price


# ---------------------------------------------------------------------------
# Unit: Kalman filter basics
# ---------------------------------------------------------------------------

class TestKalmanOracle:

    def test_converges_to_true_value(self):
        """Low-noise observations pull estimate toward the true value."""
        kf = KalmanOracle(estimate=0.5, P=1.0, Q=0.0)
        for _ in range(50):
            kf.update(z=1.0, R=0.01)
        assert abs(kf.x - 1.0) < 0.001
        assert kf.P < 0.001

    def test_low_R_has_more_influence(self):
        """A low-R observation moves the estimate more than a high-R one."""
        kf1 = KalmanOracle(estimate=1.0, P=0.01, Q=0.0)
        K_low = kf1.update(z=1.1, R=0.001)    # trusted reporter

        kf2 = KalmanOracle(estimate=1.0, P=0.01, Q=0.0)
        K_high = kf2.update(z=1.1, R=1.0)     # untrusted reporter

        assert K_low > K_high
        assert kf1.x > kf2.x  # low-R moved estimate further

    def test_predict_increases_uncertainty(self):
        """Time propagation grows P by Q."""
        kf = KalmanOracle(estimate=1.0, P=0.01, Q=0.005)
        kf.predict()
        assert kf.P == pytest.approx(0.015)
        assert kf.x == pytest.approx(1.0)  # estimate unchanged


# ---------------------------------------------------------------------------
# Unit: Reporter reputation
# ---------------------------------------------------------------------------

class TestReporter:

    def test_new_reporter_high_R(self):
        """New reporters (< 3 settlements) have high R."""
        r = Reporter("new", stake=100)
        assert r.R == 1.0
        assert r.n_settled == 0

    def test_R_decreases_with_accuracy(self):
        """After settling with low error, R decreases."""
        r = Reporter("good", stake=100)
        for _ in range(5):
            r.ema_sq_error = 0.3 * 0.000025 + 0.7 * r.ema_sq_error
            r.n_settled += 1
        assert r.R < 0.5  # much lower than the initial 1.0


# ---------------------------------------------------------------------------
# Integration: honest reporters converge
# ---------------------------------------------------------------------------

class TestHonestConvergence:

    def test_stable_price(self):
        """5 honest reporters, stable price -- estimate stays accurate."""
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0)
        reporters = []
        for i in range(5):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            reporters.append((r, 0.005, 0.0))  # 0.5% noise, no bias

        results = run_simulation(oracle, reporters, n_rounds=30,
                                 true_price_fn=lambda _: PRICE, seed=42)

        # Final estimate within 1% of true price
        final = results[-1]
        assert final["estimate_error"] < 0.01, f"Error {final['estimate_error']:.4f} > 1%"
        # All reporters should not have significant losses
        for r, _, _ in reporters:
            net = r.earnings - r.losses
            log.info("  %s net=%.2f", r.name, net)

    def test_random_walk(self):
        """Estimate tracks a drifting price."""
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0, Q=0.001)
        reporters = []
        for i in range(5):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            reporters.append((r, 0.005, 0.0))

        results = run_simulation(oracle, reporters, n_rounds=50,
                                 initial_price=PRICE, price_drift=0.005, seed=123)

        # Average error over last 20 rounds should be < 2%
        late_errors = [r["estimate_error"] for r in results[-20:]]
        avg_err = sum(late_errors) / len(late_errors)
        assert avg_err < 0.02, f"Average late error {avg_err:.4f} > 2%"


# ---------------------------------------------------------------------------
# Single manipulator loses stake
# ---------------------------------------------------------------------------

class TestSingleManipulator:

    def test_manipulator_loses(self):
        """One adversary among 4 honest reporters loses stake, estimate unaffected."""
        oracle = Oracle(initial_estimate=PRICE, min_stake=2.0)
        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=500)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        adversary = Reporter("adversary", stake=500)
        oracle.register(adversary)
        all_reporters = honest + [(adversary, 0.0, 0.10)]  # +10% bias

        results = run_simulation(oracle, all_reporters, n_rounds=20,
                                 true_price_fn=lambda _: PRICE, seed=99)

        # Estimate stays within 3% (allow warmup slack)
        for result in results[-10:]:
            assert result["estimate_error"] < 0.03, (
                f"Round {result['round']} error {result['estimate_error']:.4f}")

        # Adversary lost more than they earned
        assert adversary.losses > adversary.earnings, (
            f"Adversary should be net loser: earned {adversary.earnings:.1f}, "
            f"lost {adversary.losses:.1f}")

        # Honest reporters profited collectively
        honest_net = sum(r.earnings - r.losses for r, _, _ in honest)
        assert honest_net > 0, f"Honest reporters should profit collectively: {honest_net:.1f}"

        log.info("Adversary final: %s", adversary)
        for r, _, _ in honest:
            log.info("Honest final: %s", r)


# ---------------------------------------------------------------------------
# Coordinated manipulation defeated by challenge
# ---------------------------------------------------------------------------

class TestCoordinatedManipulation:

    def test_challenge_defeats_cartel(self):
        """3 adversaries vs 3 honest + 1 challenger.  Challenge restores truth."""
        oracle = Oracle(initial_estimate=PRICE, min_stake=2.0, tolerance=0.05)
        honest = []
        for i in range(3):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        cartel = []
        for i in range(3):
            r = Reporter(f"cartel_{i}", stake=1000)
            oracle.register(r)
            cartel.append((r, 0.0, 0.15))  # +15% bias

        challenger = Reporter("challenger", stake=1000)
        oracle.register(challenger)
        all_reporters = honest + cartel + [(challenger, 0.005, 0.0)]

        def challenge_when_outlier(oracle_inst, round_id, submissions):
            if not submissions or round_id < 2:
                return None
            est = oracle_inst.kalman.x
            P = oracle_inst.kalman.P
            if P <= 0:
                return None
            sigma = math.sqrt(P)
            for sub in submissions:
                # Check relative deviation
                norm_val = oracle_inst._normalize(sub.value)
                if sigma > 0 and abs(norm_val - est) > 3 * sigma:
                    return challenger
            return None

        results = run_simulation(oracle, all_reporters, n_rounds=15,
                                 true_price_fn=lambda _: PRICE, seed=77,
                                 challenge_fn=challenge_when_outlier)

        # Cartel members should be net losers
        cartel_net = sum(r.earnings - r.losses for r, _, _ in cartel)
        assert cartel_net < 0, f"Cartel should lose money: net {cartel_net:.1f}"

        # Later rounds: estimate should be within 5% despite cartel
        late_errors = [r["estimate_error"] for r in results[-5:]]
        avg_err = sum(late_errors) / len(late_errors)
        assert avg_err < 0.05, f"Late average error {avg_err:.4f} > 5%"

        log.info("Cartel net P&L: %.1f", cartel_net)
        for r, _, _ in cartel:
            log.info("Cartel: %s", r)


# ---------------------------------------------------------------------------
# Early accurate reporters earn the most
# ---------------------------------------------------------------------------

class TestEarlyBonus:

    def test_first_submitter_earns_more(self):
        """Among equally accurate reporters, earlier submitters earn more.

        The entire reward pool is redistributed weighted by submission order,
        so even in all-honest scenarios the first submitter gets a larger share.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0, early_halflife=2.0)

        reporters = []
        for i in range(5):
            r = Reporter(f"reporter_{i}", stake=1000)
            oracle.register(r)
            reporters.append(r)

        # Run 40 rounds with fixed submission order (reporter_0 always first)
        import random as _rand
        rng = _rand.Random(42)
        for rid in range(40):
            oracle.open_round()
            for idx, r in enumerate(reporters):
                noise = rng.gauss(0, 0.003)
                value = PRICE * (1.0 + noise)
                oracle.submit(r, value, stake=1.0)
            oracle.settle(true_price=PRICE)

        earnings = [(r.name, r.earnings - r.losses) for r in reporters]
        for name, net in earnings:
            log.info("  %s net=%.3f", name, net)

        # Reporter_0 (always first) should have earned more than reporter_4 (always last)
        first_net = reporters[0].earnings - reporters[0].losses
        last_net = reporters[-1].earnings - reporters[-1].losses
        assert first_net > last_net, (
            f"First submitter net ({first_net:.3f}) should exceed "
            f"last submitter net ({last_net:.3f})")


# ---------------------------------------------------------------------------
# Reputation degradation: honest-then-adversarial reporter
# ---------------------------------------------------------------------------

class TestReputationDegradation:

    def test_turned_adversary_loses_reputation(self):
        """Reporter starts honest, turns adversarial -- R increases and stake lost."""
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0)

        honest = []
        for i in range(4):
            r = Reporter(f"bg_{i}", stake=1000)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        turncoat = Reporter("turncoat", stake=1000)
        oracle.register(turncoat)

        def turncoat_strategy(round_id):
            if round_id < 10:
                return (0.005, 0.0)   # honest for first 10 rounds
            return (0.0, 0.12)        # then adversarial (+12% bias)

        all_reporters = honest + [(turncoat, 0.005, 0.0)]
        results = run_simulation(oracle, all_reporters, n_rounds=25,
                                 true_price_fn=lambda _: PRICE, seed=55,
                                 strategies={"turncoat": turncoat_strategy})

        # After adversarial phase, turncoat should have losses
        assert turncoat.losses > 0, f"Turncoat should have losses: {turncoat.losses:.1f}"

        # Their R should be elevated (less trusted) due to high error in later rounds
        assert turncoat.ema_sq_error > 0.001, (
            f"Turncoat ema_sq_error should be elevated: {turncoat.ema_sq_error:.6f}")

        log.info("Turncoat final: %s", turncoat)


# ---------------------------------------------------------------------------
# Wrong challenger loses bond
# ---------------------------------------------------------------------------

class TestFalseChallenge:

    def test_unjustified_challenge_costs_bond(self):
        """Challenging when there's no manipulation costs the challenger."""
        oracle = Oracle(initial_estimate=PRICE, min_stake=2.0)

        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=500)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        bad_challenger = Reporter("bad_challenger", stake=500)
        oracle.register(bad_challenger)

        def always_challenge(oracle_inst, round_id, submissions):
            return bad_challenger

        all_reporters = honest + [(bad_challenger, 0.005, 0.0)]
        results = run_simulation(oracle, all_reporters, n_rounds=10,
                                 true_price_fn=lambda _: PRICE, seed=33,
                                 challenge_fn=always_challenge)

        honest_avg_net = sum(r.earnings - r.losses for r, _, _ in honest) / len(honest)
        challenger_net = bad_challenger.earnings - bad_challenger.losses
        log.info("Honest avg net: %.1f, Bad challenger net: %.1f",
                 honest_avg_net, challenger_net)
        # Frivolous challenger should do worse than honest non-challengers
        assert challenger_net < honest_avg_net, (
            f"Frivolous challenger ({challenger_net:.1f}) should do worse "
            f"than honest non-challengers ({honest_avg_net:.1f})")


# ---------------------------------------------------------------------------
# Sybil attack: adversary creates many identities to overwhelm honest majority
# ---------------------------------------------------------------------------

class TestSybilAttack:

    def test_sybil_overwhelms_without_challenge(self):
        """10 sybil identities vs 3 honest: sybils drag the estimate.

        Without challenges, sybils gradually build reputation and their
        biased values eventually dominate.  The settled value drifts
        toward the sybil bias -- a real vulnerability that demonstrates
        why challenge escalation is essential.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=2.0)
        honest = []
        for i in range(3):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        sybils = []
        for i in range(10):
            r = Reporter(f"sybil_{i}", stake=200)
            oracle.register(r)
            sybils.append((r, 0.0, 0.08))  # +8% bias

        all_reporters = honest + sybils
        results = run_simulation(oracle, all_reporters, n_rounds=30,
                                 true_price_fn=lambda _: PRICE, seed=101)

        # The sybil attack SUCCEEDS without challenges: estimate drifts
        late_errors = [r["estimate_error"] for r in results[-5:]]
        avg_err = sum(late_errors) / len(late_errors)
        assert avg_err > 0.03, (
            f"Expected sybil attack to drag estimate, but avg error only {avg_err:.4f}")

        # Honest reporters become the "dishonest" ones relative to the biased
        # settled value, so they actually LOSE money.  This is the core problem.
        honest_net = sum(r.earnings - r.losses for r, _, _ in honest)
        log.info("Sybil unchallenged: estimate drifted %.1f%%, honest net=%.1f",
                 avg_err * 100, honest_net)

    def test_sybil_defeated_by_challenge_at_moderate_ratio(self):
        """5 sybils vs 4 honest + 1 challenger: challenges restore truth.

        At a moderate sybil ratio (5:5 including challenger), challenge
        escalation doubles Kalman P and allows honest re-submissions to
        restore the estimate.  Sybils pay 5x the stake cost and lose.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=2.0, tolerance=0.05)
        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        sybils = []
        for i in range(5):
            r = Reporter(f"sybil_{i}", stake=500)
            oracle.register(r)
            sybils.append((r, 0.0, 0.08))  # +8% bias

        challenger = Reporter("challenger", stake=1000)
        oracle.register(challenger)
        all_reporters = honest + sybils + [(challenger, 0.005, 0.0)]

        def challenge_outlier_cluster(oracle_inst, round_id, submissions):
            if round_id < 2:
                return None
            est = oracle_inst.kalman.x
            P = oracle_inst.kalman.P
            if P <= 0:
                return None
            sigma = math.sqrt(P)
            outliers = sum(
                1 for sub in submissions
                if abs(oracle_inst._normalize(sub.value) - est) > 2 * sigma
            )
            if outliers >= 2:
                return challenger
            return None

        results = run_simulation(oracle, all_reporters, n_rounds=30,
                                 true_price_fn=lambda _: PRICE, seed=101,
                                 challenge_fn=challenge_outlier_cluster)

        # With challenge at moderate ratio, estimate stays closer to truth
        late_errors = [r["estimate_error"] for r in results[-5:]]
        avg_err = sum(late_errors) / len(late_errors)
        assert avg_err < 0.05, (
            f"Challenge should contain sybil drift: avg error {avg_err:.4f}")

        # Sybils collectively lose money
        sybil_net = sum(r.earnings - r.losses for r, _, _ in sybils)
        assert sybil_net < 0, f"Sybils should lose: net {sybil_net:.1f}"

        honest_net = sum(r.earnings - r.losses for r, _, _ in honest)
        log.info("Sybil moderate ratio: avg err=%.2f%%, sybil net=%.1f, honest net=%.1f",
                 avg_err * 100, sybil_net, honest_net)


# ---------------------------------------------------------------------------
# Visualization: run a full scenario and produce a summary plot
# ---------------------------------------------------------------------------

class TestVisualization:

    def test_plot_simulation(self):
        """Run a rich scenario and produce a summary chart."""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            pytest.skip("matplotlib not available")

        from pathlib import Path

        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0, Q=0.001)

        # 4 honest, 2 adversaries, 1 challenger
        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=2000)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        adversaries = []
        for i in range(2):
            r = Reporter(f"adversary_{i}", stake=2000)
            oracle.register(r)
            adversaries.append((r, 0.0, 0.08))  # +8% bias

        challenger = Reporter("challenger", stake=2000)
        oracle.register(challenger)

        all_reporters = honest + adversaries + [(challenger, 0.003, 0.0)]

        def challenge_outliers(oracle_inst, round_id, submissions):
            if round_id < 3:
                return None
            est = oracle_inst.kalman.x
            P = oracle_inst.kalman.P
            if P <= 0:
                return None
            sigma = math.sqrt(P)
            for sub in submissions:
                norm_val = oracle_inst._normalize(sub.value)
                if sigma > 0 and abs(norm_val - est) > 2.5 * sigma:
                    return challenger
            return None

        results = run_simulation(oracle, all_reporters, n_rounds=50,
                                 initial_price=PRICE, price_drift=0.003, seed=42,
                                 challenge_fn=challenge_outliers)

        # Plot
        rounds = [r["round"] for r in results]
        true_prices = [r["true_price"] for r in results]
        estimates = [r["settled_value"] for r in results]
        errors = [r["estimate_error"] * 100 for r in results]

        fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)

        # Panel 1: price tracking
        ax = axes[0]
        ax.plot(rounds, true_prices, "k-", label="True price", linewidth=2)
        ax.plot(rounds, estimates, "b--", label="Oracle estimate", linewidth=1.5)
        ax.set_ylabel("Price")
        ax.set_title("TruthStake Oracle: Kalman filter with adversarial reporters")
        ax.legend()
        ax.grid(True, alpha=0.3)

        # Panel 2: estimation error
        ax = axes[1]
        ax.plot(rounds, errors, "r-", linewidth=1)
        ax.axhline(y=5, color="orange", linestyle="--", alpha=0.5, label="5% tolerance")
        ax.set_ylabel("Estimate error (%)")
        ax.legend()
        ax.grid(True, alpha=0.3)

        # Panel 3: cumulative P&L by reporter type
        ax = axes[2]
        honest_names = {r.name for r, _, _ in honest}
        adv_names = {r.name for r, _, _ in adversaries}
        h_cum, a_cum, c_cum = [], [], []
        h_total = a_total = c_total = 0
        for result in results:
            payouts = result["payouts"]
            stake = oracle.min_stake
            for name, payout in payouts.items():
                net = payout - stake
                if name in honest_names:
                    h_total += net
                elif name in adv_names:
                    a_total += net
                elif name == "challenger":
                    c_total += net
            h_cum.append(h_total)
            a_cum.append(a_total)
            c_cum.append(c_total)

        ax.plot(rounds, h_cum, "g-", label="Honest (total)", linewidth=2)
        ax.plot(rounds, a_cum, "r-", label="Adversaries (total)", linewidth=2)
        ax.plot(rounds, c_cum, "b-", label="Challenger", linewidth=1.5)
        ax.axhline(y=0, color="black", linewidth=0.5)
        ax.set_ylabel("Cumulative P&L")
        ax.set_xlabel("Round")
        ax.legend()
        ax.grid(True, alpha=0.3)

        plt.tight_layout()
        plot_path = Path(__file__).parent / "truthstake_simulation.png"
        fig.savefig(str(plot_path), dpi=150)
        plt.close(fig)
        log.info("Plot saved to %s", plot_path)

        # Summary
        for r, _, _ in honest:
            log.info("  %s", r)
        for r, _, _ in adversaries:
            log.info("  %s", r)
        log.info("  %s", challenger)

        # Assertions: the mechanism worked
        adv_net = sum(r.earnings - r.losses for r, _, _ in adversaries)
        honest_net = sum(r.earnings - r.losses for r, _, _ in honest)
        assert adv_net < 0, f"Adversaries should lose money: {adv_net:.1f}"
        assert honest_net > 0, f"Honest reporters should profit: {honest_net:.1f}"
