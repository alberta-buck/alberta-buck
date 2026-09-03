// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "./BuckKControllerDirect.sol";
import {IShadowObserver} from "./basket/IShadowObserver.sol";

/// @title BuckKControllerShadow -- the direct PID reading the shadow bvib.
///
/// @notice CARRY-CONVEXITY.org D4 (mid-ranging): once a fast desk absorbs a
///         dump, bvib improves, K sees less error and stops tightening, and
///         the desk is stranded holding inventory with nothing behind it.
///         So K's process variable becomes the SHADOW deviation -- bvib as it
///         would have been without the fast actuators:
///
///           bvib_shadow = bvib + sum_i lambda_i * netInventory_i / D
///
///         assembled by the observer (`IShadowObserver`, the ops shell
///         through wave 3) from the level-1 stabilizers' books.  Equivalently
///         K is tasked with driving the desk's inventory to zero.
///
///         This variant IS BuckKControllerDirect -- the same ppm loop, rails,
///         conditional integration, priming, dS and dT -- with two seams:
///
///           * `_readReferences` reads `observer.shadowValueInBuck()` when an
///             observer is wired (else the basket, else UNIT, exactly as
///             Direct);
///           * gain scheduling on `saturation`: the effective integral gain
///             is Ki_eff = Ki * (1 + gamma * shadowSaturation()/1e18), so
///             that when the fast actuators are pinned at their bounds the
///             structural lever moves faster.
///
///         THE SCHEDULE IS APPLIED TO THE INCREMENT, NOT THE STOCK.  The
///         integrator accumulates err * dt * (1 + gamma * sat) each cycle and
///         the output is still buckK0 + Kp*P + Ki*I; the slope of K under a
///         sustained error is Ki_eff * err either way, but the wound integral
///         is never rescaled when saturation changes (no step in K when a
///         bound is hit or released), and reprime(), retune(), setBuckK0()
///         keep their bumpless algebra unchanged.
///
///         With gamma = 0 and every lambda_i = 0 (or no observer) this
///         contract reproduces BuckKControllerDirect's outputs EXACTLY on the
///         same inputs; that identity is the regression test.
///
///         SIGNS (CARRY-CONVEXITY.org 7.3): BUCK absorbed under the weak side
///         makes netInventory positive, raises bvib_shadow, makes the error
///         more negative, and K falls harder -- the contraction the
///         absorption was betting on.  BUCK issued under the strong side
///         makes it negative, lowers bvib_shadow, and K rises -- restoring
///         the headroom the fast actuators spent.
contract BuckKControllerShadow is BuckKControllerDirect {

    /// @notice The observer whose shadowValueInBuck() is K's process
    ///         variable.  Unset: read the basket directly (Direct).
    IShadowObserver public observer;

    /// @notice Gain-scheduling strength, 1e18-scaled: Ki_eff = Ki * (1 +
    ///         gamma * saturation).  0 = no scheduling.
    uint256 public gamma;

    uint256 internal constant MAX_GAMMA = 1_000e18;   // sanity bound

    event ObserverSet(address indexed observer);
    event GammaSet(uint256 gamma);

    constructor(
        int256 _Kp, int256 _Ki, int256 _Kd,
        uint256 _dT,
        uint256 _buckKMin, uint256 _buckKMax, uint256 _buckK,
        address _governance
    ) BuckKControllerDirect(_Kp, _Ki, _Kd, _dT, _buckKMin, _buckKMax, _buckK, _governance) {}

    /// @notice Governance: wire (or re-wire; address(0) unwires) the observer.
    /// @dev    Switching observers steps the process variable; `reprime()` is
    ///         the basket's to call, so govern the switch at a quiet moment.
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

    function _readReferences() internal view override
        returns (int256 buckValue, int256 basketValue)
    {
        buckValue = UNIT;
        if (address(observer) != address(0)) {
            basketValue = observer.shadowValueInBuck();
        } else if (address(basket) != address(0)) {
            basketValue = basket.basketValueInBuck();
        } else {
            basketValue = UNIT;
        }
    }

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
}
