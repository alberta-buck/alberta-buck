// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "../../src/BN254.sol";
import {IdentityRegistry} from "../../src/IdentityRegistry.sol";

/// @notice Test-only IdentityRegistry that restores the pre-P1-B uncertified
///         bindContract path.  Fixture tests that need a dummy (pk, E) on a
///         contract (Notes, Buck, baskets, ...) deploy this subclass; production
///         IdentityRegistry and IdentityRegistry.t.sol use the certified API.
///
///         register() is inherited unchanged (still requires a real credential).
contract IdentityRegistryHarness is IdentityRegistry {
    constructor(address _governance) IdentityRegistry(_governance) {}

    /// @dev Old 5-arg bind: deployed + unbound, then store caller-supplied
    ///      (pk, E, flags).  No credential, no registered-binder check.
    function bindContract(
        address target,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_
    ) external override {
        _fixtureBind(target, pk, E, isPublicIdentity_, isCarrying_, 0);
    }

    /// @dev Old 6-arg bind, including unconstrained identityLeaf insert.
    ///      Fixture E2E tests replay a known tree; production refuses this.
    function bindContract(
        address target,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_,
        uint256 identityLeaf
    ) external override {
        _fixtureBind(target, pk, E, isPublicIdentity_, isCarrying_, identityLeaf);
    }

    function _fixtureBind(
        address target,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_,
        uint256 identityLeaf
    ) internal {
        require(target.code.length > 0, "target not a deployed contract");
        require(!_isRegistered(target), "already bound");

        _pk[target]              = pk;
        _E_addr[target]          = E;
        isPublicIdentity[target] = isPublicIdentity_;
        isCarrying[target]       = isCarrying_;
        binderOf[target]         = msg.sender;
        emit ContractBound(target, msg.sender, isPublicIdentity_);
        emit CarryingFlagSet(target, isCarrying_);

        if (identityLeaf != 0 && identityPoseidon != address(0)) {
            uint256 newRoot = _insertIdentityLeaf(identityLeaf);
            emit IdentityRootUpdated(identityRoot, newRoot);
            identityRoot = newRoot;
        }
    }

    /// @notice Admit one leaf that is an ASSOCIATION rather than an account
    ///         binding -- a mailbox leaf, say, which commits an Identity and
    ///         the receiving key its Notes are addressed to.  Such leaves have
    ///         no address of their own, so they cannot arrive through a bind.
    ///
    ///         On a deployment they arrive the way every leaf does: the
    ///         organisation admits them to its subtree and governance posts the
    ///         composed root.  The harness inserts them directly only so a
    ///         fixture's tree can be replayed incrementally on chain.
    function fixtureInsertLeaf(uint256 leaf) external {
        require(leaf != 0, "zero leaf");
        uint256 newRoot = _insertIdentityLeaf(leaf);
        emit IdentityRootUpdated(identityRoot, newRoot);
        identityRoot = newRoot;
    }
}
