// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "./BuckKControllerDirect.sol";
import {IShadowObserver} from "./basket/IShadowObserver.sol";

/// @title BuckKControllerShadow -- the direct PID plus a second loop on the
///        level's aggregate POSITION (CARRY-CONVEXITY.org D4, D7; WAVE3.org
///        WP-3a, WP-13).
///
/// @notice Once fast facilities absorb a dump, bvib improves, K sees less
///         error and stops tightening, and the facilities are stranded
///         holding inventory with nothing behind it (D4).  K is therefore
///         tasked with driving the level's inventory back to zero -- MID-
///         RANGING (4.4) -- and D7 makes that a loop of its own.  This
///         contract runs TWO loops on one lever, one integrator each:
///
///           K = K0 + [ Kp e + Ki Int(e) + Kd de/dt ]         the price loop (Direct)
///                  + [ Kq r + Kqi Int(r) + Kqd dr/dt ]       the position loop
///
///         with e = 1 - bvib the price error (ppm) read from the RAW basket
///         (WAVE3.org decision 10: the price loop and fundingFactor() never
///         see the position), and r = 0 - s the POSITION ERROR (ppm), s the
///         observer's aggregate position (absorbed positive, issued
///         negative; `IShadowObserver`, in S or V units) carried in ppm like
///         e: s_ppm = s_1e18 / 1e12.  Both loops are PIDs on an error with
///         the same real*1e12 gain convention, so (Kq, Kqi, Kqd) are read
///         exactly as (Kp, Ki, Kd) and every stored state (P, S) is an error.
///
///         SIGNS (7.3, D7): BUCK absorbed under the weak side makes s
///         positive, r negative, and K falls -- the contraction the
///         absorption was betting on; BUCK issued makes s negative and K
///         rises, restoring the headroom the fast actuators spent.  With
///         mode S and (Kq, Kqi, Kqd) = (Kp, Ki, Kd) the two loops sum to
///         D4 as built term for term in the interior (e + r = 1 - (bvib +
///         s_S)); at a rail the anti-windup is per integrator, so that
///         identity is interior-exact and rail-approximate.
///
///         ONE position integrator, on the aggregate: K is one lever and can
///         zero one position (D7, "the lever count").  RAIL ANTI-WINDUP is
///         per integrator: at a rail each of I and IS may only move in the
///         direction that brings the raw output back toward the band (the
///         Direct rule, applied to each; gains assumed positive).
///
///         BUMPLESS ALGEBRA.  In the interior the committed state satisfies
///
///           buckK == buckK0 + Kp*P + Ki*I + Kq*S + Kqi*IS      (uD = uQD = 0)
///
///         so every re-derivation of I -- retune(), retunePosition(),
///         setBuckK0(), reprime() -- solves that for I and the live buckK is
///         unchanged; the integrators then trim from the new resting point.
///
///         gamma (WP-3a) stays as an OPTIONAL schedule on the price
///         integral's increment, Ki_eff = Ki (1 + gamma sat), default 0,
///         keyed to the observer's HELD saturation (never a guard trip:
///         decision 9).  The increment is scheduled, never the stock.
///
///         IDENTITY.  With Kq = Kqi = Kqd = 0 and gamma = 0 (or no observer)
///         this contract reproduces BuckKControllerDirect's state EXACTLY on
///         the same inputs; that identity is the regression test.
///
///         ATTRIBUTION (R14).  `terms()` returns the last cycle's six
///         contributions in 1e18 K units under the committed state, the
///         aggregate position as read, and the observer's stale / excluded
///         masks; `BuckKTerms` carries the same beside `BuckKUpdated`.
contract BuckKControllerShadow is BuckKControllerDirect {

    /// @notice The observer whose aggregate position feeds the position
    ///         loop (and whose held saturation gamma keys off).  Unset: the
    ///         position loop reads 0 and this IS Direct.
    IShadowObserver public observer;

    /// @notice Gain-scheduling strength, 1e18-scaled: Ki_eff = Ki * (1 +
    ///         gamma * saturation).  0 = no scheduling.
    uint256 public gamma;

    // --- The position loop (WP-13) ---------------------------------------- //

    /// @notice Position-loop gains, real * 1e12 like Kp / Ki / Kd.
    int256 public Kq;
    int256 public Kqi;
    int256 public Kqd;

    /// @notice Last position ERROR r = 0 - s, ppm (the S of the algebra).
    int256 public S;
    /// @notice Integral of the position error, ppm*seconds.
    int256 public IS;
    /// @notice Last change in the position error, ppm (like D for the price).
    int256 public DS;
    /// @notice The aggregate position as last read from the observer, 1e18.
    int256 public lastS;
    /// @notice The last cycle's effective dt (seconds), for terms().
    uint256 public lastDt;

    /// @notice The last cycle's attribution (1e18 K units) and flags.
    struct Terms {
        int256  uP;
        int256  uI;
        int256  uD;
        int256  uQ;
        int256  uQI;
        int256  uQD;
        int256  s;              // 1e18, absorbed positive
        uint256 staleMask;      // observer.flags()
        uint256 excludedMask;
    }

    uint256 internal constant MAX_GAMMA = 1_000e18;   // sanity bound

    event ObserverSet(address indexed observer);
    event GammaSet(uint256 gamma);
    event PositionGainsUpdated(int256 Kq, int256 Kqi, int256 Kqd);
    event PositionRetuned(int256 Kq, int256 Kqi, int256 Kqd, int256 I);
    event BuckKTerms(int256 uP, int256 uI, int256 uD, int256 uQ, int256 uQI, int256 uQD,
                     int256 s, uint256 staleMask, uint256 excludedMask);

    constructor(
        int256 _Kp, int256 _Ki, int256 _Kd,
        uint256 _dT,
        uint256 _buckKMin, uint256 _buckKMax, uint256 _buckK,
        address _governance
    ) BuckKControllerDirect(_Kp, _Ki, _Kd, _dT, _buckKMin, _buckKMax, _buckK, _governance) {}

    // --- Governance --------------------------------------------------------- //

    /// @notice Governance: wire (or re-wire; address(0) unwires) the observer.
    /// @dev    Switching observers steps the position; `reprime()` is the
    ///         basket's to call, so govern the switch at a quiet moment.
    function setObserver(address _observer) external {
        require(msg.sender == governance, "Not governance");
        observer = IShadowObserver(_observer);
        emit ObserverSet(_observer);
    }

    /// @notice Governance: the gain-scheduling strength (1e18 = Ki doubles at
    ///         full saturation).  Takes effect on the next increment only;
    ///         the live buckK and the wound integral are untouched.
    function setGamma(uint256 _gamma) external {
        require(msg.sender == governance, "Not governance");
        require(_gamma <= MAX_GAMMA, "gamma too large");
        gamma = _gamma;
        emit GammaSet(_gamma);
    }

    /// @notice Governance: raw position gains (the setGains analogue).  A
    ///         wound IS steps buckK by IS*(Kqi_new - Kqi_old); use
    ///         retunePosition for a live loop.
    function setPositionGains(int256 _Kq, int256 _Kqi, int256 _Kqd) external {
        require(msg.sender == governance, "Not governance");
        Kq = _Kq; Kqi = _Kqi; Kqd = _Kqd;
        emit PositionGainsUpdated(_Kq, _Kqi, _Kqd);
    }

    /// @notice Governance: position gains with BUMPLESS TRANSFER -- the price
    ///         integrator is re-derived under the new gains so the live
    ///         buckK is unchanged (the retune analogue; needs Ki != 0).
    function retunePosition(int256 _Kq, int256 _Kqi, int256 _Kqd) external {
        require(msg.sender == governance, "Not governance");
        require(Ki != 0, "retune needs Ki");
        Kq = _Kq; Kqi = _Kqi; Kqd = _Kqd;
        I = _rederiveI();
        emit PositionGainsUpdated(_Kq, _Kqi, _Kqd);
        emit PositionRetuned(_Kq, _Kqi, _Kqd, I);
    }

    // --- Views --------------------------------------------------------------- //

    /// @notice The schedule's current multiplier on the integral increment,
    ///         1e18-scaled (1e18 = Direct's).  Telemetry and forecasting.
    function integralBoost() public view returns (uint256) {
        if (gamma == 0 || address(observer) == address(0)) return 1e18;
        uint256 sat = observer.shadowSaturation();
        if (sat > 1e18) sat = 1e18;
        return 1e18 + gamma * sat / 1e18;
    }

    /// @notice Ki * (1 + gamma * saturation), the effective integral gain.
    function kiEffective() external view returns (int256) {
        return Ki * int256(integralBoost()) / 1e18;
    }

    /// @notice The last cycle's per-term attribution under the committed
    ///         state (at a rail the discarded increments are not in it, so
    ///         the six terms sum to the raw output, not the rail).
    function terms() public view returns (Terms memory t) {
        int256 dt = int256(lastDt);
        t.uP  = Kp * P;
        t.uI  = Ki * I;
        t.uD  = dt > 0 ? Kd * D / dt : int256(0);
        t.uQ  = Kq * S;
        t.uQI = Kqi * IS;
        t.uQD = dt > 0 ? Kqd * DS / dt : int256(0);
        t.s   = lastS;
        if (address(observer) != address(0)) {
            (t.staleMask, t.excludedMask) = observer.flags();
        }
    }

    // --- The cycle ------------------------------------------------------------ //

    /// @dev err * dt * (1 + gamma * sat).  With gamma = 0 (or no observer)
    ///      the boost is exactly 1e18 and the increment is exactly Direct's
    ///      `err * dt` -- no rounding enters the identity.
    function _integralStep(int256 err, int256 dt) internal view override
        returns (int256)
    {
        int256 step = err * dt;
        if (gamma == 0 || address(observer) == address(0)) return step;
        return step * int256(integralBoost()) / UNIT;
    }

    /// @dev The aggregate position for this cycle: the observer refreshes
    ///      its held caps and returns s (1e18); 0 without an observer.
    function _observe() internal returns (int256) {
        if (address(observer) == address(0)) return 0;
        return observer.observe();
    }

    /// @notice Run (or cache) one two-loop PID cycle in ppm space and return
    ///         buckK.  Permissionless; cheap cached-read when dT has not
    ///         elapsed.
    function compute() external virtual override returns (uint256) {
        uint256 elapsed = block.timestamp - lastUpdate;
        if (elapsed < dT) {
            return buckK;
        }

        uint256 effective = elapsed > dTMax ? dTMax : elapsed;
        int256  dt = int256(effective);

        // The price loop, exactly Direct's, on the RAW basket.
        (int256 buckValue, int256 basketValue) = _readReferences();
        int256 setpoint = buckValue   / TO18;   // ppm (== PPM in direct mode)
        int256 process  = basketValue / TO18;   // ppm
        int256 err      = setpoint - process;   // ppm
        int256 newI     = I + _integralStep(err, dt);   // ppm*seconds
        int256 dErr     = err - P;              // ppm
        int256 rawOutput = int256(buckK0)
            + Kp * err
            + Ki * newI
            + (dt > 0 ? Kd * dErr / dt : int256(0));

        // The position loop: r = 0 - s in ppm, its own integrator.
        int256 s      = _observe();             // 1e18
        int256 sErr   = -(s / TO18);            // ppm
        int256 newIS  = IS + sErr * dt;         // ppm*seconds
        int256 dSErr  = sErr - S;               // ppm
        rawOutput += Kq * sErr
            + Kqi * newIS
            + (dt > 0 ? Kqd * dSErr / dt : int256(0));

        // Anti-windup, per integrator: at a rail, only let an integrator
        // move back toward the operating band (never wind further into the
        // clamp).
        uint256 newBuckK;
        if (rawOutput < int256(buckKMin)) {
            newBuckK = buckKMin;
            if (newI  > I)  I  = newI;
            if (newIS > IS) IS = newIS;
        } else if (rawOutput > int256(buckKMax)) {
            newBuckK = buckKMax;
            if (newI  < I)  I  = newI;
            if (newIS < IS) IS = newIS;
        } else {
            newBuckK = uint256(rawOutput);
            I  = newI;
            IS = newIS;
        }

        P              = err;
        D              = dErr;
        S              = sErr;
        DS             = dSErr;
        lastS          = s;
        lastDt         = effective;
        buckK          = newBuckK;
        lastUpdate     = block.timestamp;
        lastBasketCost = process;               // ppm, for fundingFactor()
        lastBuckPrice  = setpoint;              // ppm

        emit BuckKUpdated(newBuckK, err, P, I, D);
        _emitTerms();
        return newBuckK;
    }

    function _emitTerms() internal {
        Terms memory t = terms();
        emit BuckKTerms(t.uP, t.uI, t.uD, t.uQ, t.uQI, t.uQD, t.s, t.staleMask, t.excludedMask);
    }

    // --- Bumpless algebra ------------------------------------------------------ //

    /// @dev buckK == buckK0 + Kp*P + Ki*I + Kq*S + Kqi*IS, so the I that
    ///      holds the current output is (buckK - buckK0 - Kp*P - Kq*S -
    ///      Kqi*IS) / Ki -- the price integrator absorbs the step, the
    ///      position integrator keeps its history.
    function _rederiveI() internal view virtual override returns (int256) {
        return (int256(buckK) - int256(buckK0) - Kp * P - Kq * S - Kqi * IS) / Ki;
    }

    /// @notice Privileged: BuckBasket calls this after addBasketToken to
    ///         absorb the dilution discontinuity.  Recaptures P against the
    ///         raw basket AND S against the observer (a new constituent
    ///         changes D under S and the caps under V), then re-derives I so
    ///         the next no-motion cycle reproduces the current buckK.
    function reprime() external virtual override {
        require(msg.sender == address(basket), "only basket");
        (int256 buckValue, int256 basketValue) = _readReferences();
        int256 setpoint = buckValue   / TO18;
        int256 process  = basketValue / TO18;
        int256 err      = setpoint - process;
        int256 s        = _observe();
        int256 sErr     = -(s / TO18);

        P = err;
        D = 0;
        S = sErr;
        DS = 0;
        lastS = s;
        if (Ki != 0) {
            I = (int256(buckK) - int256(buckK0) - Kp * err - Kq * sErr - Kqi * IS) / Ki;
        }
        lastBasketCost = process;
        lastBuckPrice  = setpoint;
        lastUpdate     = block.timestamp;
        emit Reprimed(P, I);
    }
}
