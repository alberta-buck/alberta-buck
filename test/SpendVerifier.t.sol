// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {BN254}                  from "../src/BN254.sol";
import {IdentityRegistry}       from "../src/IdentityRegistry.sol";
import {Buck}                   from "../src/Buck.sol";
import {BuckCredit}             from "../src/BuckCredit.sol";
import {BuckCreditHarness}             from "./harness/BuckCreditHarness.sol";
import {BuckKControllerStatic}  from "../src/BuckKControllerStatic.sol";
import {Notes}                  from "../src/Notes.sol";
import {SpendGroth16Verifier}   from "../src/SpendGroth16Verifier.sol";
import {SpendVerifierAdapter}   from "../src/SpendVerifierAdapter.sol";
import {StubMintVerifier}       from "../src/StubMintVerifier.sol";

/// @title SpendVerifier.t.sol -- end-to-end Groth16 spend.
/// @notice The Phase 7-bis pivot moved per-leaf Merkle insertion into the
///         mint SNARK; the legacy spend fixture's `noteRoot` was generated
///         against a tree containing a specific commitment opening.  To keep
///         the spend test orthogonal to the mint pivot, we use the stub mint
///         verifier to seed Notes' rolling root directly to the value the
///         spend fixture expects -- the spend SNARK then exercises the same
///         (noteRoot, nullifier, face, recipient, chainId) public binding it
///         always did.
///
/// @dev    Future work: regenerate the spend fixture against a tree built by
///         the new prove_mint_batch.js so the test can run "real mint + real
///         spend" chained.  Tracked in alberta-buck-notes-rollup-mint.org.
contract SpendVerifierTest is Test {

    Buck                    internal buck;
    BuckCreditHarness              internal credit;
    BuckKControllerStatic   internal kCtrl;
    IdentityRegistry        internal reg;
    Notes                   internal notes;
    StubMintVerifier        internal mintStub;
    SpendGroth16Verifier    internal spendG16;
    SpendVerifierAdapter    internal spendAdapter;

    address internal constant GOV    = address(0xA0);
    address internal constant ISSUER = address(0x1551E1);
    address internal constant POOL   = address(0xBA51C);

    address internal alice;
    address internal bob;

    // Spend fixture (leaf 0 -> bob).
    uint256 internal fxSpendNoteRoot;
    uint256 internal fxSpendNullifier;
    uint256 internal fxSpendFace;
    address internal fxSpendRecipient;
    uint256 internal fxSpendChainId;
    bytes   internal fxSpendProof;

    function setUp() public {
        vm.chainId(1);

        string memory ij = vm.readFile("test/vectors/identity.json");
        reg = new IdentityRegistry(GOV);
        _trustIssuer(ij);
        alice = address(uint160(vm.parseJsonUint(ij, ".alice.registrant")));
        bob   = address(uint160(vm.parseJsonUint(ij, ".bob.registrant")));
        _registerFrom(ij, "alice", alice);
        _registerFrom(ij, "bob",   bob);

        credit = new BuckCreditHarness();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));

        mintStub     = new StubMintVerifier(GOV);
        spendG16     = new SpendGroth16Verifier();
        spendAdapter = new SpendVerifierAdapter(address(spendG16));
        notes = new Notes(
            address(buck),
            address(mintStub),
            address(spendAdapter),
            GOV
        );

        // Bind Notes as a Public-Identity contract.
        reg.bindContract(
            address(notes),
            BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}),
            true, // isPublicIdentity
            true  // isCarrying
        );
        // Mutual decryptability: Alice and Bob must CP-approve the public Notes.
        {
            bytes32 _fragSlot = keccak256(
                abi.encode(address(notes), keccak256(abi.encode(alice, uint256(5))))
            );
            vm.store(address(buck), _fragSlot, bytes32(uint256(1)));
            _fragSlot = keccak256(
                abi.encode(address(notes), keccak256(abi.encode(bob, uint256(5))))
            );
            vm.store(address(buck), _fragSlot, bytes32(uint256(1)));
        }

        _grantCredit(alice, 1000e18);
        vm.prank(alice);
        buck.mint(500e18);
        _approveNotes(alice, 500e18);

        // Spend fixture (leaf 0 -> bob).
        string memory spendFx =
            vm.readFile("build/snark/spend/fixtures/spend_leaf0_to_bob.json");
        fxSpendNoteRoot  = vm.parseJsonUint(spendFx, ".spend.public.noteRoot");
        fxSpendNullifier = vm.parseJsonUint(spendFx, ".spend.public.nullifier");
        fxSpendFace      = vm.parseJsonUint(spendFx, ".spend.public.face");
        fxSpendRecipient = vm.parseJsonAddress(spendFx, ".spend.public.recipient");
        fxSpendChainId   = vm.parseJsonUint(spendFx, ".spend.public.chainId");
        fxSpendProof     = vm.parseJsonBytes(spendFx, ".spend.proofBytes");
    }

    // ---- Identity JSON loaders (mirrors MintVerifier.t.sol) ----------------

    function _u(string memory j, string memory k) internal pure returns (uint256) {
        return vm.parseJsonUint(j, k);
    }

    function _g1(string memory j, string memory k) internal pure returns (BN254.G1Point memory) {
        return BN254.G1Point(
            _u(j, string.concat(k, ".x")),
            _u(j, string.concat(k, ".y"))
        );
    }

    function _ct(string memory j, string memory k)
        internal pure returns (IdentityRegistry.ElGamalCT memory c)
    {
        c.R = _g1(j, string.concat(k, ".R"));
        c.C = _g1(j, string.concat(k, ".C"));
    }

    function _trustIssuer(string memory j) internal {
        IdentityRegistry.PSPubKey memory ipk;
        ipk.X.X[0] = _u(j, ".issuer.pk_X.x[0]");
        ipk.X.X[1] = _u(j, ".issuer.pk_X.x[1]");
        ipk.X.Y[0] = _u(j, ".issuer.pk_X.y[0]");
        ipk.X.Y[1] = _u(j, ".issuer.pk_X.y[1]");
        ipk.Y.X[0] = _u(j, ".issuer.pk_Y.x[0]");
        ipk.Y.X[1] = _u(j, ".issuer.pk_Y.x[1]");
        ipk.Y.Y[0] = _u(j, ".issuer.pk_Y.y[0]");
        ipk.Y.Y[1] = _u(j, ".issuer.pk_Y.y[1]");
        vm.prank(GOV);
        reg.trustIssuer(ISSUER, ipk);
    }

    function _registerFrom(string memory j, string memory who, address acct) internal {
        BN254.G1Point memory pk = _g1(j, string.concat(".", who, ".elgamal_kp.pk"));
        IdentityRegistry.ElGamalCT memory E = _ct(j, string.concat(".", who, ".ciphertext"));
        IdentityRegistry.PSSig memory sigma;
        sigma.sigma_1 = _g1(j, string.concat(".", who, ".ps_sig_rerand.sigma_1"));
        sigma.sigma_2 = _g1(j, string.concat(".", who, ".ps_sig_rerand.sigma_2"));
        IdentityRegistry.RegistrationProof memory p;
        string memory base = string.concat(".", who, ".registration_proof");
        p.e    = _u(j, string.concat(base, ".e"));
        p.s_m  = _u(j, string.concat(base, ".s_m"));
        p.s_r  = _u(j, string.concat(base, ".s_r"));
        p.A_ps = _g1(j, string.concat(base, ".A_ps"));
        p.T_C  = _g1(j, string.concat(base, ".T_C"));
        p.T_R  = _g1(j, string.concat(base, ".T_R"));
        vm.prank(acct);
        reg.register(ISSUER, pk, E, sigma, p);
    }

    function _grantCredit(address client, uint256 faceValue) internal {
        uint256 tokenId = credit.createCredit(
            client, 0, faceValue, faceValue,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(client);
        credit.forceActivate(tokenId, faceValue);
    }

    function _approveNotes(address from, uint256 amount) internal {
        // _allowances lives at slot 2 in the new packed-state Buck.sol
        // (slot 0 is _state, slot 1 is _totalSupply).
        bytes32 slot = keccak256(
            abi.encode(address(notes), keccak256(abi.encode(from, uint256(2))))
        );
        vm.store(address(buck), slot, bytes32(amount));
    }

    /// @dev Seed Notes' rolling root to the value the spend fixture expects.
    ///      Two stub mints because the legacy spend fixture covers a tree
    ///      with two leaves (CM1, CM2) -- nextLeafIndex must be 2 for the
    ///      ROOT_HISTORY_SIZE assertion to remain meaningful.  The first
    ///      stub mint pulls fxSpendFace BUCK from Alice to back the spend.
    function _seedTreeForSpend() internal {
        // Single batched stub mint with two arbitrary commitments and the
        // SNARK-attested root pinned to the spend fixture's expected value.
        uint256[] memory cms = new uint256[](2);
        cms[0] = uint256(keccak256("seedcm0")) % notes.FIELD_R();
        cms[1] = uint256(keccak256("seedcm1")) % notes.FIELD_R();
        // Snapshot live state BEFORE vm.prank so argument-eval calls don't
        // burn the prank.
        uint256 oldRoot       = notes.noteRoot();
        uint32  nextLeafIndex = notes.nextLeafIndex();
        vm.prank(alice);
        notes.mint(
            hex"deadbeef",
            oldRoot,                // empty tree
            fxSpendNoteRoot,        // newRoot = spend fixture's expected root
            nextLeafIndex,
            fxSpendFace,            // pull exactly enough BUCK to cover spend
            cms
        );
    }

    // ---- tests -------------------------------------------------------------

    function test_seed_treeRootMatchesProverExpectation() public {
        _seedTreeForSpend();
        assertEq(notes.noteRoot(), fxSpendNoteRoot,
            "seeded root did not land on prover-expected root");
        assertTrue(notes.isAcceptedRoot(fxSpendNoteRoot));
        assertEq(notes.nextLeafIndex(), 2);
    }

    function test_spend_happyPath() public {
        _seedTreeForSpend();
        uint256 poolRawBefore = buck.rawBalanceOf(address(notes));
        uint256 bobRawBefore  = buck.rawBalanceOf(bob);
        uint256 faceSumBefore = notes.noteFaceSum();

        assertFalse(notes.nullifiers(fxSpendNullifier));

        notes.spend(
            fxSpendProof,
            fxSpendNoteRoot,
            fxSpendNullifier,
            fxSpendFace,
            fxSpendRecipient
        );

        assertEq(buck.rawBalanceOf(address(notes)), poolRawBefore - fxSpendFace,
            "pool raw balance did not decrease by face");
        assertEq(buck.rawBalanceOf(bob), bobRawBefore + fxSpendFace,
            "bob raw balance did not increase by face");
        assertEq(notes.noteFaceSum(), faceSumBefore - fxSpendFace,
            "noteFaceSum not decremented");
        assertTrue(notes.nullifiers(fxSpendNullifier),
            "nullifier not burned");
    }

    function test_spend_emitsSpentEvent() public {
        _seedTreeForSpend();
        vm.expectEmit(true, true, false, true, address(notes));
        emit Notes.Spent(fxSpendNullifier, fxSpendFace, fxSpendRecipient);
        notes.spend(
            fxSpendProof,
            fxSpendNoteRoot,
            fxSpendNullifier,
            fxSpendFace,
            fxSpendRecipient
        );
    }

    function test_spend_doubleSpendReverts() public {
        _seedTreeForSpend();
        notes.spend(
            fxSpendProof, fxSpendNoteRoot, fxSpendNullifier,
            fxSpendFace, fxSpendRecipient
        );
        vm.expectRevert(bytes("Notes: already spent"));
        notes.spend(
            fxSpendProof, fxSpendNoteRoot, fxSpendNullifier,
            fxSpendFace, fxSpendRecipient
        );
    }

    function test_spend_rejectedOnUnknownRoot() public {
        _seedTreeForSpend();
        vm.expectRevert(bytes("Notes: unknown root"));
        notes.spend(
            fxSpendProof,
            fxSpendNoteRoot ^ 1,      // any value not in the ring buffer
            fxSpendNullifier,
            fxSpendFace,
            fxSpendRecipient
        );
    }

    function test_spend_rejectedOnTamperedRecipient() public {
        _seedTreeForSpend();
        vm.expectRevert(bytes("Notes: bad spend proof"));
        notes.spend(
            fxSpendProof, fxSpendNoteRoot, fxSpendNullifier,
            fxSpendFace, alice                     // != fxSpendRecipient
        );
    }

    function test_spend_rejectedOnTamperedFace() public {
        _seedTreeForSpend();
        vm.expectRevert(bytes("Notes: bad spend proof"));
        notes.spend(
            fxSpendProof, fxSpendNoteRoot, fxSpendNullifier,
            fxSpendFace + 1, fxSpendRecipient
        );
    }

    function test_spend_rejectedOnTamperedNullifier() public {
        _seedTreeForSpend();
        vm.expectRevert(bytes("Notes: bad spend proof"));
        notes.spend(
            fxSpendProof, fxSpendNoteRoot, fxSpendNullifier ^ 1,
            fxSpendFace, fxSpendRecipient
        );
    }

    function test_spend_rejectedOnChainIdMismatch() public {
        _seedTreeForSpend();
        // Spend proof was generated for chainid=1; re-chain the VM so the
        // Groth16 public-input binding fails.
        vm.chainId(fxSpendChainId + 1);
        vm.expectRevert(bytes("Notes: bad spend proof"));
        notes.spend(
            fxSpendProof, fxSpendNoteRoot, fxSpendNullifier,
            fxSpendFace, fxSpendRecipient
        );
    }

    function test_spend_rejectedOnZeroRecipient() public {
        _seedTreeForSpend();
        vm.expectRevert(bytes("Notes: zero recipient"));
        notes.spend(
            fxSpendProof, fxSpendNoteRoot, fxSpendNullifier,
            fxSpendFace, address(0)
        );
    }

    /// @notice With demurrage accumulating on the pool between mint and
    ///         spend, the recipient absorbs the pool's average age via
    ///         the registry-dispatched Carrying transfer (Notes is bound
    ///         with isCarrying = true, so notes.spend's transfer call lands
    ///         in Buck's Carrying path automatically).
    function test_spend_recipientAbsorbsPoolAge() public {
        _seedTreeForSpend();
        skip(30 days);

        uint256 supplyBefore = buck.totalSupply();
        notes.spend(
            fxSpendProof, fxSpendNoteRoot, fxSpendNullifier,
            fxSpendFace, fxSpendRecipient
        );

        assertEq(buck.rawBalanceOf(bob), fxSpendFace,
            "bob raw balance is not exactly face");
        assertGt(buck.feeOwing(bob), 0,
            "bob inherits pool's BUCK-age via Carrying dispatch");
        // The transfer itself does not change totalSupply (no mint/burn
        // happened in this call); supply is unchanged.
        assertEq(buck.totalSupply(), supplyBefore,
            "spend transfer preserves totalSupply");
    }
}
