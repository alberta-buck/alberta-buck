// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "@chainlink/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol";

/// @title BuckKController — On-Chain PID Value Stabilization
/// @notice Maintains purchasing-power parity between BUCK and its commodity basket
///         via a PID controller computing a dynamic credit-limit multiplier (BUCK_K).
contract BuckKController {

    // --- PID Gains (governance-set, 18-decimal fixed point) ---
    int256 public Kp;
    int256 public Ki;
    int256 public Kd;

    // --- PID State ---
    int256 public P;
    int256 public I;
    int256 public D;
    uint256 public lastUpdate;

    // --- Output ---
    uint256 public buckK;     // Current BUCK_K (18-decimal, 1e18 = 1.0)
    uint256 public dT;        // Minimum seconds between PID state updates

    // --- Output Limits (anti-windup) ---
    uint256 public buckKMin;
    uint256 public buckKMax;

    // --- Oracle Configuration ---
    address public buckUsdcPool;    // Uniswap V3 BUCK/USDC pool
    uint32  public twapInterval;    // TWAP window in seconds

    struct BasketComponent {
        AggregatorV3Interface feed;
        uint256 weight;         // 18 decimals, sum = 1e18
        uint8   feedDecimals;
    }
    BasketComponent[] public basket;

    int256 constant UNIT = 1e18;
    address public governance;

    event BuckKUpdated(uint256 newBuckK, int256 error, int256 P, int256 I, int256 D);
    event GainsUpdated(int256 Kp, int256 Ki, int256 Kd);

    constructor(
        int256 _Kp, int256 _Ki, int256 _Kd,
        uint256 _dT,
        uint256 _buckKMin, uint256 _buckKMax,
        address _buckUsdcPool, uint32 _twapInterval,
        address _governance
    ) {
        Kp = _Kp; Ki = _Ki; Kd = _Kd;
        dT = _dT;
        buckKMin = _buckKMin;
        buckKMax = _buckKMax;
        buckUsdcPool = _buckUsdcPool;
        twapInterval = _twapInterval;
        governance = _governance;
        buckK = uint256(UNIT);  // Start at 1.0 (neutral)
        lastUpdate = block.timestamp;
    }

    /// @notice Compute and return the current BUCK_K value.
    /// @dev If dT has elapsed, performs a full PID cycle (oracle reads + state update).
    ///      Otherwise returns cached buckK.  The minter pays gas for any PID update.
    function compute() external returns (uint256) {
        if (block.timestamp - lastUpdate < dT) {
            return buckK;
        }

        int256 basketCost = _getBasketCost();
        int256 buckPrice  = _getBuckPrice();

        int256 dt = int256(block.timestamp - lastUpdate);
        int256 error = basketCost - buckPrice;

        int256 newP = error;
        int256 newI = I + error * dt / UNIT;
        int256 newD = dt > 0
            ? (error - P) * UNIT / dt
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

        P = newP;
        D = newD;
        buckK = newBuckK;
        lastUpdate = block.timestamp;

        emit BuckKUpdated(newBuckK, error, P, I, D);
        return newBuckK;
    }

    /// @notice Current BUCK_K without updating state (view-only).
    function currentBuckK() external view returns (uint256) {
        return buckK;
    }

    // --- Oracle Helpers ---

    function _getBasketCost() internal view returns (int256) {
        int256 total = 0;
        for (uint i = 0; i < basket.length; i++) {
            BasketComponent storage comp = basket[i];
            (, int256 price,,,) = comp.feed.latestRoundData();
            int256 normalized = price * int256(10 ** (18 - comp.feedDecimals));
            total += normalized * int256(comp.weight) / UNIT;
        }
        return total;
    }

    /// @dev BUCK/USDC price from Uniswap V3 TWAP oracle.
    ///      Placeholder — production uses OracleLibrary.consult().
    function _getBuckPrice() internal view returns (int256) {
        // TODO: Implement with Uniswap V3 OracleLibrary
        // For testing, override this via a mock or vm.mockCall
        revert("_getBuckPrice: implement with OracleLibrary");
    }

    // --- Governance ---

    function setGains(int256 _Kp, int256 _Ki, int256 _Kd) external {
        require(msg.sender == governance, "Not governance");
        Kp = _Kp; Ki = _Ki; Kd = _Kd;
        emit GainsUpdated(_Kp, _Ki, _Kd);
    }

    function setDT(uint256 _dT) external {
        require(msg.sender == governance, "Not governance");
        dT = _dT;
    }

    function addBasketComponent(address feed, uint256 weight, uint8 decimals) external {
        require(msg.sender == governance, "Not governance");
        basket.push(BasketComponent({
            feed: AggregatorV3Interface(feed),
            weight: weight,
            feedDecimals: decimals
        }));
    }
}
