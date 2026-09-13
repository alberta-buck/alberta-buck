// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {Notes} from "../src/Notes.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {BN254} from "../src/BN254.sol";
import {StubMintVerifier} from "../src/StubMintVerifier.sol";
import {StubSpendVerifier} from "../src/StubSpendVerifier.sol";
import {MintBatchA2N1Groth16Verifier} from "../src/MintBatchA2N1Groth16Verifier.sol";
import {MintBatchA2N2Groth16Verifier} from "../src/MintBatchA2N2Groth16Verifier.sol";
import {MintVerifierA2Adapter}        from "../src/MintVerifierA2Adapter.sol";

contract MockBuckTie {
    function transferFrom(address, address, uint256) external pure returns (bool) { return true; }
    function transfer(address, uint256) external pure returns (bool) { return true; }
}

/// @notice Phase 2c: the A2 eIss leaf-tie, end to end against the *real*
///         mint_batch_a2 verifier.  Notes.mint passes each binding's eIss as the
///         A2 circuit's public input, so a Groth16 accept proves the binding's
///         eIss IS the committed leaf's -- closing the floating-/duplicate-
///         binding collusion sub-cases that per-batch count alone could not.
///
///         The `tie` fixture pins leaf 0's eIss to the canonical issuer_reenc
///         binding (test/vectors/identity.json), so a real binding drives the
///         full path.  The `tie_dup` fixture commits a *distinct* second leaf;
///         minting it with a duplicated binding (the count-only attack) now
///         fails the leaf-tie.
///
///         NOTE on scope: the leaf-tie binds each committed leaf to a verified
///         re-encryption of the issuer's registered Identity.  It does NOT force
///         the binding's pk_rec to be the addressed recipient's key (see
///         alberta-buck-notes.org ("The Non-Deniable-Receipt Invariant", A2 recipient-key coupling / note-binding tie) and notes-flow.org (A2 flows)
///         gap"); that residual collusion hole is out of scope here.
contract NotesA2TieTest is Test {
    address constant GOV = address(0xB0);

    IdentityRegistry      reg;
    Notes                 notes;
    MintVerifierA2Adapter a2adapter;
    string                vj;
    address               issuer;     // canonical private A2 issuer (msg.sender)

    function setUp() public {
        vm.chainId(1);                       // issuer_reenc transcript chainid = 1
        vj  = vm.readFile("test/vectors/identity.json");
        reg = new IdentityRegistryHarness(GOV);

        issuer = address(uint160(_u(".issuer_reenc.issuer")));
        vm.etch(issuer, hex"60006000fd");
        reg.bindContract(issuer, _g1(".issuer_reenc.pk_iss"),
                         _ct(".issuer_reenc.E_reg"), false /*private*/, false);

        // Real A2 verifier + adapter (N=1, N=2).
        a2adapter = new MintVerifierA2Adapter(GOV);
        vm.startPrank(GOV);
        a2adapter.registerVerifier(1, address(new MintBatchA2N1Groth16Verifier()));
        a2adapter.registerVerifier(2, address(new MintBatchA2N2Groth16Verifier()));
        vm.stopPrank();

        StubMintVerifier  m = new StubMintVerifier(GOV);
        StubSpendVerifier s = new StubSpendVerifier(GOV);
        MockBuckTie       b = new MockBuckTie();
        notes = new Notes(address(b), address(m), address(s), GOV);
        vm.startPrank(GOV);
        notes.setIdentityRegistry(address(reg));
        notes.setA2MintVerifier(address(a2adapter));
        vm.stopPrank();
    }

    // ---- loaders -----------------------------------------------------------

    function _u(string memory k) internal view returns (uint256) { return vm.parseJsonUint(vj, k); }
    function _g1(string memory k) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point(_u(string.concat(k, ".x")), _u(string.concat(k, ".y")));
    }
    function _ct(string memory k) internal view returns (IdentityRegistry.ElGamalCT memory c) {
        c.R = _g1(string.concat(k, ".R"));
        c.C = _g1(string.concat(k, ".C"));
    }
    function _proof() internal view returns (IdentityRegistry.IssuerReencProof memory p) {
        p.e   = _u(".issuer_reenc.proof.e");
        p.s_r = _u(".issuer_reenc.proof.s_r");
        p.s_b = _u(".issuer_reenc.proof.s_b");
        p.s_s = _u(".issuer_reenc.proof.s_s");
        p.s_g = _u(".issuer_reenc.proof.s_g");
        p.A1  = _g1(".issuer_reenc.proof.A1");
        p.A2  = _g1(".issuer_reenc.proof.A2");
        p.A3  = _g1(".issuer_reenc.proof.A3");
        p.A4  = _g1(".issuer_reenc.proof.A4");
        p.A5  = _g1(".issuer_reenc.proof.A5");
        p.Q   = _g1(".issuer_reenc.proof.Q");
        p.U   = _g1(".issuer_reenc.proof.U");
        p.T   = _g1(".issuer_reenc.proof.T");
    }
    /// @dev The canonical binding (eIss = the pinned leaf-0 ciphertext).
    function _binding() internal view returns (Notes.A2Binding memory a) {
        a = Notes.A2Binding({eIss: _ct(".issuer_reenc.E_iss"), proof: _proof()});
    }

    struct Fx {
        uint256   n;
        uint256   oldRoot;
        uint256   newRoot;
        uint256   nextLeafIndex;
        uint256   totalFace;
        uint256[] cms;
        bytes     proof;
    }
    function _loadFx(string memory path) internal view returns (Fx memory fx) {
        string memory j = vm.readFile(path);
        fx.n             = vm.parseJsonUint(j, ".N");
        fx.oldRoot       = vm.parseJsonUint(j, ".public.oldRoot");
        fx.newRoot       = vm.parseJsonUint(j, ".public.newRoot");
        fx.nextLeafIndex = vm.parseJsonUint(j, ".public.nextLeafIndex");
        fx.totalFace     = vm.parseJsonUint(j, ".public.totalFace");
        fx.cms           = new uint256[](fx.n);
        for (uint256 i = 0; i < fx.n; i++) {
            fx.cms[i] = vm.parseJsonUint(j, string.concat(".public.cm[", vm.toString(i), "]"));
        }
        fx.proof = vm.parseJsonBytes(j, ".proofBytes");
    }
    function _privMode(uint256 n) internal pure returns (uint256[] memory mm) {
        mm = new uint256[](n);
        for (uint256 i = 0; i < n; i++) mm[i] = 2;  // MODE_PRIVATE
    }

    // ---- tests -------------------------------------------------------------

    /// @notice Happy path: the committed leaf's eIss == the binding's eIss, so
    ///         the real A2 verifier accepts and the mint succeeds.
    function test_a2Tie_mints() public {
        Fx memory fx = _loadFx("build/snark/mint_batch_a2_n1/fixtures/tie.json");
        assertEq(fx.oldRoot, notes.EMPTY_ROOT(), "fresh tree");

        Notes.A2Binding[] memory bindings = new Notes.A2Binding[](1);
        bindings[0] = _binding();
        uint256[] memory mode = _privMode(1);

        vm.prank(issuer);
        notes.mint(fx.proof, fx.oldRoot, fx.newRoot, uint32(fx.nextLeafIndex),
                   fx.totalFace, fx.cms, mode, bindings);
        assertEq(notes.nextLeafIndex(), 1, "A2 leaf appended");
        assertEq(notes.noteRoot(),      fx.newRoot);
    }

    /// @notice Collusion regression: a 2-leaf batch committing two DISTINCT eIss
    ///         (leaf 0 pinned to the binding, leaf 1 independent), minted with a
    ///         DUPLICATED binding -- the count-only attack.  The leaf-tie rejects
    ///         it: leaf 1's committed eIss != the duplicated binding's eIss, so
    ///         the A2 verifier's pairing check fails.
    function test_a2Tie_duplicateBinding_reverts() public {
        Fx memory fx = _loadFx("build/snark/mint_batch_a2_n2/fixtures/tie_dup.json");

        Notes.A2Binding[] memory bindings = new Notes.A2Binding[](2);
        bindings[0] = _binding();
        bindings[1] = _binding();        // duplicate -- not leaf 1's committed eIss
        uint256[] memory mode = _privMode(2);

        vm.prank(issuer);
        vm.expectRevert(bytes("Notes: bad mint proof"));
        notes.mint(fx.proof, fx.oldRoot, fx.newRoot, uint32(fx.nextLeafIndex),
                   fx.totalFace, fx.cms, mode, bindings);
    }

    /// @notice A binding whose eIss does not match the committed leaf (a
    ///         floating binding) fails the leaf-tie even at N=1.
    function test_a2Tie_floatingBinding_reverts() public {
        Fx memory fx = _loadFx("build/snark/mint_batch_a2_n1/fixtures/tie.json");

        Notes.A2Binding[] memory bindings = new Notes.A2Binding[](1);
        bindings[0] = _binding();
        bindings[0].eIss.C.X ^= 1;       // perturb eIss -> no longer the committed leaf's
        uint256[] memory mode = _privMode(1);

        vm.prank(issuer);
        vm.expectRevert(bytes("Notes: bad mint proof"));
        notes.mint(fx.proof, fx.oldRoot, fx.newRoot, uint32(fx.nextLeafIndex),
                   fx.totalFace, fx.cms, mode, bindings);
    }

}
