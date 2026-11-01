// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckCredit} from "../BuckCredit.sol";
import {JubileeClaimBook} from "../JubileeClaimBook.sol";
import {BuckLedger} from "./BuckLedger.sol";

/// @notice Linked maturity operations for the experimental BuckNet token.
/// @dev Library calls execute in BuckNet's storage context. BuckNet must
///      authenticate the caller and hold its reentrancy lock around each call.
library BuckNetMaturity {
    using BuckLedger for BuckLedger.Ledger;
    uint256 private constant Q = 1e36;
    error ReliefLiquidityLimit();

    /// @notice Full-value BUCK, in Q-scaled raw units, that can be spent on
    ///         one protected outflow at the current block and cash balance.
    /// @dev This is a transient quote, not a reservation. The first positive
    ///      outflow snapshots 10% of then-current net cash for the block;
    ///      later inflows cannot replenish that block's remaining budget.
    function availableReliefScaled(BuckLedger.Ledger storage ledger) public view returns (uint256 available) {
        (, uint256 net) = ledger.fundParts();
        available = net / 10;
        BuckLedger.ReliefBudget storage budget = ledger.reliefBudget;
        if (budget.started && budget.blockNumber == block.number && budget.remainingScaled < available) {
            available = budget.remainingScaled;
        }
    }

    /// @dev The host must authenticate and lock the complete operation. A
    ///      later revert rolls this update back together with the settlement.
    function consumeReliefBudget(BuckLedger.Ledger storage ledger, uint256 reliefScaled) public {
        _consumeReliefBudget(ledger, reliefScaled);
    }

    function _consumeReliefBudget(BuckLedger.Ledger storage ledger, uint256 reliefScaled) private {
        if (reliefScaled == 0) return;
        (, uint256 net) = ledger.fundParts();
        uint256 perOperation = net / 10;
        if (reliefScaled > perOperation) revert ReliefLiquidityLimit();
        BuckLedger.ReliefBudget storage budget = ledger.reliefBudget;
        if (!budget.started || budget.blockNumber != block.number) {
            budget.started = true;
            budget.blockNumber = block.number;
            budget.remainingScaled = perOperation;
        }
        if (reliefScaled > budget.remainingScaled) revert ReliefLiquidityLimit();
        budget.remainingScaled -= reliefScaled;
    }

    function close(BuckCredit credit, JubileeClaimBook claims,
                   mapping(uint256 => address) storage pools,
                   mapping(uint256 => uint256) storage premiums,
                   address owner, uint256 tokenId) public returns (uint256 principal)
    {
        principal = claims.deferMatured(tokenId);
        credit.recordReductionFromBuck(tokenId, owner, principal);
        (, uint256 coverage,) = credit.creditInfo(tokenId);
        if (coverage != 0) credit.deactivateFromBuck(tokenId, owner, coverage);
        delete pools[tokenId];
        delete premiums[tokenId];
        credit.closeFromBuck(tokenId, owner);
    }

    function burn(BuckLedger.Ledger storage ledger, JubileeClaimBook claims, uint256 principal)
        public returns (uint256 nominal)
    {
        if (principal > type(uint256).max / Q) revert ReliefLiquidityLimit();
        _consumeReliefBudget(ledger, principal * Q);
        nominal = ledger.retireFundDebt(principal);
        claims.consumeDeferred(principal);
    }
}
