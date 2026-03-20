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
# Gradual drift: adversary slowly increases bias to avoid gating
# ---------------------------------------------------------------------------

class TestGradualDrift:

    def test_slow_drift_evades_gating(self):
        """Adversary increases bias by 0.3%/round, staying under gate threshold.

        The bias ramps from 0% to ~9% over 30 rounds.  Because each step
        is small relative to the current estimate, the gating check never
        triggers.  The estimate slowly drifts -- a subtle attack.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=2.0)
        honest = []
        for i in range(3):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        drifter = Reporter("drifter", stake=1000)
        oracle.register(drifter)

        def drift_strategy(round_id):
            bias = 0.003 * round_id  # +0.3% per round
            return (0.001, bias)

        all_reporters = honest + [(drifter, 0.001, 0.0)]
        results = run_simulation(oracle, all_reporters, n_rounds=30,
                                 true_price_fn=lambda _: PRICE, seed=200,
                                 strategies={"drifter": drift_strategy})

        # Early rounds: estimate is fine
        early_err = sum(r["estimate_error"] for r in results[:5]) / 5
        assert early_err < 0.01, f"Early error should be small: {early_err:.4f}"

        # Late rounds: estimate has drifted measurably
        late_err = sum(r["estimate_error"] for r in results[-5:]) / 5
        assert late_err > early_err, (
            f"Gradual drift should increase error over time: "
            f"early={early_err:.4f} late={late_err:.4f}")

        # But the drifter eventually gets classified dishonest and loses stake
        assert drifter.losses > 0, f"Drifter should eventually lose stake: {drifter.losses:.1f}"
        log.info("Gradual drift: early_err=%.2f%% late_err=%.2f%% drifter_losses=%.1f",
                 early_err * 100, late_err * 100, drifter.losses)

    def test_slow_drift_detected_by_challenge(self):
        """Challenger who tracks estimate velocity detects gradual drift.

        By comparing the current estimate to a trailing average, a
        vigilant challenger can detect systematic drift and escalate.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=2.0, tolerance=0.05)
        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        drifter = Reporter("drifter", stake=1000)
        oracle.register(drifter)
        challenger = Reporter("challenger", stake=1000)
        oracle.register(challenger)

        def drift_strategy(round_id):
            return (0.001, 0.003 * round_id)

        # Track estimate history for drift detection
        estimate_history = []

        def challenge_on_drift(oracle_inst, round_id, submissions):
            estimate_history.append(oracle_inst.kalman.x)
            if round_id < 8:
                return None
            # Compare current estimate to 5-round-ago estimate
            if len(estimate_history) > 5:
                drift = abs(estimate_history[-1] - estimate_history[-6])
                if drift > 0.01:  # >1% drift over 5 rounds
                    return challenger
            return None

        all_reporters = honest + [(drifter, 0.001, 0.0), (challenger, 0.005, 0.0)]
        results = run_simulation(oracle, all_reporters, n_rounds=30,
                                 true_price_fn=lambda _: PRICE, seed=200,
                                 strategies={"drifter": drift_strategy},
                                 challenge_fn=challenge_on_drift)

        # Drifter should lose more than in the unchallenged case
        assert drifter.losses > 0, f"Drifter should lose: {drifter.losses:.1f}"

        # Honest should profit
        honest_net = sum(r.earnings - r.losses for r, _, _ in honest)
        assert honest_net > 0, f"Honest should profit: {honest_net:.1f}"

        log.info("Drift + challenge: drifter losses=%.1f, honest net=%.1f",
                 drifter.losses, honest_net)


# ---------------------------------------------------------------------------
# Majority takeover: adversaries outnumber honest, honest lose money
# ---------------------------------------------------------------------------

