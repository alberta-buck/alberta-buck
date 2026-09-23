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
    // ---- a flat incremental accumulator, for fixture replay only ----------
    //
    // A deployment never inserts a leaf through the registry: authorities
    // admit leaves to their own subtrees and the aggregator posts the composed
    // root.  Some fixtures replay a flat tree leaf by leaf instead; this does
    // that, and posts each new root through the same ring a real posting uses.

    /// @notice Leaves replayed so far.
    uint32                       public  identityNextLeafIndex;
    uint256[IDENTITY_TREE_DEPTH] internal _identityFilledSubtrees;

    /// @dev The empty-subtree ladder ZERO_{d+1} = Poseidon(ZERO_d, ZERO_d),
    ///      ZERO_0 = 0, computed on first insertion rather than carried as
    ///      constants: insertion needs the Poseidon contract anyway, and the
    ///      constants would push the harness past the EIP-170 size limit.
    uint256[IDENTITY_TREE_DEPTH] internal _zeros;

    constructor(address _governance) IdentityRegistry(_governance) {}

    /// @dev Tornado-style insertion: the new root.
    function _insertIdentityLeaf(uint256 leaf) internal returns (uint256) {
        uint256 index = identityNextLeafIndex;
        if (index == 0) {
            uint256 z = 0;
            for (uint8 d = 0; d < IDENTITY_TREE_DEPTH; d++) {
                _zeros[d] = z;
                _identityFilledSubtrees[d] = z;
                z = _hashPair(z, z);
            }
        }
        require(index < (uint256(1) << IDENTITY_TREE_DEPTH), "id tree full");
        uint256 current = leaf;
        for (uint8 d = 0; d < IDENTITY_TREE_DEPTH; d++) {
            if (index & 1 == 0) {
                _identityFilledSubtrees[d] = current;
                current = _hashPair(current, _zeros[d]);
            } else {
                current = _hashPair(_identityFilledSubtrees[d], current);
            }
            index >>= 1;
        }
        identityNextLeafIndex++;
        return current;
    }

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
            _postRoot(_insertIdentityLeaf(identityLeaf), bytes32(0));
        }
    }

    /// @notice Admit one leaf that is an ASSOCIATION rather than an account
    ///         binding -- a mailbox leaf, say, which commits an Identity and
    ///         the receiving key its Notes are addressed to.  Such leaves have
    ///         no address of their own, so they cannot arrive through a bind.
    ///
    ///         On a deployment they arrive the way every leaf does: the
    ///         organisation admits them to its subtree and the aggregator posts
    ///         the composed root.  The harness inserts them directly only so a
    ///         fixture's flat tree can be replayed incrementally on chain.
    function fixtureInsertLeaf(uint256 leaf) external {
        require(leaf != 0, "zero leaf");
        _postRoot(_insertIdentityLeaf(leaf), bytes32(0));
    }

    /// @notice Post a root without appointing a root authority and aggregator.
    ///         A fixture shortcut: suites whose subject is not the accumulator
    ///         need a live root, not the roles that post one.
    function fixturePostRoot(uint256 root) external {
        _postRoot(root, bytes32(0));
    }
}
