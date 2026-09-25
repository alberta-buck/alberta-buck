// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {BN254} from "../src/BN254.sol";
import {IPoseidonT3} from "../src/IPoseidonT3.sol";
import {PoseidonT3Bytecode} from "../src/PoseidonT3Bytecode.sol";
import {PoseidonT4Bytecode} from "../src/PoseidonT4Bytecode.sol";

/// @notice The insurer gate of doc/review/accumulator-spec.org section 12,
///         end to end: a regulator's predicate subtrees enrolled in the
///         accumulator, an insurer proving its envelope once, and issuance
///         checked against it with the reasons of the Python reference
///         (alberta_buck/registry/regulator.py check_issuance).
contract InsurerGateTest is Test {
    address constant GOV   = address(0x6011);
    address constant AGG   = address(0xA99);
    address constant CLIENT = address(0xC11E);
    string  constant NS    = "regulator:ca-ab";

    IdentityRegistryHarness reg;
    BuckCredit              credit;
    IPoseidonT3             p3;

    address insurer = address(0x1E5);
    uint256 constant SK = 0x5EC12E7;           // the insurer's account key
    uint256 constant M_SCALAR = 0x1D;          // its Identity scalar
    uint256 constant R_E = 0x77;               // its credential's randomness
    BN254.G1Point M;

    // The predicates this world's regulator attests for the insurer.
    string[] predicates;
    uint8 constant SUB_DEPTH = 2;

    function setUp() public {
        vm.warp(1_800_000_000);
        reg = new IdentityRegistryHarness(GOV);
        p3 = IPoseidonT3(PoseidonT3Bytecode.deploy());
        vm.startPrank(GOV);
        reg.setIdentityPoseidon(address(p3));
        reg.setIdentityPoseidonT4(PoseidonT4Bytecode.deploy());
        reg.setRootAuthority(GOV);
        reg.setAggregator(AGG);
        vm.stopPrank();

        credit = new BuckCredit();
        credit.configureInsurerGate(address(reg), NS, 90 days);

        // The insurer: a registered account whose credential encrypts M.
        M = BN254.mul(BN254.g1(), M_SCALAR);
        BN254.G1Point memory pk = BN254.mul(BN254.g1(), SK);
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT(
            BN254.mul(BN254.g1(), R_E), BN254.add(M, BN254.mul(pk, R_E)));
        vm.etch(insurer, hex"60006000fd");
        reg.bindContract(insurer, pk, E, true, false);

        vm.prank(CLIENT);
        credit.setCreditIssuer(insurer, true);

        predicates.push("insurer");
        predicates.push("insurer:face:5");
        predicates.push("insurer:dep:0");
        predicates.push("insurer:dep:1");
        predicates.push("insurer:depRate:1000");
        predicates.push("insurer:premium:500");
        predicates.push("insurer:general");
        predicates.push("scope:asset:bicycle");
    }

    // ---- the accumulator world ------------------------------------------

    function _h(uint256 l, uint256 r) internal view returns (uint256) {
        return p3.poseidon([l, r]);
    }

    function _zeros(uint256 n) internal view returns (uint256[] memory z) {
        z = new uint256[](n + 1);
        for (uint256 d = 1; d <= n; d++) z[d] = _h(z[d - 1], z[d - 1]);
    }

    /// @dev Each predicate subtree holds the insurer's leaf at index 0 beside
    ///      one other member; subtree k sits at aggregator slot k.  Returns
    ///      the root and one path per predicate, in the gate's claim order.
    function _enrollAndPost(string[] memory preds)
        internal returns (uint256 root, BuckCredit.Path[] memory paths)
    {
        uint256 leaf = reg.publicIdentityLeaf(M);
        uint256[] memory z = _zeros(20);
        uint256 n = preds.length;
        uint256[] memory subRoots = new uint256[](8);
        paths = new BuckCredit.Path[](n);
        for (uint256 k = 0; k < n; k++) {
            uint256 other = 0xA000 + k;
            paths[k].sub = new uint256[](SUB_DEPTH);
            paths[k].sub[0] = other;
            paths[k].sub[1] = z[1];
            paths[k].subIndex = 0;
            subRoots[k] = _h(_h(leaf, other), z[1]);
            vm.prank(GOV);
            reg.enrollSubtree(keccak256(bytes(string.concat(NS, ":", preds[k]))),
                              uint32(k), SUB_DEPTH, true, GOV, "");
        }
        // Aggregator: slots 0..7 hold the subtree roots (empty ones are 0).
        uint256[] memory l1 = new uint256[](4);
        for (uint256 i = 0; i < 4; i++) l1[i] = _h(subRoots[2 * i], subRoots[2 * i + 1]);
        uint256[] memory l2 = new uint256[](2);
        l2[0] = _h(l1[0], l1[1]);
        l2[1] = _h(l1[2], l1[3]);
        root = _h(l2[0], l2[1]);
        for (uint256 d = 3; d < 20; d++) root = _h(root, z[d]);
        for (uint256 k = 0; k < n; k++) {
            paths[k].agg = new uint256[](20);
            paths[k].agg[0] = subRoots[k ^ 1];
            paths[k].agg[1] = l1[(k >> 1) ^ 1];
            paths[k].agg[2] = l2[(k >> 2) ^ 1];
            for (uint256 d = 3; d < 20; d++) paths[k].agg[d] = z[d];
        }
        vm.prank(AGG);
        reg.postIdentityRoot(root, keccak256("leaf list"));
    }

    function _opening(address account, BN254.G1Point memory claimedM)
        internal view returns (BuckCredit.IdentityOpening memory op)
    {
        BN254.G1Point memory pk = BN254.mul(BN254.g1(), SK);
        IdentityRegistry.ElGamalCT memory E = reg.ciphertextOf(account);
        uint256 t = 0x7777;
        op.T1 = BN254.mul(BN254.g1(), t);
        op.T2 = BN254.mul(E.R, t);
        BN254.G1Point[] memory pts = new BN254.G1Point[](6);
        pts[0] = E.R;
        pts[1] = E.C;
        pts[2] = pk;
        pts[3] = claimedM;
        pts[4] = op.T1;
        pts[5] = op.T2;
        uint256[] memory scl = new uint256[](4);
        scl[0] = uint256(uint160(account));
        scl[1] = block.chainid;
        scl[2] = uint256(uint160(address(reg)));
        scl[3] = uint256(keccak256("AlbertaBuck/FiatShamir/IdentityRegistry/IdentityOpening/v2"));
        op.e = BN254.fsChallenge(pts, scl);
        op.s = addmod(t, mulmod(op.e, SK, BN254.R), BN254.R);
    }

    function _claim(bool general, string[] memory scopes)
        internal pure returns (BuckCredit.EnvelopeClaim memory c)
    {
        c.faceBand = 5;
        c.depTypes = 0x3;                        // NONE and LINEAR
        c.maxDepRate = 1000;
        c.maxPremiumRate = 500;
        c.general = general;
        c.scopes = scopes;
    }

    function _bicycle() internal pure returns (string[] memory s) {
        s = new string[](1);
        s[0] = "asset:bicycle";
    }

    function _attest() internal {
        (uint256 root, BuckCredit.Path[] memory paths) = _enrollAndPost(predicates);
        BuckCredit.IdentityOpening memory op = _opening(insurer, M);  // before the prank
        vm.prank(insurer);
        credit.attestInsurer(M, op, _claim(true, _bicycle()), root, paths);
    }

    function _create(uint256 face, BuckCredit.DepreciationType dep, uint32 depRate, uint32 prem)
        internal returns (uint256)
    {
        vm.prank(insurer);
        return credit.createCredit(CLIENT, 0, face, 0, dep, depRate, uint48(block.timestamp), prem);
    }

    // ---- attestation -----------------------------------------------------

    function test_attestationCachesTheEnvelope() public {
        _attest();
        (uint48 expiresAt, uint8 band, uint8 deps, uint32 maxDep, uint32 maxPrem, uint32 epoch) =
            credit.envelopeOf(insurer);
        assertEq(expiresAt, block.timestamp + 90 days);
        assertEq(band, 5);
        assertEq(deps, 3);
        assertEq(maxDep, 1000);
        assertEq(maxPrem, 500);
        assertEq(epoch, 1);
        assertTrue(credit.scopeAttested(insurer, bytes32(0)));
        assertTrue(credit.scopeAttested(insurer,
                   keccak256(bytes(string.concat(NS, ":scope:asset:bicycle")))));
    }

    function test_anotherAccountCannotClaimTheInsurersIdentity() public {
        (uint256 root, BuckCredit.Path[] memory paths) = _enrollAndPost(predicates);
        address mallory = address(0xBAD);
        vm.etch(mallory, hex"60006000fd");
        reg.bindContract(mallory, BN254.mul(BN254.g1(), SK),
                         IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1()), true, false);
        // Mallory holds the same key but a credential that is not M's.
        BuckCredit.IdentityOpening memory op = _opening(mallory, M);
        vm.prank(mallory);
        vm.expectRevert(bytes("BuckCredit: identity opening fails"));
        credit.attestInsurer(M, op, _claim(true, _bicycle()), root, paths);
    }

    function test_aClaimTheRegulatorDidNotAttestIsRefused() public {
        (uint256 root, BuckCredit.Path[] memory paths) = _enrollAndPost(predicates);
        BuckCredit.EnvelopeClaim memory c = _claim(true, _bicycle());
        c.faceBand = 6;                          // a band never attested
        BuckCredit.IdentityOpening memory op = _opening(insurer, M);
        vm.prank(insurer);
        vm.expectRevert(bytes("BuckCredit: not attested: insurer:face:6"));
        credit.attestInsurer(M, op, c, root, paths);
    }

    function test_aStaleRootIsRefused() public {
        (uint256 root, BuckCredit.Path[] memory paths) = _enrollAndPost(predicates);
        vm.warp(block.timestamp + 1 days + 1);   // the insurer consumer takes one day
        BuckCredit.IdentityOpening memory op = _opening(insurer, M);
        vm.prank(insurer);
        vm.expectRevert(bytes("BuckCredit: root not accepted"));
        credit.attestInsurer(M, op, _claim(true, _bicycle()), root, paths);
    }

    function test_reattestationWithdrawsScopesItDoesNotRepeat() public {
        _attest();
        bytes32 bicycle = keccak256(bytes(string.concat(NS, ":scope:asset:bicycle")));
        (uint256 root, BuckCredit.Path[] memory paths) = _repost();
        // Re-attest with the general scope only: drop the bicycle path.
        BuckCredit.Path[] memory fewer = new BuckCredit.Path[](paths.length - 1);
        for (uint256 i = 0; i < fewer.length; i++) fewer[i] = paths[i];
        BuckCredit.IdentityOpening memory op = _opening(insurer, M);
        vm.prank(insurer);
        credit.attestInsurer(M, op, _claim(true, new string[](0)), root, fewer);
        assertFalse(credit.scopeAttested(insurer, bicycle), "a scope not repeated is withdrawn");
        assertTrue(credit.scopeAttested(insurer, bytes32(0)));
    }

    /// @dev The same world, re-posted an hour later (a fresh root record).
    function _repost() internal returns (uint256 root, BuckCredit.Path[] memory paths) {
        vm.warp(block.timestamp + 1 hours);
        root = reg.identityRoot();
        vm.prank(AGG);
        reg.postIdentityRoot(root, keccak256("leaf list, again"));
        // Rebuild the paths without re-enrolling.
        (, paths) = _paths(predicates);
    }

    function _paths(string[] memory preds)
        internal view returns (uint256 root, BuckCredit.Path[] memory paths)
    {
        uint256 leaf = reg.publicIdentityLeaf(M);
        uint256[] memory z = _zeros(20);
        uint256 n = preds.length;
        uint256[] memory subRoots = new uint256[](8);
        paths = new BuckCredit.Path[](n);
        for (uint256 k = 0; k < n; k++) {
            paths[k].sub = new uint256[](SUB_DEPTH);
            paths[k].sub[0] = 0xA000 + k;
            paths[k].sub[1] = z[1];
            subRoots[k] = _h(_h(leaf, 0xA000 + k), z[1]);
        }
        uint256[] memory l1 = new uint256[](4);
        for (uint256 i = 0; i < 4; i++) l1[i] = _h(subRoots[2 * i], subRoots[2 * i + 1]);
        uint256[] memory l2 = new uint256[](2);
        l2[0] = _h(l1[0], l1[1]);
        l2[1] = _h(l1[2], l1[3]);
        root = _h(l2[0], l2[1]);
        for (uint256 d = 3; d < 20; d++) root = _h(root, z[d]);
        for (uint256 k = 0; k < n; k++) {
            paths[k].agg = new uint256[](20);
            paths[k].agg[0] = subRoots[k ^ 1];
            paths[k].agg[1] = l1[(k >> 1) ^ 1];
            paths[k].agg[2] = l2[(k >> 2) ^ 1];
            for (uint256 d = 3; d < 20; d++) paths[k].agg[d] = z[d];
        }
    }

    // ---- issuance --------------------------------------------------------

    function test_withinTheEnvelopeACreditIsWritten() public {
        _attest();
        uint256 id = _create(99_999e6, BuckCredit.DepreciationType.LINEAR, 1000, 500);
        assertEq(credit.ownerOf(id), CLIENT);
        bytes32 bicycle = keccak256(bytes(string.concat(NS, ":scope:asset:bicycle")));
        vm.prank(insurer);
        credit.createCredit(CLIENT, 0, 80e6, 0, BuckCredit.DepreciationType.LINEAR, 500,
                            uint48(block.timestamp), 300, bicycle);
    }

    function test_everyRefusalCarriesTheReferenceReason() public {
        vm.expectRevert(bytes("insurer not in good standing"));
        _create(1e6, BuckCredit.DepreciationType.NONE, 0, 0);

        _attest();
        vm.expectRevert(bytes("face above attested band"));
        _create(100_000e6, BuckCredit.DepreciationType.NONE, 0, 0);   // band 6
        vm.expectRevert(bytes("depreciation model not attested"));
        _create(1e6, BuckCredit.DepreciationType.DECLINING_BALANCE, 0, 0);
        vm.expectRevert(bytes("depreciation rate above attested maximum"));
        _create(1e6, BuckCredit.DepreciationType.LINEAR, 1001, 0);
        vm.expectRevert(bytes("premium rate above attested maximum"));
        _create(1e6, BuckCredit.DepreciationType.LINEAR, 0, 501);

        bytes32 car = keccak256(bytes(string.concat(NS, ":scope:asset:car")));
        vm.prank(insurer);
        vm.expectRevert(bytes("scope not attested"));
        credit.createCredit(CLIENT, 0, 1e6, 0, BuckCredit.DepreciationType.NONE, 0,
                            uint48(block.timestamp), 0, car);

        vm.warp(block.timestamp + 90 days + 1);
        vm.expectRevert(bytes("attestation expired"));
        _create(1e6, BuckCredit.DepreciationType.NONE, 0, 0);
    }

    function test_aReappraisalStaysInsideTheEnvelope() public {
        _attest();
        uint256 id = _create(1_000e6, BuckCredit.DepreciationType.LINEAR, 1000, 500);
        vm.prank(insurer);
        vm.expectRevert(bytes("face above attested band"));
        credit.updateCredit(id, 1_000_000e6, 0, BuckCredit.DepreciationType.LINEAR, 1000,
                            uint48(block.timestamp), 500);
        vm.prank(insurer);
        credit.updateCredit(id, 50_000e6, 0, BuckCredit.DepreciationType.NONE, 0,
                            uint48(block.timestamp), 100);
    }

    function test_theBandLadder() public view {
        assertEq(credit.bandForFace(0), 1);
        assertEq(credit.bandForFace(10e6 - 1), 1);
        assertEq(credit.bandForFace(10e6), 2);
        assertEq(credit.bandForFace(100_000_000e6 - 1), 8);
        assertEq(credit.bandForFace(100_000_000e6), 9);
    }

    // ---- configuration ---------------------------------------------------

    function test_theGateIsConfiguredOnceByTheDeployer() public {
        vm.expectRevert(bytes("BuckCredit: gate configured"));
        credit.configureInsurerGate(address(reg), NS, 30 days);
        BuckCredit fresh = new BuckCredit();
        vm.prank(address(0xBEEF));
        vm.expectRevert(bytes("BuckCredit: not deployer"));
        fresh.configureInsurerGate(address(reg), NS, 30 days);
    }

    function test_anUnconfiguredGateIssuesUngated() public {
        BuckCredit fresh = new BuckCredit();
        vm.prank(CLIENT);
        fresh.setCreditIssuer(insurer, true);
        vm.prank(insurer);
        fresh.createCredit(CLIENT, 0, 1e15, 0, BuckCredit.DepreciationType.DECLINING_BALANCE,
                           9000, uint48(block.timestamp), 9000);
    }
}
