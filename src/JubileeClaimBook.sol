// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckCredit} from "./BuckCredit.sol";
import {JubileeClaims} from "./lib/JubileeClaims.sol";

/// @notice Bounded aggregate of capped BuckCredit claims for one BuckNet.
/// @dev The token alone can synchronize a facility; this contract fetches its
///      principal and claimable seconds directly from its immutable credit
///      contract. Anyone can process matured entries in bounded batches.
///      Exact views and sync revert until a maturity backlog is cleared.
contract JubileeClaimBook {
    using JubileeClaims for JubileeClaims.Book;

    address public immutable buck;
    BuckCredit public immutable credit;
    JubileeClaims.Book private _book;
    error NotBuck();

    constructor(address buck_, address credit_) {
        buck = buck_;
        credit = BuckCredit(credit_);
        _book.initialize();
    }

    function sync(uint256 tokenId) external {
        if (msg.sender != buck) revert NotBuck();
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(tokenId);
        _book.update(tokenId, principal, seconds_);
    }

    function process(uint256 maxSteps) external returns (uint256 processed, bool current) {
        return _book.process(maxSteps);
    }
    function totalSeconds() external view returns (uint256) { return _book.totalSeconds(); }
    function pending() external view returns (bool) { return _book.pending(); }
    function nextMaturity() external view returns (uint256 tokenId, uint256 at) { return _book.nextMaturity(); }
    function positionSeconds(uint256 tokenId) external view returns (uint256) {
        return _book.positionSeconds(tokenId);
    }
}