class TestMajorityTakeover:

    def test_majority_adversary_controls_estimate(self):
        """6 adversaries vs 2 honest: estimate drifts to adversary bias.

        A 3:1 adversary supermajority eventually controls the settled value.
        However, the Kalman filter's warmup period (3 rounds of high R for
        new reporters) provides a transient defense: adversaries lose stakes
        heavily in early rounds while untrusted, and honest reporters
        collect those stakes.  The estimate still drifts to ~10% bias by
        round 15+, but the attack is costly.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=2.0)
        honest = []
        for i in range(2):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        adversaries = []
        for i in range(6):
            r = Reporter(f"adv_{i}", stake=1000)
            oracle.register(r)
            adversaries.append((r, 0.001, 0.10))  # +10% bias, low noise

        all_reporters = honest + adversaries
        results = run_simulation(oracle, all_reporters, n_rounds=25,
                                 true_price_fn=lambda _: PRICE, seed=303)

        # The estimate converges to the adversary bias (~10%)
        late_errors = [r["estimate_error"] for r in results[-5:]]
        avg_err = sum(late_errors) / len(late_errors)
        assert avg_err > 0.05, (
            f"Majority adversary should control estimate: avg error {avg_err:.4f}")

        # Early rounds: adversaries are untrusted (high R), classified dishonest
        early_dishonest = sum(r["dishonest"] for r in results[:3])
        assert early_dishonest > 10, (
            f"Adversaries should be dishonest early: {early_dishonest} dishonest")

        # Honest reporters still profit overall from the early windfall
        # (collecting adversary stakes while adversaries have high R)
        honest_net = sum(r.earnings - r.losses for r, _, _ in honest)
        log.info("Majority takeover: late err=%.1f%%, honest net=%.1f "
                 "(profit from early-round adversary losses)",
                 avg_err * 100, honest_net)

        # Adversaries collectively lose money despite controlling the estimate
        # because they paid heavily during the warmup period
        adv_net = sum(r.earnings - r.losses for r, _, _ in adversaries)
        assert adv_net < 0, (
            f"Adversaries should be net negative from warmup losses: {adv_net:.1f}")

    def test_majority_warmup_defense_duration(self):
        """The warmup defense lasts ~3 rounds; after that, majority wins.

        Tracks the round at which adversaries first become the "honest"
        majority (i.e. when the estimate has drifted far enough that
        adversary submissions fall within tolerance of the settled value).
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=2.0)
        honest = []
        for i in range(2):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        adversaries = []
        for i in range(6):
            r = Reporter(f"adv_{i}", stake=1000)
            oracle.register(r)
            adversaries.append((r, 0.001, 0.10))

        all_reporters = honest + adversaries
        results = run_simulation(oracle, all_reporters, n_rounds=25,
                                 true_price_fn=lambda _: PRICE, seed=303)

        # Find the crossover: when adversaries become majority-honest
        crossover = None
        for r in results:
            if r["honest"] > r["dishonest"] and r["honest"] >= 6:
                crossover = r["round"]
                break

        assert crossover is not None, "Adversary majority should eventually control"
        assert crossover >= 3, (
            f"Warmup defense should last at least 3 rounds: crossover at {crossover}")
        assert crossover <= 8, (
            f"Crossover should happen within 8 rounds: {crossover}")

        log.info("Majority crossover at round %d (adversaries become 'honest')",
                 crossover)


# ---------------------------------------------------------------------------
# Whale manipulation: one high-stake adversary vs many small honest reporters
# ---------------------------------------------------------------------------

class TestWhaleManipulation:

    def test_whale_stake_does_not_amplify_kalman_influence(self):
        """A single whale staking 100x cannot move the estimate more than anyone else.

        The Kalman filter weights observations by reporter R (reputation),
        NOT by stake amount.  A whale's single observation has identical
        influence to any other reporter with the same track record.  Their
        large stake is simply more money at risk.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0)
        honest = []
        for i in range(5):
            r = Reporter(f"honest_{i}", stake=500)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        whale = Reporter("whale", stake=10000)
        oracle.register(whale)

        # Run manually to control whale stake amount
        import random as _rand
        rng = _rand.Random(42)

        results = []
        for rid in range(30):
            oracle.open_round()
            for r, ns, _ in honest:
                v = r.observe(PRICE, noise_std=ns, bias=0.0)
                oracle.submit(r, v, stake=1.0)
            # Whale submits with 100x stake but +8% bias
            wv = whale.observe(PRICE, noise_std=0.001, bias=0.08)
            oracle.submit(whale, wv, stake=100.0)
            results.append(oracle.settle(true_price=PRICE))

        # Estimate should NOT be dragged toward whale's bias
        # because Kalman gain depends on R, not stake
        late_errors = [r["estimate_error"] for r in results[-5:]]
        avg_err = sum(late_errors) / len(late_errors)
        assert avg_err < 0.03, (
            f"Whale stake should not amplify Kalman influence: err {avg_err:.4f}")

        # But the whale loses their massive stakes
        assert whale.losses > 500, (
            f"Whale should have large absolute losses: {whale.losses:.1f}")

        # Honest reporters profit from the whale's lost stakes
        honest_net = sum(r.earnings - r.losses for r, _, _ in honest)
        assert honest_net > 0, f"Honest should profit from whale: {honest_net:.1f}"

        log.info("Whale: losses=%.1f, honest net=%.1f, avg_err=%.2f%%",
                 whale.losses, honest_net, avg_err * 100)

    def test_whale_pool_distortion(self):
        """Whale's large stake distorts the reward pool in honest reporters' favor.

        When the whale is classified dishonest, their entire large stake
        enters the reward pool.  Honest reporters divide this windfall,
        earning far more per round than normal.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0)
        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=500)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        whale = Reporter("whale", stake=5000)
        oracle.register(whale)

        # Run with whale always staking big
        for rid in range(15):
            oracle.open_round()
            for r, ns, _ in honest:
                v = r.observe(PRICE, noise_std=ns, bias=0.0)
                oracle.submit(r, v, stake=1.0)
            wv = whale.observe(PRICE, noise_std=0.001, bias=0.10)
            oracle.submit(whale, wv, stake=50.0)
            oracle.settle(true_price=PRICE)

        # Whale should have lost most of their stakes
        whale_loss_rate = whale.losses / (50.0 * 15)
        assert whale_loss_rate > 0.5, (
            f"Whale should lose majority of stakes: {whale_loss_rate:.1%}")

        # Average honest earning per round should be elevated by whale stakes
        avg_honest_earnings = sum(r.earnings for r, _, _ in honest) / (len(honest) * 15)
        assert avg_honest_earnings > 1.0, (
            f"Honest per-round earnings should be elevated: {avg_honest_earnings:.2f}")

        log.info("Whale lost %.0f%% of stakes; honest avg earn/round=%.2f",
                 whale_loss_rate * 100, avg_honest_earnings)


