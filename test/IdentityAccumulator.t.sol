// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {BN254} from "../src/BN254.sol";
import {IPoseidonT3} from "../src/IPoseidonT3.sol";
import {PoseidonT3Bytecode} from "../src/PoseidonT3Bytecode.sol";
import {PoseidonT4Bytecode} from "../src/PoseidonT4Bytecode.sol";

/// @notice The accumulator root as phase 4 of the plan builds it: a root
///         authority that enrolls subtrees and appoints an aggregator, a ring
///         of timestamped roots, a maximum age per consumer, and public
///         membership paths whose aggregator index comes from enrollment
///         (doc/review/accumulator-spec.org, sections 5, 11.2 and 17).
contract IdentityAccumulatorTest is Test {
    address constant GOV       = address(0x6011);
    address constant ROOT_AUTH = address(0xA07);
    address constant AGG       = address(0xA99);
    address constant POSTER    = address(0x9057);

    IdentityRegistry reg;
    IPoseidonT3      p3;

    bytes32 constant NOTES   = keccak256("AlbertaBuck/Accumulator/Consumer/NotesMembership/v2");
    bytes32 constant INSURER = keccak256("AlbertaBuck/Accumulator/Consumer/InsurerAttestation/v2");
    bytes32 constant REGULATOR_STANDING = keccak256("regulator:ca-ab:insurer");

    function setUp() public {
        vm.warp(1_800_000_000);
        reg = new IdentityRegistry(GOV);
        p3 = IPoseidonT3(PoseidonT3Bytecode.deploy());
        vm.startPrank(GOV);
        reg.setIdentityPoseidon(address(p3));
        reg.setIdentityPoseidonT4(PoseidonT4Bytecode.deploy());
        reg.setRootAuthority(ROOT_AUTH);
        vm.stopPrank();
        vm.prank(ROOT_AUTH);
        reg.setAggregator(AGG);
    }

    function _post(uint256 root) internal {
        vm.prank(AGG);
        reg.postIdentityRoot(root, keccak256(abi.encode("leaf list", root)));
    }

    function _h(uint256 l, uint256 r) internal view returns (uint256) {
        return p3.poseidon([l, r]);
    }

    // ---- roles -------------------------------------------------------------

    function test_onlyGovernanceAppointsTheRootAuthority() public {
        vm.expectRevert(bytes("not governance"));
        reg.setRootAuthority(address(this));
    }

    function test_onlyTheRootAuthorityEnrollsAndAppoints() public {
        vm.expectRevert(bytes("not root authority"));
        reg.setAggregator(address(this));
        vm.prank(GOV);
        vm.expectRevert(bytes("not root authority"));
        reg.enrollSubtree(REGULATOR_STANDING, 3, 10, true, POSTER, "https://example/leaves");
    }

    function test_onlyTheAggregatorPosts() public {
        vm.prank(ROOT_AUTH);
        vm.expectRevert(bytes("not aggregator"));
        reg.postIdentityRoot(7, bytes32(0));
    }

    // ---- enrollment --------------------------------------------------------

    function test_enrollmentClaimsASlotOnce() public {
        vm.startPrank(ROOT_AUTH);
        reg.enrollSubtree(REGULATOR_STANDING, 3, 10, true, POSTER, "https://example/leaves");
        (uint32 slot, uint8 depth, bool enrolled, bool isPublic, address poster) =
            reg.subtrees(REGULATOR_STANDING);
        assertEq(slot, 3);
        assertEq(depth, 10);
        assertTrue(enrolled && isPublic);
        assertEq(poster, POSTER);
        assertEq(reg.subtreeAtSlot(4), REGULATOR_STANDING);

        vm.expectRevert(bytes("subtree enrolled"));
        reg.enrollSubtree(REGULATOR_STANDING, 5, 10, true, POSTER, "");
        vm.expectRevert(bytes("slot taken"));
        reg.enrollSubtree(keccak256("regulator:ca-ab:insurer:face:5"), 3, 10, true, POSTER, "");

        reg.evictSubtree(REGULATOR_STANDING);
        (, , enrolled, , ) = reg.subtrees(REGULATOR_STANDING);
        assertFalse(enrolled);
        reg.enrollSubtree(keccak256("regulator:ca-ab:insurer:face:5"), 3, 10, true, POSTER, "");
        vm.stopPrank();
    }

    // ---- the ring and the consumers' maximum ages ---------------------------

    function test_consumersDeclareTheirOwnAge() public {
        assertEq(reg.maxRootAge(NOTES), 7 days);
        assertEq(reg.maxRootAge(INSURER), 1 days);
        assertEq(reg.maxRootAge(keccak256("anything else")), 7 days);

        _post(11);
        assertTrue(reg.acceptsRoot(11, NOTES));
        assertTrue(reg.acceptsRoot(11, INSURER));
        vm.warp(block.timestamp + 1 days + 1);
        assertTrue(reg.acceptsRoot(11, NOTES), "a registration check tolerates a late batch");
        assertFalse(reg.acceptsRoot(11, INSURER), "an attestation needs a fresh root");
        vm.warp(block.timestamp + 6 days);
        assertFalse(reg.acceptsRoot(11, NOTES));
        assertFalse(reg.acceptsRoot(0, NOTES), "the zero root is never accepted");
        assertFalse(reg.acceptsRoot(12, NOTES), "a root never posted is never accepted");
    }

    function test_theRingEvictsTheOldestAndKeepsARepostedRoot() public {
        uint256 ring = reg.ROOT_RING_SIZE();
        _post(1000);                                 // sequence 0
        _post(2000);                                 // sequence 1
        for (uint256 i = 2; i < ring; i++) _post(5000 + i);
        assertEq(reg.rootPostedAt(1000), block.timestamp, "the ring is full, nothing evicted");
        _post(2000);                                 // sequence 256: evicts sequence 0
        assertEq(reg.rootPostedAt(1000), 0, "the oldest posting is evicted");
        _post(9000);                                 // evicts sequence 1, the OLD 2000
        assertEq(reg.rootPostedAt(2000), block.timestamp,
                 "evicting an older posting keeps a root re-posted since");
        assertEq(reg.rootSequence(), ring + 2);
        assertEq(reg.identityRoot(), 9000);
    }

    function test_aMaxAgeTheFullRingCannotHonourIsRefused() public {
        uint256 ring = reg.ROOT_RING_SIZE();
        for (uint256 i = 0; i < ring; i++) {
            _post(100 + i);
            vm.warp(block.timestamp + 1 hours);
        }
        // The ring now reaches back ~ring hours; a longer window would be
        // silently truncated, so it is refused.
        vm.prank(GOV);
        vm.expectRevert(bytes("maxAge beyond the ring"));
        reg.setMaxRootAge(NOTES, uint32(ring * 1 hours + 1 days));
        vm.prank(GOV);
        reg.setMaxRootAge(NOTES, 3 days);
        assertEq(reg.maxRootAge(NOTES), 3 days);
    }

    // ---- public membership -------------------------------------------------

    struct World {
        uint256 leaf;
        uint256[] sub;
        uint256 subIndex;
        uint256[] agg;
        uint256 root;
    }

    /// @dev A public subtree of depth `depth` holding `leaf` at index 1 beside
    ///      one other leaf, enrolled at aggregator slot 3 beside one other
    ///      subtree, and the aggregator root over them -- built with the same
    ///      Poseidon the registry folds with.
    function _world(uint8 depth) internal returns (World memory w) {
        BN254.G1Point memory M = BN254.mul(BN254.g1(), 0xC0FFEE);
        w.leaf = reg.publicIdentityLeaf(M);
        w.subIndex = 1;
        w.sub = new uint256[](depth);
        uint256 z = 0;
        uint256 cur = _h(0x1111, w.leaf);            // level 0: [0x1111, leaf]
        w.sub[0] = 0x1111;
        for (uint256 d = 1; d < depth; d++) {
            z = d == 1 ? _h(0, 0) : _h(z, z);        // empty sibling subtrees
            w.sub[d] = z;
            cur = _h(cur, z);
        }
        uint256 subRoot = cur;

        // Aggregator (depth 20): slot 2 holds another subtree root, slot 3 ours.
        uint256 other = 0x2222;
        w.agg = new uint256[](20);
        w.agg[0] = other;                            // slot 3 is a right child of [2,3]
        cur = _h(other, subRoot);
        uint256 zero = 0;
        uint256 zd = 0;
        for (uint256 d = 1; d < 20; d++) {
            zd = d == 1 ? _h(zero, zero) : _h(zd, zd);
            // slot 3 >> d: at d == 1 the node index is 1 (a right child).
            if ((uint256(3) >> d) & 1 == 1) {
                uint256 left = _h(0, 0);             // the node over slots [0,1]
                w.agg[d] = left;
                cur = _h(left, cur);
            } else {
                w.agg[d] = zd;
                cur = _h(cur, zd);
            }
        }
        w.root = cur;

        vm.prank(ROOT_AUTH);
        reg.enrollSubtree(REGULATOR_STANDING, 3, depth, true, POSTER, "");
        _post(w.root);
    }

    function test_publicMembershipFoldsThroughTheEnrolledSlot() public {
        World memory w = _world(10);
        assertTrue(reg.verifyPublicMembership(REGULATOR_STANDING, w.leaf, w.sub, w.subIndex, w.agg, w.root));
        assertTrue(reg.acceptsRoot(w.root, INSURER));

        // The same path claimed for a subtree enrolled at another slot fails:
        // the slot, not the caller, fixes the aggregator index.
        bytes32 other = keccak256("regulator:ca-ab:insurer:face:3");
        vm.prank(ROOT_AUTH);
        reg.enrollSubtree(other, 2, 10, true, POSTER, "");
        assertFalse(reg.verifyPublicMembership(other, w.leaf, w.sub, w.subIndex, w.agg, w.root));

        // A tampered sibling, a wrong index, a wrong root: all fail.
        w.sub[4] ^= 1;
        assertFalse(reg.verifyPublicMembership(REGULATOR_STANDING, w.leaf, w.sub, w.subIndex, w.agg, w.root));
        w.sub[4] ^= 1;
        assertFalse(reg.verifyPublicMembership(REGULATOR_STANDING, w.leaf, w.sub, 0, w.agg, w.root));
        assertFalse(reg.verifyPublicMembership(REGULATOR_STANDING, w.leaf, w.sub, w.subIndex, w.agg, w.root ^ 1));
    }

    function test_privateOrEvictedSubtreesAdmitNoPublicPath() public {
        World memory w = _world(10);
        vm.startPrank(ROOT_AUTH);
        reg.evictSubtree(REGULATOR_STANDING);
        assertFalse(reg.verifyPublicMembership(REGULATOR_STANDING, w.leaf, w.sub, w.subIndex, w.agg, w.root),
                    "an evicted subtree proves nothing");
        reg.enrollSubtree(REGULATOR_STANDING, 3, 10, false, POSTER, "");
        vm.stopPrank();
        assertFalse(reg.verifyPublicMembership(REGULATOR_STANDING, w.leaf, w.sub, w.subIndex, w.agg, w.root),
                    "a private subtree's membership is proven in zero knowledge, never by a path");
    }

    /// @notice The specification asks phase 4 to measure a public proof both
    ///         ways: through the aggregator (subtree + 20 levels), and against a
    ///         posted subtree root alone.  The second is what a registry that
    ///         stored public subtree roots would pay.
    function test_gas_publicMembership() public {
        World memory w = _world(10);
        uint256 g = gasleft();
        reg.verifyPublicMembership(REGULATOR_STANDING, w.leaf, w.sub, w.subIndex, w.agg, w.root);
        uint256 viaAggregator = g - gasleft();

        g = gasleft();
        uint256 cur = w.leaf;
        for (uint256 d = 0; d < w.sub.length; d++) {
            cur = (w.subIndex >> d) & 1 == 0 ? _h(cur, w.sub[d]) : _h(w.sub[d], cur);
        }
        uint256 subtreeOnly = g - gasleft();
        emit log_named_uint("public membership, subtree + aggregator (30 levels)", viaAggregator);
        emit log_named_uint("public membership, subtree root alone (10 levels)", subtreeOnly);
        assertGt(viaAggregator, subtreeOnly);
    }
}
