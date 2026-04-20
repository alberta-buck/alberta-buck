// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {BN254}                from "../src/BN254.sol";
import {IdentityRegistry}     from "../src/IdentityRegistry.sol";
import {Buck}                 from "../src/Buck.sol";
import {BuckCredit}           from "../src/BuckCredit.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";
import {Notes}                from "../src/Notes.sol";
import {IMintVerifier}        from "../src/IMintVerifier.sol";
import {StubMintVerifier}     from "../src/StubMintVerifier.sol";

/// @notice IMintVerifier that always rejects -- exercises the negative path
///         without depending on StubMintVerifier's enabled toggle.
contract RejectingMintVerifier is IMintVerifier {
    function verifyMint(bytes calldata, uint256, uint256[] calldata, address)
        external pure returns (bool) { return false; }
}

contract NotesTest is Test {

    Buck                  internal buck;
    BuckCredit            internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;
    Notes                 internal notes;
    StubMintVerifier      internal stub;

    address internal constant GOV     = address(0xA0);
    address internal constant ISSUER  = address(0x1551E1);
    address internal constant POOL    = address(0xBA51C);  // Buck.sol's "insurance pool"

    address internal alice;
    address internal bob;

    string  internal vj;

    // Three precomputed commitments from the Python wallet:
    // alberta_buck.wallet.notes.note_commitment(NoteOpening(...))
    uint256 internal constant CM1 = 0x2f32199a12908d70cb27b94f766fccde66484f15ec37b1143f0c9958cdd3379d;
    uint256 internal constant CM2 = 0x067dc83e554e6adbf068d54a60a711b426be3a79cb43907396eee3dd1cd0b7ab;
    uint256 internal constant CM3 = 0x114e67cd78234325b9227116d9abc66397e28860b3287259aad1b60e7ac11346;

    bytes   internal constant DUMMY_PROOF = hex"deadbeef";

    // ---- harness setup -----------------------------------------------------

    function setUp() public {
        vm.chainId(1);
        vj = vm.readFile("test/vectors/identity.json");

        // Identity layer + Alice/Bob registered (mirrors Buck.t.sol).
        reg = new IdentityRegistry(GOV);
        _trustIssuer();
        alice = address(uint160(_u(".alice.registrant")));
        bob   = address(uint160(_u(".bob.registrant")));
        _registerAlice();
        _registerBob();

        // Buck stack.
        credit = new BuckCredit();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);

        // Notes stack.
        stub  = new StubMintVerifier(GOV);
        notes = new Notes(address(buck), address(stub), GOV);

        // The pool is a "regular account" -- it has no PS credential, so we
        // mark it system-public to satisfy Buck's identity-bound transfer.
        vm.prank(GOV);
        reg.setSystemPublic(address(notes), true);

        // Give Alice a credit limit and BUCK balance so she can mint notes.
        _grantCredit(alice, 1000e18);
        vm.prank(alice);
        buck.mint(500e18);
    }

    // ---- JSON helpers (lifted from Buck.t.sol) -----------------------------

    function _u(string memory key) internal view returns (uint256) {
        return vm.parseJsonUint(vj, key);
    }

    function _g1(string memory key) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point(_u(string.concat(key, ".x")), _u(string.concat(key, ".y")));
    }

    function _ps(string memory who) internal view returns (IdentityRegistry.PSSig memory s) {
        s.sigma_1 = _g1(string.concat(".", who, ".ps_sig_rerand.sigma_1"));
        s.sigma_2 = _g1(string.concat(".", who, ".ps_sig_rerand.sigma_2"));
    }

    function _ct(string memory key) internal view returns (IdentityRegistry.ElGamalCT memory c) {
        c.R = _g1(string.concat(key, ".R"));
        c.C = _g1(string.concat(key, ".C"));
    }

    function _regProof(string memory who) internal view returns (IdentityRegistry.RegistrationProof memory p) {
        string memory base = string.concat(".", who, ".registration_proof");
        p.e    = _u(string.concat(base, ".e"));
        p.s_m  = _u(string.concat(base, ".s_m"));
        p.s_r  = _u(string.concat(base, ".s_r"));
        p.A_ps = _g1(string.concat(base, ".A_ps"));
        p.T_C  = _g1(string.concat(base, ".T_C"));
        p.T_R  = _g1(string.concat(base, ".T_R"));
    }

    function _trustIssuer() internal {
        IdentityRegistry.PSPubKey memory ipk;
        ipk.X.X[0] = _u(".issuer.pk_X.x[0]");
        ipk.X.X[1] = _u(".issuer.pk_X.x[1]");
        ipk.X.Y[0] = _u(".issuer.pk_X.y[0]");
        ipk.X.Y[1] = _u(".issuer.pk_X.y[1]");
        ipk.Y.X[0] = _u(".issuer.pk_Y.x[0]");
        ipk.Y.X[1] = _u(".issuer.pk_Y.x[1]");
        ipk.Y.Y[0] = _u(".issuer.pk_Y.y[0]");
        ipk.Y.Y[1] = _u(".issuer.pk_Y.y[1]");
        vm.prank(GOV);
        reg.trustIssuer(ISSUER, ipk);
    }

    function _registerAlice() internal {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        vm.prank(alice);
        reg.register(ISSUER, pk, E, _ps("alice"), _regProof("alice"));
    }

    function _registerBob() internal {
        BN254.G1Point memory pk = _g1(".bob.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".bob.ciphertext");
        vm.prank(bob);
        reg.register(ISSUER, pk, E, _ps("bob"), _regProof("bob"));
    }

    function _grantCredit(address client, uint256 faceValue) internal {
        uint256 tokenId = credit.createCredit(
            client, 0, faceValue, faceValue,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(client);
        credit.activate(tokenId, faceValue);
    }

    /// @dev Approve Notes from Alice to spend `amount` BUCK.  Notes is public
    ///      (set in setUp), so Buck's identity-bound approve takes the
    ///      public-spender path and the CP proof / ciphertext are unused.
    function _approveNotes(address from, uint256 amount) internal {
        IdentityRegistry.ElGamalCT memory junk;
        IdentityRegistry.CPProof memory junkPi;
        vm.prank(from);
        buck.approve(address(notes), amount, junk, junkPi);
    }

    function _cms(uint256 a, uint256 b) internal pure returns (uint256[] memory cms) {
        cms = new uint256[](2);
        cms[0] = a;
        cms[1] = b;
    }

    // ---- constructor -------------------------------------------------------

    function test_constructor_setsImmutables() public view {
        assertEq(address(notes.buck()),          address(buck));
        assertEq(address(notes.mintVerifier()),  address(stub));
        assertEq(notes.governance(),             GOV);
        assertEq(notes.commitmentCount(),        0);
        assertEq(notes.noteFaceSum(),            0);
    }

    function test_constructor_rejectsZero() public {
        vm.expectRevert(bytes("buck=0"));
        new Notes(address(0), address(stub), GOV);
        vm.expectRevert(bytes("verifier=0"));
        new Notes(address(buck), address(0), GOV);
        vm.expectRevert(bytes("governance=0"));
        new Notes(address(buck), address(stub), address(0));
    }

    // ---- governance --------------------------------------------------------

    function test_transferGovernance_onlyGovernance() public {
        vm.prank(alice);
        vm.expectRevert(bytes("not governance"));
        notes.transferGovernance(alice);

        vm.prank(GOV);
        notes.transferGovernance(alice);
        assertEq(notes.governance(), alice);
    }

    function test_setMintVerifier_onlyGovernance() public {
        RejectingMintVerifier rej = new RejectingMintVerifier();
        vm.prank(alice);
        vm.expectRevert(bytes("not governance"));
        notes.setMintVerifier(address(rej));

        vm.prank(GOV);
        notes.setMintVerifier(address(rej));
        assertEq(address(notes.mintVerifier()), address(rej));
    }

    // ---- mint --------------------------------------------------------------

    function test_mint_happyPath() public {
        uint256 face = 200e18;
        _approveNotes(alice, face);
        uint256[] memory cms = _cms(CM1, CM2);
        uint256 aliceBefore  = buck.balanceOf(alice);
        uint256 poolBefore   = buck.balanceOf(address(notes));

        vm.prank(alice);
        notes.mint(DUMMY_PROOF, cms, face);

        assertEq(buck.balanceOf(alice),         aliceBefore - face);
        assertEq(buck.balanceOf(address(notes)), poolBefore + face);
        assertEq(notes.noteFaceSum(),           face);
        assertEq(notes.commitmentCount(),       2);
        assertEq(notes.commitments(0),          CM1);
        assertEq(notes.commitments(1),          CM2);
        assertTrue(notes.commitmentExists(CM1));
        assertTrue(notes.commitmentExists(CM2));
    }

    function test_mint_emitsAppendedAndMintedEvents() public {
        _approveNotes(alice, 200e18);
        uint256[] memory cms = _cms(CM1, CM2);

        vm.expectEmit(true, true, false, false, address(notes));
        emit Notes.Appended(CM1, 0);
        vm.expectEmit(true, true, false, false, address(notes));
        emit Notes.Appended(CM2, 1);
        vm.expectEmit(true, false, false, true, address(notes));
        emit Notes.Minted(alice, 200e18, 0, 2);

        vm.prank(alice);
        notes.mint(DUMMY_PROOF, cms, 200e18);
    }

    function test_mint_secondBatchExtendsLeafIndex() public {
        _approveNotes(alice, 300e18);
        uint256[] memory first = _cms(CM1, CM2);
        vm.prank(alice);
        notes.mint(DUMMY_PROOF, first, 200e18);

        uint256[] memory second = new uint256[](1);
        second[0] = CM3;
        vm.prank(alice);
        notes.mint(DUMMY_PROOF, second, 100e18);

        assertEq(notes.commitmentCount(), 3);
        assertEq(notes.commitments(2),    CM3);
        assertEq(notes.noteFaceSum(),     300e18);
    }

    function test_mint_rejectsEmptyBatch() public {
        _approveNotes(alice, 0);
        uint256[] memory empty;
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: empty mint"));
        notes.mint(DUMMY_PROOF, empty, 0);
    }

    function test_mint_rejectsZeroCommitment() public {
        _approveNotes(alice, 100e18);
        uint256[] memory bad = new uint256[](1);
        bad[0] = 0;
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: zero commitment"));
        notes.mint(DUMMY_PROOF, bad, 100e18);
    }

    function test_mint_rejectsDuplicateCommitmentInBatch() public {
        _approveNotes(alice, 200e18);
        uint256[] memory dup = _cms(CM1, CM1);
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: duplicate commitment"));
        notes.mint(DUMMY_PROOF, dup, 200e18);
    }

    function test_mint_rejectsDuplicateAcrossBatches() public {
        _approveNotes(alice, 200e18);
        uint256[] memory first = new uint256[](1);
        first[0] = CM1;
        vm.prank(alice);
        notes.mint(DUMMY_PROOF, first, 100e18);

        uint256[] memory second = new uint256[](1);
        second[0] = CM1;
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: duplicate commitment"));
        notes.mint(DUMMY_PROOF, second, 100e18);
    }

    function test_mint_rejectedByVerifier() public {
        // Disable the stub -> verifyMint() returns false.
        vm.prank(GOV);
        stub.setEnabled(false);

        _approveNotes(alice, 100e18);
        uint256[] memory cms = new uint256[](1);
        cms[0] = CM1;
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: bad mint proof"));
        notes.mint(DUMMY_PROOF, cms, 100e18);
    }

    function test_mint_rejectedByExternalRejectingVerifier() public {
        RejectingMintVerifier rej = new RejectingMintVerifier();
        vm.prank(GOV);
        notes.setMintVerifier(address(rej));

        _approveNotes(alice, 100e18);
        uint256[] memory cms = new uint256[](1);
        cms[0] = CM1;
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: bad mint proof"));
        notes.mint(DUMMY_PROOF, cms, 100e18);
    }

    function test_mint_revertsOnMissingApproval() public {
        // No allowance set.
        uint256[] memory cms = new uint256[](1);
        cms[0] = CM1;
        vm.prank(alice);
        vm.expectRevert(); // OZ ERC20InsufficientAllowance
        notes.mint(DUMMY_PROOF, cms, 100e18);
    }

    function test_mint_revertsWhenIssuerLacksBalance() public {
        // Bob is verified but has zero BUCK balance and no credit.
        _approveNotes(bob, 100e18);  // approval works without balance
        uint256[] memory cms = new uint256[](1);
        cms[0] = CM1;
        vm.prank(bob);
        vm.expectRevert(); // OZ ERC20InsufficientBalance
        notes.mint(DUMMY_PROOF, cms, 100e18);
    }

    function test_mint_failedTransferLeavesNoStateMutation() public {
        // Set up an over-spend: Alice approves 50, tries to mint 100 face value.
        _approveNotes(alice, 50e18);
        uint256[] memory cms = _cms(CM1, CM2);
        vm.prank(alice);
        vm.expectRevert();
        notes.mint(DUMMY_PROOF, cms, 100e18);

        // No commitments leaked.
        assertEq(notes.commitmentCount(),  0);
        assertFalse(notes.commitmentExists(CM1));
        assertFalse(notes.commitmentExists(CM2));
        assertEq(notes.noteFaceSum(),      0);
    }
}
