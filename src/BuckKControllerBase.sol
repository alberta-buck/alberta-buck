// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title BuckKControllerBase -- Abstract PID core for BUCK_K stabilization.
///
/// @notice Concentrates everything universal to a BUCK_K PID:
///
///           * P/I/D state, anti-windup, dT pacing, dTMax clamp
///           * Constructor-time priming for steady-state continuity
///           * dS-corrected derivative (strips setpoint movement)
///           * Counter-cyclical insurance fundingFactor() view
///           * Governance hooks (setGains, setDT, setDTMax)
///
///         SIGN CONVENTION
///         ---------------
///         error = buckValue - basketValue
///
///           BUCK BELOW basket (inflation):   error < 0 -> buckK DECREASES,
///                                            contracting credit, inducing
///                                            voluntary burns, pulling BUCK
///                                            price back up.
///
///           BUCK ABOVE basket (deflation):   error > 0 -> buckK INCREASES,
///                                            expanding credit, encouraging
///                                            new mints + sales, pulling BUCK
///                                            price back down.
///
///         All shipped gains should be POSITIVE (Kp, Ki, Kd >= 0).
///
///         SUBCLASS RESPONSIBILITY
///         -----------------------
///         Override `_readReferences()` to return the embodiment's current
///         (buckValue, basketValue) pair.  Both are 18-decimal int256.  In
///         the legacy USD-intermediated embodiment, buckValue is the BUCK
///         price in USD and basketValue is the commodity basket cost in USD.
///         In the direct embodiment, buckValue is the constant 1e18 and
///         basketValue is the basket's value denominated in BUCK.
///
///         The base stores the most recently observed pair in
///         `lastBuckPrice` / `lastBasketCost` for fundingFactor() and the
///         next cycle's dS computation.  Field names reflect the legacy
///         embodiment but apply unchanged to direct-mode: BUCK-side
///         reference and basket-side reference respectively.
abstract contract BuckKControllerBase {

    // --- PID Gains (governance-set, 18-decimal fixed point) ---
    int256 public Kp;
    int256 public Ki;
    int256 public Kd;

    // --- PID State ---
    int256 public P;
    int256 public I;
    int256 public D;
    uint256 public lastUpdate;

    /// @notice Always true after construction.  Retained for ABI compatibility
    ///         with earlier two-phase priming designs.  See constructor.
    bool public primed;

    /// @notice Last observed basket-side reference (18-dec).  Used for
    ///         setpoint-shift `dS` correction and as the denominator of
    ///         fundingFactor().
    int256 public lastBasketCost;

    /// @notice Last observed BUCK-side reference (18-dec).  Compared against
    ///         lastBasketCost in fundingFactor().
    int256 public lastBuckPrice;

    // --- Output ---
    uint256 public buckK;     // Current BUCK_K (18-decimal, 1e18 = 1.0)
    uint256 public dT;        // Minimum seconds between PID state updates

    /// @notice Maximum effective dt (seconds) used for integration /
    ///         derivative.  See setDTMax for rationale.
    uint256 public dTMax;

    // --- Output Limits (anti-windup) ---
    uint256 public buckKMin;
    uint256 public buckKMax;

    int256 constant UNIT = 1e18;
    address public governance;

    event BuckKUpdated(uint256 newBuckK, int256 error, int256 P, int256 I, int256 D);
    event GainsUpdated(int256 Kp, int256 Ki, int256 Kd);

    constructor(
        int256 _Kp, int256 _Ki, int256 _Kd,
        uint256 _dT,
        uint256 _buckKMin, uint256 _buckKMax, uint256 _buckK,
        address _governance
    ) {
        Kp = _Kp; Ki = _Ki; Kd = _Kd;
        dT = _dT;
        buckKMin = _buckKMin;
        buckKMax = _buckKMax;
        governance = _governance;
        buckK = _buckK;
        lastUpdate = block.timestamp;
        dTMax = type(uint256).max;

        // Constructor-time priming: assume error = 0 at deploy, references
        // at UNIT.  Pre-load I so the first compute() at parity reproduces
        // the initial buckK.  Steady-state algebra (with P = 0, D = 0):
        //     buckK = UNIT + (I * Ki) / UNIT
        //   => I = ((buckK - UNIT) * UNIT) / Ki
        if (_Ki != 0) {
            I = ((int256(_buckK) - UNIT) * UNIT) / _Ki;
        }
        lastBasketCost = UNIT;
        lastBuckPrice  = UNIT;
        primed         = true;
    }

    // --- Subclass extension surface --------------------------------------- //

    /// @notice Subclasses return the current (buckValue, basketValue) pair.
    /// @dev    Both 18-dec int256.  error = buckValue - basketValue.
    function _readReferences() internal view virtual
        returns (int256 buckValue, int256 basketValue);

    /// @dev Optional hook: subclasses can override to act on the cycle's
    ///      observed references after they've been cached.  Default is a
    ///      no-op.  The base has already written lastBuckPrice /
    ///      lastBasketCost by the time this fires.
    function _onCycleCommit(int256 /*buckValue*/, int256 /*basketValue*/)
        internal virtual {}

    // --- PID cycle -------------------------------------------------------- //

    /// @notice Run (or cache) one PID cycle and return the current BUCK_K.
    /// @dev    Permissionless; cheap cached-read when dT has not elapsed.
    function compute() external returns (uint256) {
        uint256 elapsed = block.timestamp - lastUpdate;
        if (elapsed < dT) {
            return buckK;
        }

        (int256 buckValue, int256 basketValue) = _readReferences();
        int256 error = buckValue - basketValue;

        // Clamp the effective integration step but advance lastUpdate to
        // real block.timestamp so the next cycle measures forward correctly.
        uint256 effective = elapsed > dTMax ? dTMax : elapsed;
        int256 dt = int256(effective);

        // Setpoint shift since last cycle.  With error = buckValue -
        // basketValue, an upward move in basketValue DECREASES error by
        // an equal amount, so the setpoint contribution to
        // (error - P_prev) is -(basketValue - lastBasketCost).
        // Subtracting that from (error - P_prev) leaves only the
        // process change (d(buckValue)/dt) for the derivative term.
        int256 dS = lastBasketCost - basketValue;

        int256 newP = error;
        int256 newI = I + error * dt / UNIT;
        int256 newD = dt > 0
            ? (error - P - dS) * UNIT / dt
            : int256(0);

        int256 rawOutput = UNIT
            + newP * Kp / UNIT
            + newI * Ki / UNIT
            + newD * Kd / UNIT;

        // Anti-windup clamping
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

        P              = newP;
        D              = newD;
        buckK          = newBuckK;
        lastUpdate     = block.timestamp;
        lastBasketCost = basketValue;
        lastBuckPrice  = buckValue;

        _onCycleCommit(buckValue, basketValue);

        emit BuckKUpdated(newBuckK, error, P, I, D);
        return newBuckK;
    }

    /// @notice Current BUCK_K without updating state (view-only).
    function currentBuckK() external view returns (uint256) {
        return buckK;
    }

    /// @notice Counter-cyclical insurance funding factor (18-dec; 1e18 = 1.0).
    ///
    /// @dev   factor = max(0, 1e18 + 10 * (basketValue - buckValue) * 1e18 / basketValue)
    ///
    ///        Saturates at 0 once basketValue <= 0.9 * buckValue (BUCK
    ///        sufficiently overvalued); reaches 2x at basketValue = 1.1 *
    ///        buckValue (BUCK 10% undervalued).
    function fundingFactor() external view returns (uint256) {
        int256 b = lastBasketCost;
        if (b <= 0) return uint256(UNIT);
        int256 p = lastBuckPrice;
        int256 raw = UNIT + int256(10) * (b - p) * UNIT / b;
        if (raw <= 0) return 0;
        return uint256(raw);
    }

    // --- Re-prime (privileged) -------------------------------------------- //

    /// @dev Re-capture the current state and re-derive I so the next cycle
    ///      with no further error change reproduces the current buckK.
    ///      Subclasses expose this through a privileged external function;
    ///      the direct embodiment uses it to absorb the discontinuity in
    ///      basketValue when governance adds a basket constituent.
    function _reprime() internal {
        (int256 buckValue, int256 basketValue) = _readReferences();
        int256 error = buckValue - basketValue;
        P = error;
        if (Ki != 0) {
            I = ((int256(buckK) - UNIT) * UNIT - error * Kp) / Ki;
        }
        lastBuckPrice  = buckValue;
        lastBasketCost = basketValue;
        lastUpdate     = block.timestamp;
    }

    // --- Governance ------------------------------------------------------- //

    function setGains(int256 _Kp, int256 _Ki, int256 _Kd) external {
        require(msg.sender == governance, "Not governance");
        Kp = _Kp; Ki = _Ki; Kd = _Kd;
        emit GainsUpdated(_Kp, _Ki, _Kd);
    }

    function setDT(uint256 _dT) external {
        require(msg.sender == governance, "Not governance");
        dT = _dT;
    }

    /// @notice Cap the effective integration window for any single PID
    ///         cycle.  Pass type(uint256).max to disable.
    function setDTMax(uint256 _dTMax) external {
        require(msg.sender == governance, "Not governance");
        require(_dTMax >= dT, "dTMax<dT");
        dTMax = _dTMax;
    }
}