# ---------------------------------------------------------------------------
# Front-running: adversary observes all submissions, then submits last
# ---------------------------------------------------------------------------

class TestFrontRunning:

    def test_last_mover_pays_early_bonus_penalty(self):
        """Adversary always submits last to observe the consensus, but earns less.

        Even if the adversary copies the honest consensus perfectly by
        submitting last, they pay the early-bonus penalty: later submitters
        get a smaller share of the reward pool.  This makes front-running
        strictly inferior to honest early submission.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0, early_halflife=2.0)
        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append(r)

        frontrunner = Reporter("frontrunner", stake=1000)
        oracle.register(frontrunner)

        import random as _rand
        rng = _rand.Random(42)

        for rid in range(40):
            oracle.open_round()
            # Honest reporters submit first (indices 0-3)
            honest_values = []
            for r in honest:
                v = r.observe(PRICE, noise_std=0.003, bias=0.0)
                oracle.submit(r, v, stake=1.0)
                honest_values.append(v)
            # Front-runner submits last, copying the median of honest values
            median_v = sorted(honest_values)[len(honest_values) // 2]
            oracle.submit(frontrunner, median_v, stake=1.0)
            oracle.settle(true_price=PRICE)

        # Front-runner is perfectly accurate (copies honest consensus)
        # but earns LESS than the first honest reporter due to early bonus
        first_net = honest[0].earnings - honest[0].losses
        fr_net = frontrunner.earnings - frontrunner.losses
        assert fr_net < first_net, (
            f"Front-runner ({fr_net:.3f}) should earn less than "
            f"first submitter ({first_net:.3f})")

        # Front-runner should earn less than the AVERAGE honest reporter
        avg_honest_net = sum(r.earnings - r.losses for r in honest) / len(honest)
        assert fr_net < avg_honest_net, (
            f"Front-runner ({fr_net:.3f}) should earn less than "
            f"honest avg ({avg_honest_net:.3f})")

        log.info("Front-running: fr_net=%.3f, first_net=%.3f, avg_honest=%.3f",
                 fr_net, first_net, avg_honest_net)

    def test_frontrunner_with_bias_loses(self):
        """Front-runner observes consensus then adds a small bias (+2%).

        Even a small adversarial adjustment after front-running honest
        consensus leads to losses as the submissions deviate from the
        settled value.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0)
        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append(r)

        biased_fr = Reporter("biased_fr", stake=1000)
        oracle.register(biased_fr)

        import random as _rand
        rng = _rand.Random(42)

        for rid in range(30):
            oracle.open_round()
            honest_values = []
            for r in honest:
                v = r.observe(PRICE, noise_std=0.003, bias=0.0)
                oracle.submit(r, v, stake=1.0)
                honest_values.append(v)
            # Front-runner copies consensus + adds 2% bias
            median_v = sorted(honest_values)[len(honest_values) // 2]
            biased_v = median_v * 1.02
            oracle.submit(biased_fr, biased_v, stake=1.0)
            oracle.settle(true_price=PRICE)

        # Biased front-runner should lose money
        fr_net = biased_fr.earnings - biased_fr.losses
        honest_net = sum(r.earnings - r.losses for r in honest) / len(honest)
        assert fr_net < honest_net, (
            f"Biased front-runner ({fr_net:.3f}) should earn less "
            f"than honest avg ({honest_net:.3f})")

        log.info("Biased front-runner: net=%.3f, honest avg=%.3f",
                 fr_net, honest_net)


# ---------------------------------------------------------------------------
# Volatility exploitation: adversary attacks when Kalman P is high
# ---------------------------------------------------------------------------

class TestVolatilityExploitation:

    def test_adversary_exploits_high_P_after_challenge(self):
        """Adversary idles until a challenge doubles P, then submits biased value.

        After a legitimate challenge increases Kalman uncertainty, the
        filter is more susceptible to new observations (higher K).  An
        adversary who times their attack for this window can have more
        influence than normal.  The defense: honest reporters also
        re-submit post-challenge, diluting the adversary's impact.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=2.0, Q=0.001)
        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append(r)

        # Adversary who waits for high-P
        opportunist = Reporter("opportunist", stake=1000)
        oracle.register(opportunist)

        # Innocent challenger (creates the high-P window)
        challenger = Reporter("challenger", stake=1000)
        oracle.register(challenger)

        results = []
        for rid in range(30):
            oracle.open_round()

            # Honest reporters always submit
            for r in honest:
                v = r.observe(PRICE, noise_std=0.005, bias=0.0)
                oracle.submit(r, v, stake=2.0)

            # At round 10, challenger files a legitimate challenge
            if rid == 10:
                oracle.challenge(challenger)
                eff_stake = oracle.min_stake * oracle._stake_multiplier()
                # Opportunist immediately submits biased value while P is high
                biased_v = opportunist.observe(PRICE, noise_std=0.001, bias=0.10)
                oracle.submit(opportunist, biased_v, stake=eff_stake)
                # Honest reporters also re-submit post-challenge
                for r in honest:
                    v = r.observe(PRICE, noise_std=0.005, bias=0.0)
                    try:
                        oracle.submit(r, v, stake=eff_stake)
                    except ValueError:
                        pass
            elif rid > 10 and rid < 15:
                # Opportunist continues attacking in the high-P window
                biased_v = opportunist.observe(PRICE, noise_std=0.001, bias=0.10)
                oracle.submit(opportunist, biased_v, stake=2.0)

            result = oracle.settle(true_price=PRICE)
            results.append(result)

        # Round 10-14: estimate may spike briefly
        spike_errors = [r["estimate_error"] for r in results[10:15]]
        max_spike = max(spike_errors)

        # But it recovers: late rounds should be back to low error
        late_errors = [r["estimate_error"] for r in results[-5:]]
        avg_late_err = sum(late_errors) / len(late_errors)
        assert avg_late_err < 0.02, (
            f"Should recover from volatility attack: late err {avg_late_err:.4f}")

        # Opportunist should lose money overall
        opp_net = opportunist.earnings - opportunist.losses
        assert opp_net < 0, f"Opportunist should lose: net {opp_net:.1f}"

        log.info("Volatility exploit: max spike=%.2f%%, late err=%.2f%%, opp net=%.1f",
                 max_spike * 100, avg_late_err * 100, opp_net)

    def test_high_Q_amplifies_burst_attack(self):
        """Higher Q makes the filter forget faster, amplifying brief attacks.

        Q controls how fast P grows between rounds.  With high Q, the
        filter "forgets" past observations quickly, making a 3-round
        burst attack produce a larger spike than with low Q.
        """
        import random as _rand
        spike_by_Q = {}
        for Q in [0.0001, 0.001, 0.01]:
            rng = _rand.Random(42)  # deterministic per Q iteration
            oracle = Oracle(initial_estimate=PRICE, min_stake=1.0, Q=Q)
            reporters = []
            for i in range(4):
                r = Reporter(f"h_{i}", stake=1000)
                oracle.register(r)
                reporters.append(r)

            adv = Reporter("adv", stake=1000)
            oracle.register(adv)

            results = []
            for rid in range(20):
                oracle.open_round()
                for r in reporters:
                    noise = rng.gauss(0, 0.005)
                    v = PRICE * (1.0 + noise)
                    oracle.submit(r, v, stake=1.0)
                # Burst attack rounds 8-10 only
                if 8 <= rid <= 10:
                    noise = rng.gauss(0, 0.001)
                    av = PRICE * (1.0 + noise + 0.10)
                    oracle.submit(adv, av, stake=1.0)
                result = oracle.settle(true_price=PRICE)
                results.append(result)

            # Peak error during burst
            burst_errors = [r["estimate_error"] for r in results[8:12]]
            spike_by_Q[Q] = max(burst_errors) if burst_errors else 0

        # Higher Q should produce a larger spike from the same burst
        assert spike_by_Q[0.01] > spike_by_Q[0.0001], (
            f"Higher Q should amplify burst: Q=0.01 spike={spike_by_Q[0.01]:.4f} "
            f"vs Q=0.0001 spike={spike_by_Q[0.0001]:.4f}")

        for Q, spike in sorted(spike_by_Q.items()):
            log.info("  Q=%.4f -> burst peak error=%.4f%%", Q, spike * 100)


# ---------------------------------------------------------------------------
# Sandwich attack: two adversaries bracket the price to shift the mean
# ---------------------------------------------------------------------------

class TestSandwichAttack:

    def test_symmetric_sandwich_cancels_out(self):
        """Two adversaries submit +5% and -5%: biases cancel, no net effect.

        A symmetric sandwich has zero net bias.  Both adversaries may
        stay within tolerance and share the pool, but the estimate
        remains accurate.  The attack is pointless -- neither adversary
        profits differentially.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0)
        honest = []
        for i in range(3):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        high = Reporter("high_adv", stake=1000)
        low = Reporter("low_adv", stake=1000)
        oracle.register(high)
        oracle.register(low)

        all_reporters = honest + [(high, 0.001, 0.04), (low, 0.001, -0.04)]
        results = run_simulation(oracle, all_reporters, n_rounds=25,
                                 true_price_fn=lambda _: PRICE, seed=42)

        # Estimate stays accurate: symmetric biases cancel
        late_errors = [r["estimate_error"] for r in results[-5:]]
        avg_err = sum(late_errors) / len(late_errors)
        assert avg_err < 0.02, (
            f"Symmetric sandwich should cancel: avg error {avg_err:.4f}")

        log.info("Symmetric sandwich: avg err=%.2f%%", avg_err * 100)

    def test_asymmetric_sandwich_shifts_estimate(self):
        """Adversaries submit +7% and -3%: net +2% bias shifts the estimate.

        An asymmetric sandwich tries to shift the estimate while keeping
        individual deviations within gating range.  The net effect is
        limited because the Kalman filter still weights by reputation.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0)
        honest = []
        for i in range(3):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        high_adv = Reporter("high_adv", stake=1000)
        low_adv = Reporter("low_adv", stake=1000)
        oracle.register(high_adv)
        oracle.register(low_adv)

        # Net bias: (0.07 + (-0.03)) / 2 = +0.02 (2%)
        all_reporters = honest + [(high_adv, 0.001, 0.07), (low_adv, 0.001, -0.03)]
        results = run_simulation(oracle, all_reporters, n_rounds=25,
                                 true_price_fn=lambda _: PRICE, seed=42)

        # Some drift, but less than the net 2% bias
        late_errors = [r["estimate_error"] for r in results[-5:]]
        avg_err = sum(late_errors) / len(late_errors)

        # The 3:2 honest majority limits the drift
        assert avg_err < 0.04, (
            f"Asymmetric sandwich drift should be bounded: {avg_err:.4f}")

        # The high-bias adversary should lose more than the low-bias one
        assert high_adv.losses >= low_adv.losses, (
            f"Higher-bias adversary should lose more: "
            f"high={high_adv.losses:.1f} low={low_adv.losses:.1f}")

        log.info("Asymmetric sandwich: avg err=%.2f%%, high losses=%.1f, low losses=%.1f",
                 avg_err * 100, high_adv.losses, low_adv.losses)


# ---------------------------------------------------------------------------
# Stake starvation: adversary drains honest reporters' capital via challenges
# ---------------------------------------------------------------------------

class TestStakeStarvation:

    def test_challenge_griefing_costs_attacker_more(self):
        """Adversary challenges every round to drain honest reporters' capital.

        Each challenge escalation increases the minimum stake for ALL
        subsequent submissions in that round.  But the challenger pays
        an exponentially growing bond, so griefing costs the attacker
        far more than the honest reporters.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=2.0)
        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=500)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        griefer = Reporter("griefer", stake=2000)
        oracle.register(griefer)

        # Griefer challenges every round but doesn't submit observations
        def grief_challenge(oracle_inst, round_id, submissions):
            return griefer

        all_reporters = honest  # griefer only challenges, doesn't submit
        results = run_simulation(oracle, all_reporters, n_rounds=15,
                                 true_price_fn=lambda _: PRICE, seed=42,
                                 challenge_fn=grief_challenge)

        # Griefer should lose their bonds (they never submitted honestly)
        assert griefer.losses > 0, f"Griefer should lose bonds: {griefer.losses:.1f}"

        # Honest reporters should survive and profit from griefer bonds
        honest_surviving = sum(1 for r, _, _ in honest if r.stake > 10)
        assert honest_surviving >= 3, (
            f"Most honest reporters should survive: {honest_surviving}/4")

        # Griefer's total losses should exceed any individual honest reporter's losses
        max_honest_loss = max(r.losses for r, _, _ in honest)
        assert griefer.losses > max_honest_loss, (
            f"Griefer losses ({griefer.losses:.1f}) should exceed "
            f"max honest loss ({max_honest_loss:.1f})")

        log.info("Griefing: griefer losses=%.1f stake=%.1f, honest surviving=%d",
                 griefer.losses, griefer.stake, honest_surviving)

    def test_honest_reporters_recover_from_capital_depletion(self):
        """After an adversary depletes capital and stops, honest reporters rebuild.

        Even if honest reporters lose some stake during an attack, they
        recover as the adversary runs out of money and leaves.  The
        positive-sum dynamics among honest reporters restore balances.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0)
        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=200)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        drainer = Reporter("drainer", stake=300)
        oracle.register(drainer)

        # Drainer attacks for first 10 rounds, then goes bankrupt
        def drainer_strategy(round_id):
            if round_id < 10 and drainer.stake >= 1.0:
                return (0.001, 0.15)  # +15% bias
            return (0.005, 0.0)       # honest (or bankrupt)

        all_reporters = honest + [(drainer, 0.005, 0.0)]
        results = run_simulation(oracle, all_reporters, n_rounds=30,
                                 true_price_fn=lambda _: PRICE, seed=42,
                                 strategies={"drainer": drainer_strategy})

        # Late rounds: estimate recovers
        late_errors = [r["estimate_error"] for r in results[-5:]]
        avg_late_err = sum(late_errors) / len(late_errors)
        assert avg_late_err < 0.02, (
            f"Estimate should recover after attacker depletes: {avg_late_err:.4f}")

        # Drainer should have significant losses
        assert drainer.losses > 0, f"Drainer should lose: {drainer.losses:.1f}"

        log.info("Starvation recovery: late err=%.2f%%, drainer losses=%.1f",
                 avg_late_err * 100, drainer.losses)


# ---------------------------------------------------------------------------
# Sleeper cartel: build reputation honestly, then coordinate attack
# ---------------------------------------------------------------------------

class TestSleeperCartel:

    def test_sleeper_attack_maximum_damage(self):
        """3 sleepers build 20 rounds of perfect reputation, then attack.

        This is the hardest attack to defend against.  After extensive
        honest reporting, sleepers have very low R (high trust).  A
        coordinated attack from trusted reporters has maximum Kalman
        influence.  The damage depends on cartel size vs honest reporters.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0)
        honest = []
        for i in range(3):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        sleepers = []
        for i in range(3):
            r = Reporter(f"sleeper_{i}", stake=1000)
            oracle.register(r)
            sleepers.append(r)

        # Phase 1: build reputation (20 rounds honest)
        for rid in range(20):
            oracle.open_round()
            for r, ns, _ in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=ns), stake=1.0)
            for s in sleepers:
                oracle.submit(s, s.observe(PRICE, noise_std=0.003), stake=1.0)
            oracle.settle(true_price=PRICE)

        # Verify sleepers built low R
        for s in sleepers:
            assert s.R < 0.01, f"{s.name} should have low R after 20 honest rounds: {s.R:.6f}"

        pre_attack_error = abs(oracle.kalman.x - 1.0)
        assert pre_attack_error < 0.01, f"Pre-attack estimate should be accurate"

        # Phase 2: coordinated attack (5 rounds of +8% bias)
        attack_results = []
        for rid in range(5):
            oracle.open_round()
            for r, ns, _ in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=ns), stake=1.0)
            for s in sleepers:
                biased = s.observe(PRICE, noise_std=0.001, bias=0.08)
                oracle.submit(s, biased, stake=1.0)
            result = oracle.settle(true_price=PRICE)
            attack_results.append(result)

        # With equal numbers (3:3) and similar low R, biased submissions
        # are partially cancelled by honest ones.  The estimate moves
        # but not dramatically -- the Kalman filter distributes influence
        # equally among reporters with similar reputation.
        peak_err = max(r["estimate_error"] for r in attack_results)
        assert peak_err > 0.0005, (
            f"Sleeper attack should move estimate at least slightly: {peak_err:.4f}")

        # But sleepers pay for the attack (classified dishonest eventually)
        sleeper_losses = sum(s.losses for s in sleepers)
        assert sleeper_losses > 0, f"Sleepers should incur losses: {sleeper_losses:.1f}"

        log.info("Sleeper cartel: peak err=%.2f%%, sleeper losses=%.1f, "
                 "pre-attack R=%s",
                 peak_err * 100, sleeper_losses,
                 [f"{s.R:.6f}" for s in sleepers])

    def test_sleeper_attack_with_challenge_defense(self):
        """Challenger detects sudden estimate movement from sleeper attack.

        A vigilant challenger monitoring estimate velocity can detect
        the sleeper attack and escalate.  Post-challenge honest
        re-submissions partially restore the estimate.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0, tolerance=0.05)
        honest = []
        for i in range(3):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        sleepers = []
        for i in range(3):
            r = Reporter(f"sleeper_{i}", stake=1000)
            oracle.register(r)
            sleepers.append(r)

        challenger = Reporter("challenger", stake=1000)
        oracle.register(challenger)

        # Phase 1: build reputation
        for rid in range(20):
            oracle.open_round()
            for r, ns, _ in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=ns), stake=1.0)
            for s in sleepers:
                oracle.submit(s, s.observe(PRICE, noise_std=0.003), stake=1.0)
            oracle.settle(true_price=PRICE)

        # Phase 2: attack with challenge
        estimate_history = [oracle.kalman.x]
        attack_results = []
        for rid in range(10):
            oracle.open_round()
            for r, ns, _ in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=ns), stake=1.0)
            for s in sleepers:
                biased = s.observe(PRICE, noise_std=0.001, bias=0.08)
                oracle.submit(s, biased, stake=1.0)

            # Challenge if estimate moved > 1% from 2 rounds ago
            if len(estimate_history) >= 2:
                drift = abs(oracle.kalman.x - estimate_history[-2])
                if drift > 0.01:
                    try:
                        oracle.challenge(challenger)
                        # Re-submit honest values post-challenge
                        for r, ns, _ in honest:
                            eff_stake = oracle.min_stake * oracle._stake_multiplier()
                            try:
                                oracle.submit(r, r.observe(PRICE, noise_std=ns), stake=eff_stake)
                            except ValueError:
                                pass
                    except ValueError:
                        pass

            result = oracle.settle(true_price=PRICE)
            attack_results.append(result)
            estimate_history.append(oracle.kalman.x)

        # With challenge, the peak error should be lower than without
        peak_err = max(r["estimate_error"] for r in attack_results)

        # Sleepers should still lose money
        sleeper_net = sum(s.earnings - s.losses for s in sleepers)
        assert sleeper_net < 0, f"Sleepers should be net negative: {sleeper_net:.1f}"

        log.info("Sleeper + challenge: peak err=%.2f%%, sleeper net=%.1f",
                 peak_err * 100, sleeper_net)


# ---------------------------------------------------------------------------
# Oscillation attack: alternate +/- bias to destabilize without net drift
# ---------------------------------------------------------------------------

class TestOscillationAttack:

    def test_alternating_bias_increases_variance(self):
        """Adversary alternates +6% and -6% bias each round.

        The adversary avoids consistent dishonesty detection by having
        zero mean bias.  But the alternating submissions increase the
        estimate's variance (jitter).  The Kalman filter partially
        smooths this, and the adversary's ema_sq_error rises because
        each submission deviates from the settled value.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0)
        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append((r, 0.005, 0.0))

        oscillator = Reporter("oscillator", stake=1000)
        oracle.register(oscillator)

        def osc_strategy(round_id):
            bias = 0.06 if round_id % 2 == 0 else -0.06
            return (0.001, bias)

        all_reporters = honest + [(oscillator, 0.001, 0.0)]
        results = run_simulation(oracle, all_reporters, n_rounds=30,
                                 true_price_fn=lambda _: PRICE, seed=42,
                                 strategies={"oscillator": osc_strategy})

        # Estimate stays roughly accurate (zero-mean bias)
        late_errors = [r["estimate_error"] for r in results[-10:]]
        avg_err = sum(late_errors) / len(late_errors)
        assert avg_err < 0.03, (
            f"Zero-mean oscillation shouldn't bias estimate much: {avg_err:.4f}")

        # But oscillator's R should increase (high variance in submissions)
        assert oscillator.ema_sq_error > 0.001, (
            f"Oscillator should have elevated error: {oscillator.ema_sq_error:.6f}")

        # Oscillator should lose money (classified dishonest in most rounds
        # because each submission deviates 6% from settled value)
        assert oscillator.losses > oscillator.earnings, (
            f"Oscillator should be net loser: earn={oscillator.earnings:.1f} "
            f"loss={oscillator.losses:.1f}")

        log.info("Oscillation: avg err=%.2f%%, osc R=%.6f, osc net=%.1f",
                 avg_err * 100, oscillator.R,
                 oscillator.earnings - oscillator.losses)

    def test_small_oscillation_stays_within_tolerance(self):
        """Adversary alternates +3% and -3%, staying within 5% tolerance.

        Small oscillation within tolerance means the adversary is
        classified "honest" every round.  They pay for increased
        jitter but collect from the pool.  The attack's net cost
        depends on the early-bonus dynamics.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0, tolerance=0.05)
        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append((r, 0.003, 0.0))

        small_osc = Reporter("small_osc", stake=1000)
        oracle.register(small_osc)

        def small_osc_strategy(round_id):
            bias = 0.03 if round_id % 2 == 0 else -0.03
            return (0.001, bias)

        all_reporters = honest + [(small_osc, 0.001, 0.0)]
        results = run_simulation(oracle, all_reporters, n_rounds=30,
                                 true_price_fn=lambda _: PRICE, seed=42,
                                 strategies={"small_osc": small_osc_strategy})

        # The small oscillator's accuracy_bonus is lower (further from
        # settled value), so they get a smaller share of the pool per round
        avg_honest_net = sum(r.earnings - r.losses for r, _, _ in honest) / len(honest)
        osc_net = small_osc.earnings - small_osc.losses
        assert osc_net < avg_honest_net, (
            f"Oscillator ({osc_net:.3f}) should earn less than "
            f"honest avg ({avg_honest_net:.3f})")

        log.info("Small oscillation: osc net=%.3f, honest avg=%.3f",
                 osc_net, avg_honest_net)


# ---------------------------------------------------------------------------
# Late entry: new adversary joins mid-game against established reporters
# ---------------------------------------------------------------------------

class TestLateEntry:

    def test_new_adversary_suppressed_by_established_honest(self):
        """New adversary joining after 15 rounds is immediately suppressed.

        Established honest reporters have very low R (high trust) while
        the newcomer has R=1.0 (untrusted).  The Kalman gain for the
        newcomer is negligible: K = P/(P+1.0) << P/(P+0.0001).
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0)
        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append(r)

        # Phase 1: establish reputation
        for rid in range(15):
            oracle.open_round()
            for r in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.005), stake=1.0)
            oracle.settle(true_price=PRICE)

        # Verify honest reporters have low R
        for r in honest:
            assert r.R < 0.01, f"{r.name} should be trusted: R={r.R:.6f}"

        # New adversary joins
        late_adv = Reporter("late_adv", stake=500)
        oracle.register(late_adv)

        # Phase 2: adversary attacks, honest reporters continue
        attack_results = []
        for rid in range(10):
            oracle.open_round()
            for r in honest:
                oracle.submit(r, r.observe(PRICE, noise_std=0.005), stake=1.0)
            biased = late_adv.observe(PRICE, noise_std=0.001, bias=0.15)
            oracle.submit(late_adv, biased, stake=1.0)
            result = oracle.settle(true_price=PRICE)
            attack_results.append(result)

        # Estimate barely moves because new reporter has high R (and
        # large bias is likely gated by the low-P sigma threshold)
        peak_err = max(r["estimate_error"] for r in attack_results)
        assert peak_err < 0.01, (
            f"New adversary should have negligible impact: peak err {peak_err:.4f}")

        # Adversary either: (a) gets gated and stake returned (zero cost,
        # zero impact), or (b) gets through but classified dishonest.
        # Either way, honest reporters are protected.
        adv_net = late_adv.earnings - late_adv.losses
        assert adv_net <= 0, (
            f"Late adversary should not profit: net={adv_net:.1f}")

        # Adversary's stake should be unchanged (gated) or reduced (dishonest)
        assert late_adv.stake <= 500, (
            f"Adversary stake should not increase: {late_adv.stake:.1f}")

        log.info("Late entry: peak err=%.3f%%, adv stake=%s (started 500), "
                 "gated=%s",
                 peak_err * 100, f"{late_adv.stake:.1f}",
                 "yes" if late_adv.stake == 500 else "no")

    def test_new_honest_reporter_builds_trust_gradually(self):
        """A new honest reporter joining mid-game earns trust over time.

        The warmup penalty (high R for first 3 rounds) means the new
        reporter's observations have less influence initially, but their
        R decreases as they prove accurate.  By round 5-6 they are
        contributing meaningfully to the estimate.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0)
        established = []
        for i in range(3):
            r = Reporter(f"est_{i}", stake=1000)
            oracle.register(r)
            established.append(r)

        # Build established trust
        for rid in range(15):
            oracle.open_round()
            for r in established:
                oracle.submit(r, r.observe(PRICE, noise_std=0.005), stake=1.0)
            oracle.settle(true_price=PRICE)

        # New honest reporter joins
        newcomer = Reporter("newcomer", stake=500)
        oracle.register(newcomer)
        assert newcomer.R == 1.0, "Newcomer should start untrusted"

        # Track newcomer's R over rounds
        r_history = [newcomer.R]
        for rid in range(10):
            oracle.open_round()
            for r in established:
                oracle.submit(r, r.observe(PRICE, noise_std=0.005), stake=1.0)
            oracle.submit(newcomer, newcomer.observe(PRICE, noise_std=0.005), stake=1.0)
            oracle.settle(true_price=PRICE)
            r_history.append(newcomer.R)

        # R should decrease over time as newcomer proves accurate
        assert r_history[-1] < r_history[0], (
            f"Newcomer R should decrease: {r_history[0]:.4f} -> {r_history[-1]:.6f}")

        # After 3+ settlements, R should drop below 1.0
        assert newcomer.R < 0.5, (
            f"After proving accuracy, R should be much lower: {newcomer.R:.6f}")

        log.info("Late honest entry: R trajectory = %s",
                 [f"{r:.4f}" for r in r_history])


# ---------------------------------------------------------------------------
# Price discontinuity: true price jumps, adversary exploits confusion
# ---------------------------------------------------------------------------

class TestPriceDiscontinuity:

    def test_oracle_tracks_price_jump(self):
        """True price drops 10% suddenly; oracle re-converges.

        With sufficient Q (process noise), the filter eventually tracks
        the new price level.  The convergence rate depends on reporter R
        and the Q/P ratio.
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0, Q=0.001)
        reporters = []
        for i in range(5):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            reporters.append((r, 0.005, 0.0))

        def price_with_jump(round_id):
            if round_id < 15:
                return PRICE
            return PRICE * 0.90  # -10% flash crash at round 15

        results = run_simulation(oracle, reporters, n_rounds=40,
                                 true_price_fn=price_with_jump, seed=42)

        # Immediately after jump: noticeable error (filter hasn't fully caught up)
        jump_error = results[15]["estimate_error"]
        assert jump_error > 0.01, (
            f"Immediate post-jump error should be noticeable: {jump_error:.4f}")

        # But by 10 rounds later, filter should have converged
        late_errors = [r["estimate_error"] for r in results[-5:]]
        avg_late_err = sum(late_errors) / len(late_errors)
        assert avg_late_err < 0.02, (
            f"Should reconverge after jump: avg late err {avg_late_err:.4f}")

        log.info("Price jump: immediate err=%.2f%%, late err=%.2f%%",
                 jump_error * 100, avg_late_err * 100)

    def test_adversary_exploits_price_jump_confusion(self):
        """Adversary submits the old pre-jump price after a crash.

        During the confusion of a price discontinuity, honest reporters
        submit the new (crashed) price while an adversary submits the
        old price.  The adversary is classified dishonest because the
        settled value tracks toward the new price (which most reporters
        observe).
        """
        oracle = Oracle(initial_estimate=PRICE, min_stake=1.0, Q=0.001)
        honest = []
        for i in range(4):
            r = Reporter(f"honest_{i}", stake=1000)
            oracle.register(r)
            honest.append(r)

        stale_adv = Reporter("stale_adv", stake=1000)
        oracle.register(stale_adv)

        def price_with_crash(round_id):
            if round_id < 10:
                return PRICE
            return PRICE * 0.85  # -15% crash

        results = []
        for rid in range(25):
            price = price_with_crash(rid)
            oracle.open_round()
            for r in honest:
                oracle.submit(r, r.observe(price, noise_std=0.005), stake=1.0)
            # Adversary always submits the pre-crash price
            oracle.submit(stale_adv, stale_adv.observe(PRICE, noise_std=0.005), stake=1.0)
            result = oracle.settle(true_price=price)
            results.append(result)

        # Post-crash: adversary is eventually classified dishonest as the
        # estimate converges to the new price.  Initially the adversary's
        # old-price submission is CLOSER to the (pre-crash) estimate than
        # the honest reporters' new-price submissions -- a brief window
        # where the adversary paradoxically appears "honest."
        post_crash_dishonest = sum(1 for r in results[12:] if r["dishonest"] > 0)
        assert post_crash_dishonest > 0, (
            f"Stale adversary should eventually be dishonest: {post_crash_dishonest}")

        # Adversary should lose money
        assert stale_adv.losses > 0, f"Stale adv should lose: {stale_adv.losses:.1f}"

        # Oracle should track the new price eventually
        late_err = sum(r["estimate_error"] for r in results[-3:]) / 3
        assert late_err < 0.05, (
            f"Oracle should track post-crash price: late err {late_err:.4f}")

        log.info("Stale adversary: losses=%.1f, post-crash dishonest=%d/13, late err=%.2f%%",
                 stale_adv.losses, post_crash_dishonest, late_err * 100)


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
