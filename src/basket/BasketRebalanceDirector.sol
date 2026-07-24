// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {RebalanceDirectorBase} from "./RebalanceDirectorBase.sol";

/// @title BasketRebalanceDirector -- the vrate signal engine on the shared
///        rebalance-director chassis.
///
/// @notice A standalone *advisor* for a BuckBasket implementing the `vrate`
///         policy modeled in alberta_buck/sim/rebalance_policy.py: per
///         constituent, an EMA of the share deviation (the delayed view),
///         its velocity (the regime: receding / level / gaining), and a
///         1-week EMA of the deviation's change (the observed closure rate),
///         sized by rate-matching and bounded by a deviation leash.
///
///         All chassis behavior -- the lazy epoch clock, the round-robin
///         `poke(maxWork)` work wheel, cached per-pool observations with
///         running sums, and the `depositHint`/`redeemHint`/`effortOf`
///         advisory surface -- lives in `RebalanceDirectorBase`; this
///         contract supplies only the signal update and the effort rule.
///
/// # Closed-form catch-up (the amortization invariant)
///
///         Under sample-and-hold, n missed epochs of the diligent per-epoch
///         EMA recursions collapse exactly (q = 1-beta):
///
///           m'    = delta + (m - delta) * q^n
///           v'    = q^n * v - (1-q)^2 * n * q^(n-1) * (m - delta)
///           ddot' = q7^(n-1) * ((delta - prevDelta)*(1-q7) + q7 * ddot)
///
///         each O(log n), so one poke after a quiet month lands the signals
///         where n diligent pokes would -- fuzzed and enforced by the tests.
contract BasketRebalanceDirector is RebalanceDirectorBase {

    struct Signal {                     // hot per-constituent state
        int128  m;                      // EMA_X of deviation (1e18)
        int128  vEma;                   // EMA_X of per-epoch d(m) (1e18)
        int128  ddotEma;                // 1-week EMA of per-epoch d(delta)
        int128  prevDelta;              // last sampled raw deviation (1e18)
        int32   effortBp;               // signed advisory effort (bp of NAV)
        uint32  lastEpoch;
        uint32  firstEpoch;
        bool    leashed;
    }

    struct Params {
        uint32  epochSeconds;           // sampling cadence (86400 = daily)
        uint32  windowEpochs;           // X: MA window, in epochs
        uint64  rho1e9;                 // match ratio (1e9; 3e9 = 3.0)
        uint64  deadband1e9;            // min |delta| to act (1e9; 15e6 = 1.5%)
        uint64  epsFrac1e9;             // level-regime band as frac of |m|/X
        uint64  leash1e9;               // raw-|delta| forced-rebalance bound
        uint64  leashInner1e9;          // leash release (hysteresis)
        uint32  capBpPerEpoch;          // max effort per constituent per epoch
    }

    Params public params;
    mapping(uint256 => Signal) public signalOf;

    int256 internal constant LN2 = 693147180559945309;   // ln(2) * 1e18

    event Poked(uint256 indexed i, uint32 epoch, int256 delta, int256 m, int32 effortBp);
    event ParamsSet(Params p);

    constructor(address _basket, address _governance, Params memory p)
        RebalanceDirectorBase(_basket, _governance, p.epochSeconds)
    {
        _setParams(p);
    }

    function setParams(Params calldata p) external onlyGov { _setParams(p); }

    function _setParams(Params memory p) internal {
        if (p.epochSeconds == 0 || p.windowEpochs < 2
            || p.leashInner1e9 > p.leash1e9) revert BadParams();
        params = p;
        epochSeconds = p.epochSeconds;
        emit ParamsSet(p);
    }

    // --- Chassis hooks -------------------------------------------------------- //

    function _isStale(uint256 i, uint32 e) internal view override returns (bool) {
        Signal storage sig = signalOf[i];
        return sig.firstEpoch == 0 || sig.lastEpoch < e;
    }

    function effortOf(uint256 i) public view override returns (int256) {
        return signalOf[i].effortBp;
    }

    // --- Signal update ---------------------------------------------------------- //

    function _pokeOne(uint256 i, uint32 e) internal override {
        Signal storage sig = signalOf[i];
        (uint256 bvNew, uint256 sNew,) = _observe(i);
        _fold(i, bvNew, sNew);

        // First touch: initialize and wait out the warmup.
        if (sig.firstEpoch == 0) {
            int256 d0 = deviationOf(i);
            sig.m = int128(d0);
            sig.prevDelta = int128(d0);
            sig.firstEpoch = e + 1;      // +1 so 0 keeps meaning "never"
            sig.lastEpoch = e;
            emit Poked(i, e, d0, d0, 0);
            return;
        }

        uint32 dn = e - sig.lastEpoch;   // wheel guarantees >= 1
        if (dn > 3650) dn = 3650;        // signals fully converged past this
        int256 delta = deviationOf(i);

        // Closed-form catch-up over dn epochs, EXACTLY reproducing dn
        // per-epoch EMA updates under sample-and-hold on delta.
        Params memory p = params;
        int256 q = ONE - 2 * ONE / int256(uint256(p.windowEpochs) + 1);
        int256 qn1 = _pow1e18(q, dn - 1);
        int256 qn = (qn1 * q) / ONE;
        int256 m0 = sig.m;
        int256 m1 = delta + ((m0 - delta) * qn) / ONE;
        sig.m = int128(m1);
        int256 t = ((ONE - q) * (ONE - q)) / ONE;
        t = (t * int256(uint256(dn)) * qn1) / ONE;
        sig.vEma = int128((int256(sig.vEma) * qn) / ONE - (t * (m0 - delta)) / ONE);

        int256 q7 = ONE - 2 * ONE / 8;                     // 1-week EMA
        int256 inner = ((delta - int256(sig.prevDelta)) * (ONE - q7)) / ONE
            + (q7 * int256(sig.ddotEma)) / ONE;
        sig.ddotEma = int128((_pow1e18(q7, dn - 1) * inner) / ONE);
        sig.prevDelta = int128(delta);
        sig.lastEpoch = e;

        sig.effortBp = _effort(i, sig, p, delta, m1, e);
        emit Poked(i, e, delta, m1, sig.effortBp);
    }

    /// @dev The vrate policy: velocity-regime gate + rate-matched sizing.
    ///      Positive effort = pool wants funds (buy/deposit side); negative =
    ///      pool should be drawn down (sell/redeem side).
    function _effort(uint256 i, Signal storage sig, Params memory p,
                     int256 delta, int256 m, uint32 e) internal returns (int32) {
        int256 absD = delta >= 0 ? delta : -delta;

        // Leash: beyond leash1e9 the mandate overrides the quench.
        int256 leash  = int256(uint256(p.leash1e9)) * 1e9;
        int256 inner  = int256(uint256(p.leashInner1e9)) * 1e9;
        bool wasLeashed = sig.leashed;
        sig.leashed = wasLeashed ? absD > inner : absD > leash;
        if (sig.leashed) {
            return delta > 0 ? -int32(p.capBpPerEpoch) : int32(p.capBpPerEpoch);
        }

        // Warmup, deadband, and raw-vs-MA sign agreement.
        if (e - sig.firstEpoch < p.windowEpochs) return 0;
        if (absD < int256(uint256(p.deadband1e9)) * 1e9) return 0;
        if (m * delta <= 0) return 0;

        // Regime from the MA's velocity: u > 0 = the delayed view is closing.
        int256 u = m > 0 ? -int256(sig.vEma) : int256(sig.vEma);
        int256 eps = (int256(uint256(p.epsFrac1e9)) * 1e9 * (m >= 0 ? m : -m))
            / ONE / int256(uint256(p.windowEpochs));
        if (u < -eps) return 0;                          // receding: quench

        // Rate-matched sizing: observed closure (1-week EMA), floored at the
        // "half the gap in one window" prior (which alone applies while level).
        int256 c0 = (LN2 * absD) / ONE / (2 * int256(uint256(p.windowEpochs)));
        int256 c = c0;
        if (u > eps) {                                   // gaining: complete it
            int256 observed = delta > 0 ? -int256(sig.ddotEma) : int256(sig.ddotEma);
            if (observed > c) c = observed;
        }

        // effort = rho * c * weight(s/S), in bp of NAV per epoch, capped.
        int256 w = int256(uint256(sOf[i])) * ONE / int256(runningS);
        int256 bp = (int256(uint256(p.rho1e9)) * c / 1e9) * w / ONE / 1e14;
        int256 cap = int256(uint256(p.capBpPerEpoch));
        if (bp > cap) bp = cap;
        if (bp == 0) return 0;
        return delta > 0 ? -int32(int256(bp)) : int32(int256(bp));
    }
}
