// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckCredit} from "../../src/BuckCredit.sol";

/// @notice Test-only subclass of BuckCredit that exposes a force-activate
///         entry point bypassing the production Buck.mint() funding-factor /
///         pool-principal payment flow.
///
/// @dev    Production BuckCredit has *no* public activation function -- all
///         activation flows through Buck.mint() which atomically pays the
///         pool principal premium and grows activatedValue in lockstep with
///         mintsBacked.  That's the load-bearing invariant ("a policy is a
///         one-time upfront purchase; the 10x pool principal funds the
///         annual premium at 10% ROI in perpetuity").
///
///         For test setups that need a holder with non-zero creditLimit but
///         aren't exercising the mint() purchase path (e.g., tests of
///         transfer, demurrage, identity, or BuckBasket flows that just
///         need somebody to have credit), this harness lets the test
///         contract poke activatedValue directly via the internal
///         _activate() helper.  Production deployments deploy vanilla
///         BuckCredit; tests deploy BuckCreditHarness.
contract BuckCreditHarness is BuckCredit {
    /// @notice Force-activate `amount` of coverage on `tokenId`, bypassing
    ///         Buck.mint()'s funding-factor / pool-principal flow.
    ///         Test-only -- not present on production BuckCredit.
    function forceActivate(uint256 tokenId, uint256 amount) external {
        _activate(tokenId, ownerOf(tokenId), amount);
    }
}
