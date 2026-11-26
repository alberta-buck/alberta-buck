// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {PortfolioMath as PM} from "./PortfolioMath.sol";

/// @notice Pure allocation and sizing used by the basket's upkeep facet.
library BasketRebalanceMath {
    // Sizing flags for confirmed fills. Tied local caps can both bind.
    uint8 internal constant STEP_LIMIT = 1;
    uint8 internal constant DEPTH_LIMIT = 2;
    uint8 internal constant PORTFOLIO_LIMIT = 4;

    function _limitLeg(uint256 requested, uint256 step, uint256 depth, uint256 grain)
        private pure returns (uint256 amount, uint8 flags)
    {
        amount = Math.min(requested, Math.min(step, depth));
        if (amount <= 2 * grain) return (0, 0);
        if (amount < requested) {
            if (amount == step) flags |= STEP_LIMIT;
            if (amount == depth) flags |= DEPTH_LIMIT;
        }
    }

    /// @notice Apply the basket's trade caps and report which reduced the requested sizes.
    function limitRebalance(uint256[] memory sells, uint256[] memory buys, uint256[] memory held,
        uint256[] memory target, uint256[] memory depth, uint256 stepBp, uint256 grain, int256 headroom)
        public pure returns (uint256[] memory, uint256[] memory, uint8[] memory sellLimits, uint8[] memory buyLimits)
    {
        uint256 n = sells.length;
        sellLimits = new uint8[](n);
        buyLimits = new uint8[](n);
        uint256 selling;
        uint256 buying;
        for (uint256 i; i < n; ++i) {
            (sells[i], sellLimits[i]) = _limitLeg(sells[i], held[i] * stepBp / 10000, depth[i], grain);
            (buys[i], buyLimits[i]) = _limitLeg(buys[i], target[i] * stepBp / 10000, depth[i], grain);
            selling += sells[i];
            buying += buys[i];
        }
        int256 line = headroom + int256(selling);
        uint256 budget = line > 0 ? uint256(line) : 0;
        if (buying > budget) {
            for (uint256 i; i < n; ++i) {
                if (buys[i] == 0) continue;
                buys[i] = buys[i] * budget / buying;
                buyLimits[i] |= PORTFOLIO_LIMIT;
            }
        }
        return (sells, buys, sellLimits, buyLimits);
    }

    /// @notice Allocate a book-wide expansion or contraction in proportion to each eligible gap.
    function rebalanceMove(uint256[] memory held, uint256[] memory target, uint256 total,
        uint256 tokens, uint256 excess, uint256 bandBp) public pure returns (uint256[] memory move)
    {
        uint256 n = held.length;
        move = new uint256[](n);
        bool grow = total > tokens;
        uint256 amount = grow ? total - tokens : tokens - total;
        if (amount * 10000 <= total * bandBp) return move;
        if (!grow) amount = amount > excess ? amount - excess : 0;
        uint256 sum;
        for (uint256 i; i < n; ++i) {
            if (grow ? target[i] > held[i] : held[i] > target[i])
                move[i] = grow ? target[i] - held[i] : held[i] - target[i];
            sum += move[i];
        }
        if (sum == 0) return move;
        for (uint256 i; i < n; ++i) move[i] = amount * move[i] / sum;
    }

    /// @notice Tilt declared weights by relative price and clamp the resulting exposures.
    function rebalanceTargets(uint256[] memory weights, uint256[] memory prices, uint256[] memory anchors,
        uint256 total, uint256 tilt, uint256 leanCapBp) public pure returns (uint256[] memory target)
    {
        uint256 n = weights.length;
        target = new uint256[](n);
        uint256 sum;
        for (uint256 i; i < n; ++i) {
            int256 above = prices[i] == 0 || anchors[i] == 0 ? int256(0) : PM.lnWad(prices[i] * 1e18 / anchors[i]);
            target[i] = weights[i] * PM.expWad(-above * int256(tilt) / 10000);
            sum += target[i];
        }
        for (uint256 i; i < n; ++i) {
            uint256 declared = total * weights[i] / 10000;
            uint256 most = declared * leanCapBp / 10000;
            uint256 leaned = total * target[i] / sum;
            target[i] = leaned > declared + most ? declared + most : leaned + most < declared ? declared - most : leaned;
        }
    }
}
