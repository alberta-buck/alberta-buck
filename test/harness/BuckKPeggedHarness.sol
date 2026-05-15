// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "../../src/BuckKController.sol";

interface IV2Pair {
    function token0() external view returns (address);
    function getReserves() external view returns (uint112, uint112, uint32);
}

/// @notice Test harness: V2 BUCK/USDC reserves drive the BUCK reference price;
///         basket is pegged to $1.00 (18-dec).  Used by the equilibrium
///         scenario test which spins a real Buck + UniswapV2 pool to
///         demonstrate the fundingFactor / buckK feedback loop.
///
///         Real BUCK is 6-decimal (see BuckTypes.DECIMALS); USDC is 6-decimal;
///         so price = usdcReserve * 1e18 / buckReserve directly yields the
///         18-decimal USD price per 1 whole BUCK with no further normalization.
///         For other pair configurations, override _getBuckPrice in a subclass.
contract BuckKPeggedHarness is BuckKController {

    address public v2Pair;
    address public buckAddr;

    constructor(
        int256 _Kp, int256 _Ki, int256 _Kd,
        uint256 _dT,
        uint256 _buckKMin, uint256 _buckKMax, uint256 _buckK,
        address _governance
    ) BuckKController(
        _Kp, _Ki, _Kd, _dT,
        _buckKMin, _buckKMax, _buckK,
        address(0),                // V3 oracle pool unused
        0,                         // V3 twap interval unused
        _governance
    ) {}

    /// @notice Wire the V2 BUCK/USDC pair as the BUCK price source.
    function setV2BuckPair(address pair, address buck_) external {
        require(msg.sender == governance, "Not governance");
        v2Pair   = pair;
        buckAddr = buck_;
    }

    /// @dev Pegged basket: $1.00 in 18-dec.  Lets the scenario test isolate
    ///      BUCK pool drift from commodity oracle noise.
    function _getBasketCost() internal pure override returns (int256) {
        return int256(1e18);
    }

    /// @dev Spot price from V2 reserves.  Returns 1e18 (parity) when the
    ///      pair is empty so priming/compute() during early bootstrap don't
    ///      divide by zero -- the first real cycle after liquidity is added
    ///      will see the actual market price.
    function _getBuckPrice() internal view override returns (int256) {
        require(v2Pair != address(0), "BuckKPegged: pair unset");
        (uint112 r0, uint112 r1,) = IV2Pair(v2Pair).getReserves();
        bool buckIs0 = IV2Pair(v2Pair).token0() == buckAddr;
        uint256 buckR  = buckIs0 ? uint256(r0) : uint256(r1);
        uint256 quoteR = buckIs0 ? uint256(r1) : uint256(r0);
        if (buckR == 0 || quoteR == 0) return int256(1e18);
        return int256(quoteR * 1e18 / buckR);
    }

    /// @notice Expose basket cost for test assertions.
    function getBasketCost() external view returns (int256) {
        return _getBasketCost();
    }

    /// @notice Expose BUCK price for test assertions.
    function getBuckPrice() external view returns (int256) {
        return _getBuckPrice();
    }
}
