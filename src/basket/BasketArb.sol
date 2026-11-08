// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityStorage} from "./BuckBasketEquityStorage.sol";
import {IBasketArb} from "./IBasketArb.sol";
import {BasketMarketOps} from "./BasketMarketOps.sol";

/// @notice Shared by the two existing upkeep facets. Arb uses actual surplus
/// cash, never new issuance, LP principal withdrawal or the caller reserve.
abstract contract BasketArb is BuckBasketEquityStorage, IBasketArb {
    error CanonicalArbRequired();
    error BasketArbReentrant();

    modifier arbLock() {
        if (_positionEntered || _creditEntered) revert BasketArbReentrant();
        _positionEntered = true;
        _;
        _positionEntered = false;
    }

    function basketArbPlan(uint256 route) external view returns (ArbPlan memory) {
        if (!netAccounting) revert CanonicalArbRequired();
        return BasketMarketOps.plan(address(buck), wheel, route);
    }

    /// @dev PRE: configured wheel, canonical basket, no position or
    /// credit operation in progress. POST: a complete, cash-funded simple
    /// cycle improves net wallet + LP value at the same frozen TOKEN marks;
    /// fees count once, no credit shortfall grows, and actual collateral is
    /// refreshed. Any refusal rolls back all swaps, fee collections and books.
    function executeBasketArb(uint256 route, uint8 direction, uint256 amount, uint256 minimumGain)
        external arbLock returns (ArbPlan memory result)
    {
        if (msg.sender != wheel || wheel == address(0)) revert NotWheel();
        if (!netAccounting) revert CanonicalArbRequired();
        controller.compute();
        uint256 shortfall = _shortfallS(_snap());
        result = BasketMarketOps.execute(address(buck), wheel, route, direction, amount, minimumGain, idleToken);
        _finishBuy(shortfall);
    }
}
