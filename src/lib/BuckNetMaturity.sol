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
        (, uint256 net) = ledger.fundParts();
        if (principal > net / (10 * Q)) revert ReliefLiquidityLimit();
        nominal = ledger.retireFundDebt(principal);
        claims.consumeDeferred(principal);
    }
}
