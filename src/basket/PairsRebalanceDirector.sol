// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {RebalanceDirectorBase} from "./RebalanceDirectorBase.sol";

/// @title PairsRebalanceDirector -- the differential-mode signal engine on
///        the shared rebalance-director chassis.
///
/// @notice Implements the `pairs` policy modeled in
///         alberta_buck/sim/rebalance_policy.py: rebalancing profit lives in
///         the CROSS-COMMODITY price differentials (the 3-phase-power
///         framing); the common numeraire (BUCK valuation, reserve-flow
///         noise) cancels exactly in pair differences and is the
///         K-controller's problem, not the rebalancer's.
///
///         Per leg, a ladder of K=7 EMAs at geometric timescales
///         (5..320 epochs) runs directly on the pool TICK -- a Uniswap tick
///         IS a log price, so no ln() is ever computed -- normalized so
///         positive always means TOKEN appreciating in BUCK.  EMA linearity
///         means every pair's moving average and velocity at every scale is
///         the difference of two legs' ladders: the full N(N-1)/2
///         differential graph from O(N*K) state.
///
///         A pair (i,j) trades when a QUORUM of scales votes: the scale's
///         MA gap must agree in sign with the raw pairwise imbalance, and
///         the gap must be CLOSING at that scale (velocity toward
///         equilibrium -- the confirmed-turn criterion; `vel` in the model,
///         measured as the better-balanced deployment default).  Effort =
///         kappa * |imbalance| * votes/K, capped; a pairwise leash with
///         hysteresis enforces the mandate through trends.  `boundaryBp`
///         optionally sizes on the EXCESS over the deadband instead of the
///         whole imbalance -- the no-trade-region form, which pays only where
///         trading is expensive (see `_pairEffort`).  The pairwise
///         imbalance is the arithmetic difference of weight ratios
///         (w_i/w*_i - w_j/w*_j, 1e18), computed from the chassis's cached
///         bv/s observations.
///
///         Pair efforts are cached and folded into per-leg NET efforts at
///         poke time, so the chassis's `depositHint`/`redeemHint`/`effortOf`
///         surface stays O(N) cached reads -- fully API-compatible with the
///         vrate director; `bestPair()` additionally names the top matched
///         trade (sell leg, buy leg) for keepers.
contract PairsRebalanceDirector is RebalanceDirectorBase {

    uint256 internal constant K = 7;
    uint32[K] internal WINDOWS = [uint32(5), 10, 20, 40, 80, 160, 320];

    struct Leg {
        int64[K] m;                 // EMA of normalized tick (tick * 1e9)
        int64[K] v;                 // per-epoch velocity EMA (tick * 1e9)
        int24    refTick;           // pair-gap reference (first observation)
        uint32   lastEpoch;
        uint32   firstEpoch;        // +1-encoded; 0 = never
    }

    struct Params {
        uint32 epochSeconds;        // sampling cadence (86400 = daily)
        uint8  quorum;              // scales that must vote the turn (of K)
        uint64 kappa1e9;            // effort gain (5e8 = 0.5)
        uint64 deadband1e9;         // min |pairwise imbalance| (15e6 = 1.5%)
        uint64 leash1e9;            // pairwise forced-rebalance bound (30%)
        uint64 leashInner1e9;       // leash release (hysteresis, 25%)
        uint32 capBpPerEpoch;       // max effort per pair per epoch
        uint32 boundaryBp;          // no-trade boundary, bp of the deadband
    }

    /// @notice Monetary-operation policy on the COMMON mode of the same
    ///         ladder.  All thresholds are in tick*1e9, and a Uniswap tick IS
    ///         a log price, so one tick is ~1bp and `100e9` reads as 100bp.
    struct MonParams {
        uint64 deadband1e9;      // below this, do nothing (100bp = 100e9)
        uint64 leash1e9;         // sustained beyond this -> outright (200e9)
        uint64 kappa1e9;         // effort gain on the deviation
        uint32 capBpPerEpoch;    // max monetary effort/epoch, bp of NAV; 0 = OFF
        uint16 persistEpochs;    // consecutive epochs past the leash -> outright
        uint8  measIdx;          // ladder rung measured on (2 = 20 epochs)
    }

    Params public params;
    MonParams public monParams;

    /// @notice Signed monetary effort, bp of NAV per epoch.
    ///         > 0 issue BUCK (BUCK dear)   < 0 retire BUCK (BUCK cheap)
    int32  public monEffortBp;
    /// @notice Whether the operation is outright (balance-sheet) or temporary.
    bool   public monOutright;
    /// @notice Consecutive epochs the common mode has spent past the leash.
    uint32 public monOverEpochs;
    uint32 internal monLastEpoch;                       // +1-encoded; 0 = never

    mapping(uint256 => Leg) internal legOf;
    mapping(uint256 => int32) internal netEffortBp;     // per leg
    mapping(uint256 => int32) internal pairEffortBp;    // per pair (i*16+j, i<j)
    uint256 internal leashedBits;                       // pair leash flags

    event Poked(uint256 indexed i, uint32 epoch, int24 normTick, int32 netEffortBp);
    event ParamsSet(Params p);

    constructor(address _basket, address _governance, Params memory p)
        RebalanceDirectorBase(_basket, _governance, p.epochSeconds)
    {
        _setParams(p);
    }

    function setParams(Params calldata p) external onlyGov { _setParams(p); }

    /// @notice Install the monetary-operation policy.  `capBpPerEpoch == 0`
    ///         switches monetary operations off entirely, which is the
    ///         default and the baseline this is compared against.
    function setMonParams(MonParams calldata mp) external onlyGov {
        // The measurement scale is a CORRECTNESS bound, not a preference:
        // past roughly the 80-epoch rung the lagged reading still says "dear"
        // long after the market has gone cheap, so an inflation excursion is
        // answered by ISSUING more.  Rung 3 (40) is the last safe one.
        if (mp.measIdx > 3 || mp.leash1e9 < mp.deadband1e9) revert BadParams();
        monParams = mp;
    }

    function _setParams(Params memory p) internal {
        if (p.epochSeconds == 0 || p.quorum == 0 || p.quorum > K
            || p.leashInner1e9 > p.leash1e9
            || p.boundaryBp > 10000) revert BadParams();
        params = p;
        epochSeconds = p.epochSeconds;
        emit ParamsSet(p);
    }

    // --- Chassis hooks -------------------------------------------------------- //

    function _isStale(uint256 i, uint32 e) internal view override returns (bool) {
        Leg storage leg = legOf[i];
        return leg.firstEpoch == 0 || leg.lastEpoch < e;
    }

    function effortOf(uint256 i) public view override returns (int256) {
        return netEffortBp[i];
    }

    // --- Views ------------------------------------------------------------------ //

    function legMa(uint256 i, uint256 k) external view returns (int64) {
        return legOf[i].m[k];
    }

    function legVel(uint256 i, uint256 k) external view returns (int64) {
        return legOf[i].v[k];
    }

    function legMeta(uint256 i)
        external view returns (int24 refTick, uint32 lastEpoch, uint32 firstEpoch)
    {
        Leg storage leg = legOf[i];
        return (leg.refTick, leg.lastEpoch, leg.firstEpoch);
    }

    /// @notice The COMMON mode at ladder scale `k`: the mean of every leg's
    ///         ladder, measured from that leg's own first observation.
    ///
    ///         A tick is a log price, so leg `i`'s ladder is log(price of
    ///         TOKEN_i in BUCK) and the MEAN of the legs is log(basket priced
    ///         in BUCK) -- basketValueInBuck itself, the K controller's own
    ///         process variable.  Because the EMA is linear,
    ///
    ///             mean_i EMA_k(c_i)  ==  EMA_k(mean_i c_i)
    ///
    ///         so this is the EMA of the common mode at scale k and it costs
    ///         no new state: the `pairs` engine already maintains every term.
    ///         The differences steer commodity rebalancing; the mean steers
    ///         monetary operations.  One filter bank, two mandates.
    ///
    ///         Sign: normTick is positive when TOKEN appreciates in BUCK, so
    ///         a POSITIVE common mode means the basket costs more BUCK than
    ///         it did -- BUCK is CHEAP.  Units are tick*1e9 (~1bp per tick).
    function commonMode(uint256 k) public view returns (int256 cm) {
        if (k >= K) return 0;
        uint256 seen = 0;
        uint256 n = constituentCount;
        for (uint256 i = 0; i < n; i++) {
            Leg storage leg = legOf[i];
            if (leg.firstEpoch == 0) continue;
            cm += int256(leg.m[k]) - int256(leg.refTick) * 1e9;
            seen++;
        }
        if (seen == 0) return 0;
        cm /= int256(seen);
    }

    /// @notice Signed monetary effort, bp of NAV per epoch, and whether the
    ///         operation is outright (changes the balance sheet) or temporary
    ///         (self-reversing).
    ///           > 0  issue BUCK      < 0  retire BUCK
    function monetaryEffort() external view returns (int32 effortBp, bool outright) {
        return (monEffortBp, monOutright);
    }

    function pairEffortOf(uint256 i, uint256 j) external view returns (int256) {
        return i < j ? int256(pairEffortBp[i * 16 + j])
                     : -int256(pairEffortBp[j * 16 + i]);
    }

    /// @notice The strongest matched trade: sell `sellIdx`, buy `buyIdx`.
    ///         (max-sentinel pair when no signal.)
    function bestPair() external view returns (uint256 sellIdx, uint256 buyIdx) {
        sellIdx = type(uint256).max;
        buyIdx = type(uint256).max;
        int256 best = 0;
        uint256 n = constituentCount;
        for (uint256 i = 0; i < n; i++) {
            for (uint256 j = i + 1; j < n; j++) {
                int256 e = pairEffortBp[i * 16 + j];
                int256 mag = e >= 0 ? e : -e;
                if (mag > best) {
                    best = mag;
                    (sellIdx, buyIdx) = e > 0 ? (i, j) : (j, i);
                }
            }
        }
    }

    // --- Signal update ---------------------------------------------------------- //

    function _pokeOne(uint256 i, uint32 e) internal override {
        Leg storage leg = legOf[i];
        (uint256 bvNew, uint256 sNew, int24 normTick) = _observe(i);
        _fold(i, bvNew, sNew);

        int256 x = int256(normTick) * 1e9;

        if (leg.firstEpoch == 0) {          // first touch: seed the ladder
            for (uint256 k = 0; k < K; k++) {
                leg.m[k] = int64(x);
            }
            leg.refTick = normTick;
            leg.firstEpoch = e + 1;
            leg.lastEpoch = e;
            emit Poked(i, e, normTick, 0);
            return;
        }

        uint32 dn = e - leg.lastEpoch;
        if (dn > 3650) dn = 3650;

        // Closed-form catch-up per window (sample-and-hold on the tick):
        //   m' = x + (m - x) q^n ;  v' = q^n v - (1-q)^2 n q^(n-1) (m - x)
        for (uint256 k = 0; k < K; k++) {
            int256 q = ONE - 2 * ONE / int256(uint256(WINDOWS[k]) + 1);
            int256 qn1 = _pow1e18(q, dn - 1);
            int256 qn = (qn1 * q) / ONE;
            int256 m0 = leg.m[k];
            leg.m[k] = int64(x + ((m0 - x) * qn) / ONE);
            int256 t = ((ONE - q) * (ONE - q)) / ONE;
            t = (t * int256(uint256(dn)) * qn1) / ONE;
            leg.v[k] = int64((int256(leg.v[k]) * qn) / ONE - (t * (m0 - x)) / ONE);
        }
        leg.lastEpoch = e;

        _refreshPairs(i, e);
        _updateMonetary(e);
        emit Poked(i, e, normTick, netEffortBp[i]);
    }

    /// @dev Advance the monetary policy once per epoch, on the common mode.
    ///
    ///      Persistence is measured as a DURATION rather than as an
    ///      instantaneous velocity.  A short-window velocity changes sign on
    ///      noise, so a "has it turned" test built on velocity almost never
    ///      reports "no" -- in the model that made the outright quadrants
    ///      unreachable, and supply never moved while half the mechanism
    ///      looked healthy.
    function _updateMonetary(uint32 e) internal {
        MonParams memory mp = monParams;
        if (mp.capBpPerEpoch == 0) return;          // monetary operations off
        if (monLastEpoch == e + 1) return;          // once per epoch
        monLastEpoch = e + 1;

        // Every leg must have the measurement rung warm, or the mean is taken
        // over ladders that are still seeded at their first observation and
        // reads as a spurious zero.
        uint32 w = WINDOWS[mp.measIdx];
        uint256 n = constituentCount;
        if (n == 0) { monEffortBp = 0; monOutright = false; return; }
        for (uint256 i = 0; i < n; i++) {
            Leg storage leg = legOf[i];
            if (leg.firstEpoch == 0 || e + 1 < leg.firstEpoch + w) {
                monEffortBp = 0; monOutright = false; return;
            }
        }

        int256 cm = commonMode(mp.measIdx);
        int256 absCm = cm >= 0 ? cm : -cm;

        monOverEpochs = absCm > int256(uint256(mp.leash1e9))
            ? monOverEpochs + 1 : 0;

        if (absCm < int256(uint256(mp.deadband1e9))) {
            monEffortBp = 0; monOutright = false; return;
        }

        int256 bp = int256(uint256(mp.kappa1e9)) * absCm / 1e9 / 1e9;
        int256 cap = int256(uint256(mp.capBpPerEpoch));
        if (bp > cap) bp = cap;
        // cm > 0 == basket dearer in BUCK == BUCK CHEAP == retire (negative).
        monEffortBp  = cm > 0 ? -int32(int256(bp)) : int32(int256(bp));
        monOutright  = monOverEpochs >= mp.persistEpochs;
    }

    /// @dev Recompute the pair efforts involving leg `i` against each synced
    ///      partner's (possibly one-wheel-turn stale) ladder, folding the
    ///      changes into both legs' cached net efforts.
    function _refreshPairs(uint256 i, uint32 e) internal {
        Params memory p = params;
        uint256 n = constituentCount;
        int256 ri = int256(_ratio1e18(i));
        for (uint256 j = 0; j < n; j++) {
            if (j == i || legOf[j].firstEpoch == 0) continue;
            (uint256 lo, uint256 hi) = i < j ? (i, j) : (j, i);
            uint256 idx = lo * 16 + hi;
            int256 d = ri - int256(_ratio1e18(j));      // imbalance from lo's view
            if (lo != i) d = -d;
            int32 eNew = _pairEffort(lo, hi, idx, d, e, p);
            int32 eOld = pairEffortBp[idx];
            if (eNew == eOld) continue;
            pairEffortBp[idx] = eNew;
            int32 dE = eNew - eOld;
            netEffortBp[lo] -= dE;                      // +pair = lo sells
            netEffortBp[hi] += dE;
        }
    }

    /// @dev Signed pair effort from `lo`'s perspective (+ = lo rich: sell lo,
    ///      buy hi), in bp of NAV per epoch.  `d` = ratio(lo) - ratio(hi).
    function _pairEffort(uint256 lo, uint256 hi, uint256 idx, int256 d,
                         uint32 e, Params memory p) internal returns (int32) {
        int256 absD = d >= 0 ? d : -d;

        // Pairwise leash with hysteresis (bit per pair).
        uint256 bit = 1 << idx;
        bool was = leashedBits & bit != 0;
        bool leashed = absD > int256(uint256(was ? p.leashInner1e9 : p.leash1e9)) * 1e9;
        if (leashed != was) leashedBits ^= bit;
        if (leashed) {
            return d > 0 ? int32(p.capBpPerEpoch) : -int32(p.capBpPerEpoch);
        }
        if (absD < int256(uint256(p.deadband1e9)) * 1e9) return 0;

        // Quorum of scales: gap sign-agrees with d AND gap is closing there.
        Leg storage a = legOf[lo];
        Leg storage b = legOf[hi];
        int256 refGap = (int256(a.refTick) - int256(b.refTick)) * 1e9;
        uint256 votes = 0;
        for (uint256 k = 0; k < K; k++) {
            uint32 w = WINDOWS[k];
            if (e + 1 < a.firstEpoch + w || e + 1 < b.firstEpoch + w) continue;
            int256 g = int256(a.m[k]) - int256(b.m[k]) - refGap;
            if (g == 0 || (g > 0) != (d > 0)) continue;   // scale disagrees
            int256 gv = int256(a.v[k]) - int256(b.v[k]);
            int256 toward = d > 0 ? -gv : gv;
            if (toward > 0) votes++;                      // gap already closing
        }
        if (votes < p.quorum) return 0;

        // Under proportional costs the optimal policy is a NO-TRADE REGION:
        // one trades only far enough to reach its boundary, never all the way
        // to the target (Davis-Norman 1990; Shreve-Soner 1994).  `boundaryBp`
        // is how much of the deadband to treat as that boundary -- 0 sizes on
        // the full |d| (toward the target), 10000 sizes on |d| - deadband (to
        // the edge).
        //
        // It is a governance knob and not a constant because the measured
        // sign FLIPS with trading cost, exactly as the theory predicts: the
        // optimal region widens with cost, so imposing a wide one where
        // trading is cheap gives up premium for nothing.  Against the Python
        // model (rebalance_policy.py, `pairs` vs `pairs-nt`, 20y x 5 seeds,
        // premium vs hold per year):
        //
        //     cost/leg      30bp      100bp      250bp
        //     delta       -5.2bp     +3.6bp    +15.9bp
        //
        // on ~15% less turnover throughout.  A 30bp venue -- which is what
        // the 0.30% TOKEN/BUCK pools are -- should leave this at 0.
        int256 sizeD = absD;
        if (p.boundaryBp != 0) {
            sizeD -= int256(uint256(p.deadband1e9)) * 1e9
                * int256(uint256(p.boundaryBp)) / 10000;
            if (sizeD <= 0) return 0;      // inside the boundary: hold still
        }

        // effort = kappa * |d| * votes/K, in bp of NAV per epoch, capped.
        int256 bp = (int256(uint256(p.kappa1e9)) * sizeD * int256(votes))
            / int256(K) / 1e9 / 1e14;
        int256 cap = int256(uint256(p.capBpPerEpoch));
        if (bp > cap) bp = cap;
        if (bp == 0) return 0;
        return d > 0 ? int32(int256(bp)) : -int32(int256(bp));
    }
}
