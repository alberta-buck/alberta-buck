// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {BN254}                from "../src/BN254.sol";
import {IdentityRegistry}     from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {Buck}                 from "../src/Buck.sol";
import {BuckCredit}           from "../src/BuckCredit.sol";
import {BuckCreditHarness}           from "./harness/BuckCreditHarness.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";
import {Notes}                from "../src/Notes.sol";
import {IMintVerifier}        from "../src/IMintVerifier.sol";
import {StubMintVerifier}     from "../src/StubMintVerifier.sol";
import {StubSpendVerifier}    from "../src/StubSpendVerifier.sol";
import {GatedMint}            from "./helpers/GatedMint.sol";

/// @notice IMintVerifier that always rejects -- exercises the negative path
///         without depending on StubMintVerifier's enabled toggle.
contract RejectingMintVerifier is IMintVerifier {
    function verifyMint(
        bytes calldata, uint256[] calldata, uint256, uint256, uint256, uint256, uint256[] calldata
    ) external pure returns (bool) { return false; }
}

/// @notice IMintVerifier whose `oldRoot` and `nextLeafIndex` echoing is
///         transparent, but otherwise accepts everything.  Lets the Notes
///         tests assert behaviour driven by the contract's stale-state
///         guards rather than verifier acceptance.
contract NotesTest is Test {

    Buck                  internal buck;
    BuckCreditHarness            internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;
    Notes                 internal notes;
    StubMintVerifier      internal stub;
    StubSpendVerifier     internal spendStub;

    address internal constant GOV     = address(0xA0);
    address internal constant ISSUER  = address(0x1551E1);
    address internal constant POOL    = address(0xBA51C);  // Buck.sol's "insurance pool"

    address internal alice;
    address internal bob;

    // Gated-only mint: alice and bob are bound PUBLIC issuers with known keys so
    // the seed/bookkeeping mints route through the public (Schnorr) path.
    uint256 internal constant SK_A = 0x1111111111111111111111111111111111111111111111111111111111111111;
    uint256 internal constant K_A  = 0x2222222222222222222222222222222222222222222222222222222222222222;
    uint256 internal constant SK_B = 0x3333333333333333333333333333333333333333333333333333333333333333;
    uint256 internal constant K_B  = 0x4444444444444444444444444444444444444444444444444444444444444444;
    BN254.G1Point internal pkA; BN254.G1Point internal RA;
    BN254.G1Point internal pkB; BN254.G1Point internal RB;

    string  internal vj;

    // Three placeholder commitments (field-bounded constants).  Phase 7-bis
    // does not validate them on-chain beyond field-membership; the SNARK is
    // the binding party.  These let us exercise mint plumbing with the stub.
    uint256 internal constant CM1 = 0x2f32199a12908d70cb27b94f766fccde66484f15ec37b1143f0c9958cdd3379d;
    uint256 internal constant CM2 = 0x067dc83e554e6adbf068d54a60a711b426be3a79cb43907396eee3dd1cd0b7ab;
    uint256 internal constant CM3 = 0x114e67cd78234325b9227116d9abc66397e28860b3287259aad1b60e7ac11346;

    bytes   internal constant DUMMY_PROOF = hex"deadbeef";

    // Derived in setUp so we can reach the empty-tree root the constructor
    // writes into roots[0].
    uint256 internal EMPTY_ROOT_;

    // ---- harness setup -----------------------------------------------------

    function setUp() public {
        vm.chainId(1);
        vj = vm.readFile("test/vectors/identity.json");

        // Identity layer + Alice/Bob registered (mirrors Buck.t.sol).
        reg = new IdentityRegistryHarness(GOV);
        _trustIssuer();
        alice = address(uint160(_u(".alice.registrant")));
        bob   = address(uint160(_u(".bob.registrant")));
        // Gated-only mint surface: bind alice & bob as PUBLIC issuers with known
        // keys (SK_A/SK_B) so their mints carry a valid issuer Schnorr.
        vm.etch(alice, hex"60006000fd");
        reg.bindContract(alice, BN254.mul(BN254.g1(), SK_A),
                         IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1()), true, false);
        vm.etch(bob, hex"60006000fd");
        reg.bindContract(bob, BN254.mul(BN254.g1(), SK_B),
                         IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1()), true, false);
        // Precompute pk = sk*G and R = k*G so the call-time Schnorr does no ecMul
        // (safe inside mint arg lists after vm.prank / vm.expectRevert).
        pkA = BN254.mul(BN254.g1(), SK_A);  RA = BN254.mul(BN254.g1(), K_A);
        pkB = BN254.mul(BN254.g1(), SK_B);  RB = BN254.mul(BN254.g1(), K_B);

        // Buck stack.
        credit = new BuckCreditHarness();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));

        // Notes stack (no PoseidonT3 dep in Phase 7-bis).
        stub      = new StubMintVerifier(GOV);
        spendStub = new StubSpendVerifier(GOV);
        notes     = new Notes(
            address(buck), address(stub), address(spendStub), GOV
        );
        vm.prank(GOV);
        notes.setIdentityRegistry(address(reg));
        EMPTY_ROOT_ = notes.EMPTY_ROOT();

        // Notes is a BUCK-aware contract operated by GOV; bind it as a
        // Public-Identity contract so identity-bound transfers fall back to
        // the deterministic _identityHash receipt for the EOA <-> Notes
        // counterparty pairs (no off-chain CP material exists for Notes).
        reg.bindContract(
            address(notes),
            BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}),
            true, // isPublicIdentity
            true  // isCarrying
        );

        // Mutual decryptability: private EOAs must CP-approve the public
        // Notes contract so the operator can decrypt identities from receipts.
        {
            bytes32 _fragSlot = keccak256(
                abi.encode(address(notes), keccak256(abi.encode(alice, uint256(5))))
            );
            vm.store(address(buck), _fragSlot, bytes32(uint256(1)));
            // Bob also needs a fragment if any test receives BUCK from Notes.
            _fragSlot = keccak256(
                abi.encode(address(notes), keccak256(abi.encode(bob, uint256(5))))
            );
            vm.store(address(buck), _fragSlot, bytes32(uint256(1)));
        }

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
        p.s_sk = _u(string.concat(base, ".s_sk"));
        p.A_ps = _g1(string.concat(base, ".A_ps"));
        p.T_C  = _g1(string.concat(base, ".T_C"));
        p.T_R  = _g1(string.concat(base, ".T_R"));
        p.T_key = _g1(string.concat(base, ".T_key"));
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
        credit.forceActivate(tokenId, faceValue);
    }

    /// @dev Approve Notes from `from` to spend `amount` BUCK.  Writes the
    ///      ERC-20 allowance slot directly (slot 2 in the packed-state Buck
    ///      layout: slot 0 is _state, slot 1 is _totalSupply, slot 2 is
    ///      _allowances) to sidestep the CP-proof requirement on
    ///      buck.approve().  This suite focuses on Notes mint/spend
    ///      mechanics, not the CP plumbing.
    function _approveNotes(address from, uint256 amount) internal {
        bytes32 slot = keccak256(
            abi.encode(address(notes), keccak256(abi.encode(from, uint256(2))))
        );
        vm.store(address(buck), slot, bytes32(amount));
    }

    function _cms(uint256 a, uint256 b) internal pure returns (uint256[] memory cms) {
        cms = new uint256[](2);
        cms[0] = a;
        cms[1] = b;
    }

    /// @dev A length-`n` PUBLIC-mode array (MODE_PUBLIC == 1).  The stub verifier
    ///      ignores the values; Notes only requires issuerMode.length == cms.length.
    function _modes(uint256 n) internal pure returns (uint256[] memory mm) {
        mm = new uint256[](n);
        for (uint256 i = 0; i < n; i++) mm[i] = 1;
    }

    /// @dev Stub-friendly mint: oldRoot pulled from live state, newRoot is a
    ///      caller-chosen scalar (the stub doesn't bind it; production mint
    ///      requires the SNARK-attested newRoot).
    function _stubMint(
        address from,
        uint256[] memory cms,
        uint256 totalFace,
        uint256 newRoot
    ) internal {
        // Snapshot live state BEFORE the gated mint (which pranks `from`).
        uint256 oldRoot       = notes.noteRoot();
        uint32  nextLeafIndex = notes.nextLeafIndex();
        _pubMint(from, DUMMY_PROOF, oldRoot, newRoot, nextLeafIndex, totalFace, cms);
    }

    /// @dev Schnorr for `from` (a bound public issuer) over keccak256(cms),
    ///      using the precomputed pk/R so it does NO ecMul at call time.
    function _sigFor(address from, uint256[] memory cms)
        internal view returns (IdentityRegistry.SchnorrProof memory)
    {
        if (from == bob) return GatedMint.signWith(pkB, RB, SK_B, K_B, cms, from, block.chainid);
        return GatedMint.signWith(pkA, RA, SK_A, K_A, cms, from, block.chainid);
    }

    /// @dev Gated PUBLIC mint as `from`: all-PUBLIC issuerMode + Schnorr.  The
    ///      sig + mode are built BEFORE vm.prank (no external call burns the
    ///      prank, and the ecMul-free signWith is safe under vm.expectRevert);
    ///      callers pass deliberately-bad oldRoot/newRoot/idx to exercise guards.
    function _pubMint(
        address from,
        bytes memory proof,
        uint256 oldRoot,
        uint256 newRoot,
        uint32  idx,
        uint256 totalFace,
        uint256[] memory cms
    ) internal {
        IdentityRegistry.SchnorrProof memory sig = _sigFor(from, cms);
        uint256[] memory mode = GatedMint.allPublic(cms.length);
        vm.prank(from);
        notes.mint(proof, oldRoot, newRoot, idx, totalFace, cms, mode, sig);
    }

    // ---- constructor -------------------------------------------------------

    function test_constructor_setsImmutables() public view {
        assertEq(address(notes.buck()),          address(buck));
        assertEq(address(notes.mintVerifier()),  address(stub));
        assertEq(notes.governance(),             GOV);
        assertEq(notes.nextLeafIndex(),          0);
        assertEq(notes.noteFaceSum(),            0);
        assertEq(notes.noteRoot(),               EMPTY_ROOT_);
    }

    function test_constructor_rejectsZero() public {
        vm.expectRevert(bytes("buck=0"));
        new Notes(address(0), address(stub), address(spendStub), GOV);
        vm.expectRevert(bytes("mintVerifier=0"));
        new Notes(address(buck), address(0), address(spendStub), GOV);
        vm.expectRevert(bytes("spendVerifier=0"));
        new Notes(address(buck), address(stub), address(0), GOV);
        vm.expectRevert(bytes("governance=0"));
        new Notes(address(buck), address(stub), address(spendStub), address(0));
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

        uint256 newRoot = uint256(keccak256("newRoot1")) % notes.FIELD_R();
        _stubMint(alice, cms, face, newRoot);

        assertEq(buck.balanceOf(alice),         aliceBefore - face);
        assertEq(buck.balanceOf(address(notes)), poolBefore + face);
        assertEq(notes.noteFaceSum(),           face);
        assertEq(notes.nextLeafIndex(),         2);
        assertEq(notes.noteRoot(),              newRoot);
        assertTrue(notes.isAcceptedRoot(newRoot));
        // The empty-tree root is still in the recent-roots window.
        assertTrue(notes.isAcceptedRoot(EMPTY_ROOT_));
    }

    function test_mint_emitsMintedEvent() public {
        _approveNotes(alice, 200e18);
        uint256[] memory cms = _cms(CM1, CM2);

        uint256 newRoot = uint256(keccak256("e1")) % notes.FIELD_R();
        vm.expectEmit(true, false, true, true, address(notes));
        emit Notes.Minted(alice, 200e18, 0, 2, newRoot);

        _stubMint(alice, cms, 200e18, newRoot);
    }

    function test_mint_secondBatchExtendsLeafIndex() public {
        _approveNotes(alice, 300e18);
        uint256[] memory first = _cms(CM1, CM2);
        uint256 r1 = uint256(keccak256("r1")) % notes.FIELD_R();
        _stubMint(alice, first, 200e18, r1);

        uint256[] memory second = new uint256[](1);
        second[0] = CM3;
        uint256 r2 = uint256(keccak256("r2")) % notes.FIELD_R();
        _stubMint(alice, second, 100e18, r2);

        assertEq(notes.nextLeafIndex(), 3);
        assertEq(notes.noteFaceSum(),   300e18);
        assertEq(notes.noteRoot(),      r2);
        // Both prior roots still in the window.
        assertTrue(notes.isAcceptedRoot(r1));
        assertTrue(notes.isAcceptedRoot(EMPTY_ROOT_));
    }

    function test_mint_rejectsEmptyBatch() public {
        _approveNotes(alice, 0);
        uint256[] memory empty;
        vm.expectRevert(bytes("Notes: empty mint"));
        _pubMint(alice, DUMMY_PROOF, EMPTY_ROOT_, EMPTY_ROOT_, 0, 0, empty);
    }

    function test_mint_rejectsStaleOldRoot() public {
        _approveNotes(alice, 100e18);
        uint256[] memory cms = new uint256[](1);
        cms[0] = CM1;
        vm.expectRevert(bytes("Notes: stale oldRoot"));
        _pubMint(alice, DUMMY_PROOF, uint256(0xdeadbeef), 1, 0, 100e18, cms);
    }

    function test_mint_rejectsStaleNextLeafIndex() public {
        _approveNotes(alice, 100e18);
        uint256[] memory cms = new uint256[](1);
        cms[0] = CM1;
        // oldRoot is correct, nextLeafIndex is wrong (claim 5 instead of 0).
        vm.expectRevert(bytes("Notes: stale nextLeafIndex"));
        _pubMint(alice, DUMMY_PROOF, EMPTY_ROOT_, 1, 5, 100e18, cms);
    }

    function test_mint_rejectsZeroNewRoot() public {
        _approveNotes(alice, 100e18);
        uint256[] memory cms = new uint256[](1);
        cms[0] = CM1;
        vm.expectRevert(bytes("Notes: zero newRoot"));
        _pubMint(alice, DUMMY_PROOF, EMPTY_ROOT_, 0, 0, 100e18, cms);
    }

    function test_mint_rejectsNewRootOutOfField() public {
        _approveNotes(alice, 100e18);
        uint256[] memory cms = new uint256[](1);
        cms[0] = CM1;
        uint256 fieldR = notes.FIELD_R();
        vm.expectRevert(bytes("Notes: newRoot out of field"));
        _pubMint(alice, DUMMY_PROOF, EMPTY_ROOT_, fieldR, 0, 100e18, cms);
    }

    function test_mint_rejectsCommitmentOutOfField() public {
        _approveNotes(alice, 100e18);
        uint256[] memory bad = new uint256[](1);
        bad[0] = notes.FIELD_R();  // exactly r is out of [0, r)
        vm.expectRevert(bytes("Notes: cm out of field"));
        _pubMint(alice, DUMMY_PROOF, EMPTY_ROOT_, 1, 0, 100e18, bad);
    }

    function test_mint_rejectedByDisabledStub() public {
        // Disable the stub -> verifyMint() returns false.
        vm.prank(GOV);
        stub.setEnabled(false);

        _approveNotes(alice, 100e18);
        uint256[] memory cms = new uint256[](1);
        cms[0] = CM1;
        vm.expectRevert(bytes("Notes: bad mint proof"));
        _pubMint(alice, DUMMY_PROOF, EMPTY_ROOT_, 1, 0, 100e18, cms);
    }

    function test_mint_rejectedByExternalRejectingVerifier() public {
        RejectingMintVerifier rej = new RejectingMintVerifier();
        vm.prank(GOV);
        notes.setMintVerifier(address(rej));

        _approveNotes(alice, 100e18);
        uint256[] memory cms = new uint256[](1);
        cms[0] = CM1;
        vm.expectRevert(bytes("Notes: bad mint proof"));
        _pubMint(alice, DUMMY_PROOF, EMPTY_ROOT_, 1, 0, 100e18, cms);
    }

    function test_mint_revertsOnMissingApproval() public {
        // No allowance set.
        uint256[] memory cms = new uint256[](1);
        cms[0] = CM1;
        vm.expectRevert(); // OZ ERC20InsufficientAllowance
        _pubMint(alice, DUMMY_PROOF, EMPTY_ROOT_, 1, 0, 100e18, cms);
    }

    function test_mint_revertsWhenIssuerLacksBalance() public {
        // Bob is verified but has zero BUCK balance and no credit.
        _approveNotes(bob, 100e18);
        uint256[] memory cms = new uint256[](1);
        cms[0] = CM1;
        vm.expectRevert(); // OZ ERC20InsufficientBalance
        _pubMint(bob, DUMMY_PROOF, EMPTY_ROOT_, 1, 0, 100e18, cms);
    }

    function test_mint_failedTransferLeavesNoStateMutation() public {
        _approveNotes(alice, 50e18);
        uint256[] memory cms = _cms(CM1, CM2);
        vm.expectRevert();
        _pubMint(alice, DUMMY_PROOF, EMPTY_ROOT_, 1, 0, 100e18, cms);

        // No advancement.
        assertEq(notes.nextLeafIndex(), 0);
        assertEq(notes.noteFaceSum(),   0);
        assertEq(notes.noteRoot(),      EMPTY_ROOT_);
    }

    /// @notice Public commitments carry immutable mint-time issuer attribution
    ///         for B1 spends.  A duplicate within one batch must therefore
    ///         revert atomically: neither the attribution written by the first
    ///         loop iteration nor the note-tree/accounting update may survive.
    function test_mint_rejectsDuplicateCommitmentInBatch() public {
        _approveNotes(alice, 200e18);
        uint256[] memory dup = _cms(CM1, CM1);
        uint256 newRoot = uint256(keccak256("dup")) % notes.FIELD_R();
        vm.expectRevert(bytes("Notes: duplicate public commitment"));
        _pubMint(alice, DUMMY_PROOF, EMPTY_ROOT_, newRoot, 0, 200e18, dup);

        assertEq(notes.nextLeafIndex(), 0);
        assertEq(notes.noteFaceSum(),   0);
        assertEq(notes.publicIssuerOfCommitment(CM1), address(0));
    }

    function test_mint_rejectsDuplicateAcrossBatches() public {
        _approveNotes(alice, 200e18);
        uint256[] memory first = new uint256[](1);
        first[0] = CM1;
        uint256 r1 = uint256(keccak256("a")) % notes.FIELD_R();
        _stubMint(alice, first, 100e18, r1);

        uint256[] memory second = new uint256[](1);
        second[0] = CM1; // same cm again
        uint256 r2 = uint256(keccak256("b")) % notes.FIELD_R();
        vm.expectRevert(bytes("Notes: duplicate public commitment"));
        _pubMint(alice, DUMMY_PROOF, r1, r2, 1, 100e18, second);

        assertEq(notes.nextLeafIndex(), 1);
        assertEq(notes.noteFaceSum(),   100e18);
        assertEq(notes.publicIssuerOfCommitment(CM1), alice);
    }

    // ---- root window -------------------------------------------------------

    function test_isAcceptedRoot_acceptsLiveAndPriorRoots() public {
        assertTrue(notes.isAcceptedRoot(EMPTY_ROOT_));

        _approveNotes(alice, 100e18);
        uint256[] memory cms = new uint256[](1);
        cms[0] = CM1;
        uint256 r1 = uint256(keccak256("R")) % notes.FIELD_R();
        _stubMint(alice, cms, 100e18, r1);

        assertTrue(notes.isAcceptedRoot(EMPTY_ROOT_));
        assertTrue(notes.isAcceptedRoot(r1));
        assertFalse(notes.isAcceptedRoot(uint256(0xdeadbeef)));
        assertFalse(notes.isAcceptedRoot(0));
    }

    function test_isAcceptedRoot_evictsAfterHistoryWindow() public {
        // Each mint advances the ring by one slot.  ROOT_HISTORY_SIZE = 30,
        // so 31 single-leaf mints overwrite the genesis root.
        _approveNotes(alice, 31 * 1e18);
        for (uint256 i = 0; i < 31; i++) {
            uint256[] memory batch = new uint256[](1);
            batch[0] = uint256(keccak256(abi.encode("cm", i))) % notes.FIELD_R();
            uint256 r = uint256(keccak256(abi.encode("rt", i))) % notes.FIELD_R();
            _stubMint(alice, batch, 1e18, r);
        }

        assertFalse(notes.isAcceptedRoot(EMPTY_ROOT_),
            "empty root should have been evicted");
        assertTrue(notes.isAcceptedRoot(notes.noteRoot()));
    }

    /// @notice Concurrency model: Bob lands a mint, Alice's pre-Bob proof
    ///         must revert (stale oldRoot OR stale nextLeafIndex), no BUCK
    ///         is moved from Alice, and Alice can re-mint after re-syncing.
    function test_mint_concurrencyLoserRevertsCleanly() public {
        _approveNotes(alice, 200e18);
        _grantCredit(bob, 1000e18);
        vm.prank(bob);
        buck.mint(200e18);
        _approveNotes(bob, 100e18);

        uint256 aliceBalBefore = buck.balanceOf(alice);

        // Snapshot the pre-Bob state Alice's prover would have used.
        uint256 staleOldRoot       = notes.noteRoot();
        uint32  staleNextLeafIndex = notes.nextLeafIndex();

        // Bob mints first, advancing both nextLeafIndex and noteRoot.
        uint256[] memory bobCms = new uint256[](1);
        bobCms[0] = CM3;
        uint256 bobRoot = uint256(keccak256("bobwins")) % notes.FIELD_R();
        _stubMint(bob, bobCms, 100e18, bobRoot);
        assertEq(notes.nextLeafIndex(), 1);
        assertEq(notes.noteRoot(),      bobRoot);

        // Alice tries to land her stale proof -- both guards would catch it,
        // but oldRoot fires first.  No BUCK movement, alice's balance
        // unchanged, no leaf-index advancement beyond Bob's contribution.
        uint256[] memory aliceCms = _cms(CM1, CM2);
        uint256 aliceRoot = uint256(keccak256("aliceloses")) % notes.FIELD_R();
        vm.expectRevert(bytes("Notes: stale oldRoot"));
        _pubMint(alice, DUMMY_PROOF, staleOldRoot, aliceRoot, staleNextLeafIndex, 200e18, aliceCms);
        assertEq(buck.balanceOf(alice), aliceBalBefore);

        // Alice re-syncs and re-mints against the new live state.
        uint256 aliceRoot2 = uint256(keccak256("aliceretries")) % notes.FIELD_R();
        uint256 liveOldRoot       = notes.noteRoot();
        uint32  liveNextLeafIndex = notes.nextLeafIndex();
        _pubMint(alice, DUMMY_PROOF, liveOldRoot, aliceRoot2, liveNextLeafIndex, 200e18, aliceCms);
        assertEq(notes.nextLeafIndex(), 3);
        assertEq(notes.noteRoot(),      aliceRoot2);
        assertEq(buck.balanceOf(alice), aliceBalBefore - 200e18);
    }
}
