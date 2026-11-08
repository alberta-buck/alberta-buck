// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityStorage, IMarkedCredit, IBuckNetAccount} from "./BuckBasketEquityStorage.sol";
import {IBasketUpkeep} from "./IBasketUpkeep.sol";
import {IBuckJubilee} from "../IBuckJubilee.sol";
import {IdentityRegistry} from "../IdentityRegistry.sol";
import {BN254} from "../BN254.sol";
import {BasketArb} from "./BasketArb.sol";
import {BasketMarketOps} from "./BasketMarketOps.sol";

/// @notice Shared setup code in the two existing equity-upkeep facets. No
///         new state or deployed initializer; their storage remains identical
///         to the shell's equity prefix. Setup cannot move BUCK or activate debt.
abstract contract BasketCreditSetup is BasketArb, IBasketUpkeep {
    /// @dev PRE: the governed shell calls itself, no facility is open, and
    ///      the canonical token/model/identity/credit bindings agree. Legacy
    ///      fixtures retain their own token path. POST: one self-insured,
    ///      zero-premium MARKED NFT, initially inactive with zero principal;
    ///      cash/supply/claim age unchanged. No arbitrary fallback caller can
    ///      bypass openCredit's governance check.
    function initializeEquityCredit(address credit_, uint256 face) external override {
        if (msg.sender != address(this)) revert NotSelf();
        if (address(credit) != address(0)) revert AlreadyPresent();
        BasketMarketOps.validateCredit(address(buck), credit_, netAccounting);
        IMarkedCredit c = IMarkedCredit(credit_);
        c.setCreditIssuer(address(this), true);
        creditId = c.createCredit(address(this), 0, face, 0, DEP_MARKED, 0, 0, 0);
        credit = c;
        emit CreditOpened(credit_, creditId, face);
    }

    /// @notice Before receiving LP deposits or opening credit, governance
    ///         authorizes a certified operator to bind the basket's exact
    ///         public ordinary identity. The production registry still checks
    ///         that operator, key, ciphertext, flags and target authorization.
    function authorizeIdentityBinding(IdentityRegistry registry, address binder,
        BN254.G1Point calldata pk, IdentityRegistry.ElGamalCT calldata E) external override onlyGov
    {
        _bindingSetup(registry);
        registry.authorizeContractBinding(binder, pk, E, true, true);
    }

    /// @notice Cancel a pending binding, under the same setup preconditions.
    function revokeIdentityBinding(IdentityRegistry registry) external override onlyGov {
        _bindingSetup(registry);
        registry.revokeContractBindingAuthorization();
    }

    function _bindingSetup(IdentityRegistry registry) private view {
        if (address(credit) != address(0) || totalShares != 0
            || address(registry) != address(IBuckJubilee(address(buck)).identity())) revert BindingSetupClosed();
    }

}
