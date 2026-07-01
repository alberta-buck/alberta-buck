// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "./BuckKControllerBase.sol";

interface IBuckBasketRef {
    /// @notice Sum of `basketAmount_i * pool_price_in_BUCK_i` across all
    ///         basket constituents, expressed in 18-dec BUCK.
    function basketValueInBuck() external view returns (int256);
}

/// @title BuckKControllerDirect -- USD-free BUCK_K stabilization.
///
/// @notice The direct embodiment of the BUCK_K PID.  No external stablecoin
///         intermediary; BUCK measures itself against a basket of RWA-token
///         / BUCK Uniswap V3 pools.  Setpoint is the constant 1.0 BUCK by
///         definition; the process variable is the BuckBasket-published
///         basket value.
///
///         RESCALED PID (ppm process/setpoint, seconds time)
///         -------------------------------------------------
///         The base PID keeps references in 1e18 and its integrator
///         increment (`error*dt/UNIT`) underflows ~12 orders below the scale
///         its gains expect -- so `I` never moves.  This embodiment runs the
///         loop in a sane, linear range instead:
///
///           setpoint = buckValue  / 1e12   (== 1_000_000 ppm, i.e. 1.0 BUCK)
///           process  = basketValue / 1e12  (~ 1_000_000 ppm)
///           error    = setpoint - process  (ppm; -50_000 == basket 5% rich)
///           dt in seconds
///
///         Gains are stored as `real_gain * 1e12` so the output lands
///         directly in 1e18 buckK units:
///
///           uP = Kp * error            (Kp = Kp_real * 1e12)
///           uI = Ki * integral         (Ki = Ki_real * 1e12; integral in ppm*s)
///           uD = Kd * dError / dt       (Kd = Kd_real * 1e12)
///           buckK = clamp(buckK0 + uP + uI + uD, buckKMin, buckKMax)
///
///         `buckK0` is the neutral feed-forward LTV (e.g. 0.5 == 2x max
///         leverage); the integrator trims K away from it to hold parity and
///         `I` is primed to 0 at deploy.  Choose Ki for a target
///         "max variance before the rail":  Ki = dK_rail / (e_max * tau_I).
///
///         DERIVATIVE
///         ----------
///         In direct mode the setpoint is the CONSTANT 1.0 BUCK, so there is
///         no setpoint motion to strip -- the base's `dS` correction (written
///         for the USD embodiment, where basketValue *is* the setpoint) would
///         cancel the entire process derivative and pin D at 0.  This override
///         drops `dS`: D is the true error derivative, and Kd is a live (if
///         normally tiny) knob.
///
///         Sign convention is inherited from BuckKControllerBase:
///
///           error = buckValue - basketValue = 1.0 - basketValue
///
///           - basketValue > 1.0 (BUCK undervalued, inflation):
///                error < 0  ->  buckK DECREASES  ->  credit contracts.
///           - basketValue < 1.0 (BUCK overvalued, deflation):
///                error > 0  ->  buckK INCREASES  ->  credit expands.
///
///         BuckBasket is the privileged caller of `reprime()` -- it invokes
///         that hook after `addBasketToken` so the dilution-induced jump in
///         basketValue doesn't manifest as a single-cycle P/I spike.
contract BuckKControllerDirect is BuckKControllerBase {

    IBuckBasketRef public basket;

    /// @notice Neutral feed-forward BUCK_K (the resting LTV at parity).  The
    ///         integrator trims K away from this to hold basketValue == 1.0.
    uint256 public buckK0;

    int256 private constant TO18 = 1e12;   // ppm -> 1e18
    int256 private constant PPM  = 1e6;    // 1.0 expressed in ppm

    event BasketSet(address indexed basket);
    event Reprimed(int256 newP, int256 newI);

    constructor(
        int256 _Kp, int256 _Ki, int256 _Kd,
        uint256 _dT,
        uint256 _buckKMin, uint256 _buckKMax, uint256 _buckK,
        address _governance
    ) BuckKControllerBase(_Kp, _Ki, _Kd, _dT, _buckKMin, _buckKMax, _buckK, _governance) {
        // Feed-forward from the initial K; integrator rests at 0 (parity).
        // Overrides the base's 1e18-scale priming, which does not apply here.
        buckK0         = _buckK;
        I              = 0;
        P              = 0;
        D              = 0;
        lastBasketCost = PPM;   // ppm parity, for fundingFactor()
        lastBuckPrice  = PPM;
    }

    /// @notice One-shot wiring from governance after BuckBasket is deployed.
    /// @dev    Locked once set; deploying a new BuckBasket requires a new
    ///         controller.
    function setBasket(address _basket) external {
        require(msg.sender == governance, "Not governance");
        require(address(basket) == address(0), "basket already set");
        require(_basket != address(0), "basket=0");
        basket = IBuckBasketRef(_basket);
        emit BasketSet(_basket);
    }

    function _readReferences() internal view override
        returns (int256 buckValue, int256 basketValue)
    {
        buckValue = UNIT;
        basketValue = (address(basket) != address(0))
                    ? basket.basketValueInBuck()
                    : UNIT;
    }

    /// @notice Run (or cache) one PID cycle in ppm space and return buckK.
    /// @dev    Permissionless; cheap cached-read when dT has not elapsed.
    function compute() external override returns (uint256) {
        uint256 elapsed = block.timestamp - lastUpdate;
        if (elapsed < dT) {
            return buckK;
        }

        (int256 buckValue, int256 basketValue) = _readReferences();
        int256 setpoint = buckValue   / TO18;   // ppm (== PPM in direct mode)
        int256 process  = basketValue / TO18;   // ppm
        int256 err      = setpoint - process;   // ppm

        uint256 effective = elapsed > dTMax ? dTMax : elapsed;
        int256  dt = int256(effective);

        int256 newI = I + err * dt;             // ppm*seconds
        int256 dErr = err - P;                  // ppm  (P holds previous err)

        int256 uP = Kp * err;                   // 1e18
        int256 uI = Ki * newI;                  // 1e18
        int256 uD = dt > 0 ? Kd * dErr / dt : int256(0);  // 1e18

        int256 rawOutput = int256(buckK0) + uP + uI + uD;

        // Anti-windup: at a rail, only let the integrator move back toward
        // the operating band (never wind further into the clamp).
        uint256 newBuckK;
        if (rawOutput < int256(buckKMin)) {
            newBuckK = buckKMin;
            if (newI > I) I = newI;
        } else if (rawOutput > int256(buckKMax)) {
            newBuckK = buckKMax;
            if (newI < I) I = newI;
        } else {
            newBuckK = uint256(rawOutput);
            I = newI;
        }

        P              = err;
        D              = dErr;
        buckK          = newBuckK;
        lastUpdate     = block.timestamp;
        lastBasketCost = process;               // ppm, for fundingFactor()
        lastBuckPrice  = setpoint;              // ppm

        emit BuckKUpdated(newBuckK, err, P, I, D);
        return newBuckK;
    }

    /// @notice Privileged: BuckBasket calls this after addBasketToken to
    ///         absorb the dilution discontinuity in basketValue.  Recaptures
    ///         P against the new process state and re-derives I so the next
    ///         no-error cycle reproduces the current buckK.
    function reprime() external {
        require(msg.sender == address(basket), "only basket");
        (int256 buckValue, int256 basketValue) = _readReferences();
        int256 setpoint = buckValue   / TO18;
        int256 process  = basketValue / TO18;
        int256 err      = setpoint - process;

        P = err;
        D = 0;
        if (Ki != 0) {
            // buckK == buckK0 + Kp*err + Ki*I  (uD == 0 at a no-motion cycle)
            I = (int256(buckK) - int256(buckK0) - Kp * err) / Ki;
        }
        lastBasketCost = process;
        lastBuckPrice  = setpoint;
        lastUpdate     = block.timestamp;
        emit Reprimed(P, I);
    }
}
