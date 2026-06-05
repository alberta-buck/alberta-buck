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
import {SpendAGroth16Verifier}  from "../src/SpendAGroth16Verifier.sol";
import {SpendGroth16Verifier}   from "../src/SpendGroth16Verifier.sol";
import {SpendVerifierAdapter}   from "../src/SpendVerifierAdapter.sol";
import {SpendAVerifierAdapter}  from "../src/SpendAVerifierAdapter.sol";
import {StubMintVerifier}       from "../src/StubMintVerifier.sol";
import {GatedMint}              from "./helpers/GatedMint.sol";

/// @title SpendAVerifier.t.sol -- end-to-end Groth16-verified A-spend (V2).
/// @notice Exercises Notes.spendACP() against the spend_a circuit V2.
///         Confirms:
///           * happy-path A-spend redeems face -> recipient, burns the
///             A-tag (4243) nullifier, and only Alice (the registered
///             owner of the note) can spend it;
///           * tamper paths (face / nullifier / recipient / chainId / E_n)
///             are rejected by the spend_a Groth16 verifier;
///           * the off-chain identity binding (IdentityRegistry.verifySpendCP)
///             rejects spends from any address other than the registered
///             recipient (msg.sender != alice => bad identity proof);
///           * the on-chain `nullifiers` mapping is shared with B-spend yet
///             cannot collide because the SNARKs enforce disjoint tags;
///           * setSpendAVerifier / setIdentityRegistry governance + the
///             address(0) lockdown paths work as advertised.
contract SpendAVerifierTest is Test {

    Buck                    internal buck;
    BuckCreditHarness              internal credit;
    BuckKControllerStatic   internal kCtrl;
    IdentityRegistry        internal reg;
    Notes                   internal notes;
    StubMintVerifier        internal mintStub;
    SpendGroth16Verifier    internal spendBG16;
    SpendAGroth16Verifier   internal spendAG16;
    SpendVerifierAdapter    internal spendBAdapter;
    SpendAVerifierAdapter   internal spendAAdapter;

    address internal constant GOV    = address(0xA0);
    address internal constant ISSUER = address(0x1551E1);
    address internal constant POOL   = address(0xBA51C);

    address internal alice;
    address internal bob;

    // Public issuer for the gated seed mint.
    address internal pubMinter = address(0x9E27E5);
    uint256 internal constant SK_PM = 0x1111111111111111111111111111111111111111111111111111111111111111;
    uint256 internal constant K_PM  = 0x2222222222222222222222222222222222222222222222222222222222222222;

    // V2 A-spend fixture (single-leaf tree, leaf 0 -> SPEND_RECIPIENT).
    uint256 internal fxNoteRoot;
    uint256 internal fxNullifier;
    uint256 internal fxFace;
    address internal fxRecipient;
    uint256 internal fxChainId;
    uint256 internal fxCm;
    bytes   internal fxProof;
    IdentityRegistry.ElGamalCT    internal fxEN;
    IdentityRegistry.SpendCPProof internal fxCP;

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

        // Two Groth16 verifiers, two adapters: B-spend wraps the legacy
        // spend.circom verifier (so Notes.spend() works); A-spend V2 wraps
        // the new spend_a.circom verifier with 9 public inputs and is hooked
        // up via setSpendAVerifier.
        mintStub      = new StubMintVerifier(GOV);
        spendBG16     = new SpendGroth16Verifier();
        spendAG16     = new SpendAGroth16Verifier();
        spendBAdapter = new SpendVerifierAdapter (address(spendBG16));
        spendAAdapter = new SpendAVerifierAdapter(address(spendAG16));
        notes = new Notes(
            address(buck),
            address(mintStub),
            address(spendBAdapter),
            GOV
        );
        vm.startPrank(GOV);
        notes.setSpendAVerifier   (address(spendAAdapter));
        notes.setIdentityRegistry (address(reg));
        vm.stopPrank();

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

        // Public issuer for the gated seed mint (bound public Identity, funded).
        vm.etch(pubMinter, hex"60006000fd");
        reg.bindContract(pubMinter, BN254.mul(BN254.g1(), SK_PM),
                         IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1()), true, false);
        _grantCredit(pubMinter, 1000e18);
        vm.prank(pubMinter);
        buck.mint(500e18);
        _approveNotes(pubMinter, 500e18);
        vm.store(address(buck),
                 keccak256(abi.encode(address(notes), keccak256(abi.encode(pubMinter, uint256(5))))),
                 bytes32(uint256(1)));

        // Load the V2 A-spend fixture (single-leaf -> SPEND_RECIPIENT, chainId 1).
        string memory fx =
            vm.readFile("build/snark/spend_a/fixtures/spendA_v2_alice.json");
        fxNoteRoot   = vm.parseJsonUint   (fx, ".spend.public.noteRoot");
        fxNullifier  = vm.parseJsonUint   (fx, ".spend.public.nullifier");
        fxFace       = vm.parseJsonUint   (fx, ".spend.public.face");
        fxRecipient  = address(uint160(vm.parseJsonUint(fx, ".spend.public.recipient")));
        fxChainId    = vm.parseJsonUint   (fx, ".spend.public.chainId");
        fxCm         = vm.parseJsonUint   (fx, ".mint.cm");
        fxProof      = vm.parseJsonBytes  (fx, ".spend.proofBytes");

        fxEN.R = BN254.G1Point(
            vm.parseJsonUint(fx, ".spend.E_n.R.x"),
            vm.parseJsonUint(fx, ".spend.E_n.R.y")
        );
        fxEN.C = BN254.G1Point(
            vm.parseJsonUint(fx, ".spend.E_n.C.x"),
            vm.parseJsonUint(fx, ".spend.E_n.C.y")
        );
        fxCP.e  = vm.parseJsonUint(fx, ".spend.cpProof.e");
        fxCP.s  = vm.parseJsonUint(fx, ".spend.cpProof.s");
        fxCP.T1 = BN254.G1Point(
            vm.parseJsonUint(fx, ".spend.cpProof.T1.x"),
            vm.parseJsonUint(fx, ".spend.cpProof.T1.y")
        );
        fxCP.T2 = BN254.G1Point(
            vm.parseJsonUint(fx, ".spend.cpProof.T2.x"),
            vm.parseJsonUint(fx, ".spend.cpProof.T2.y")
        );
    }

    // ---- Identity JSON loaders (mirrors SpendVerifier.t.sol) --------------

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
        // _allowances at slot 2 in packed-state Buck layout.
        bytes32 slot = keccak256(
            abi.encode(address(notes), keccak256(abi.encode(from, uint256(2))))
        );
        vm.store(address(buck), slot, bytes32(amount));
    }

    /// @dev Seed Notes' rolling root to the value the V2 fixture expects:
    ///      a single-leaf tree with cm at index 0.  The stub mint verifier
    ///      accepts any proof, so we pass `fxCm` straight through.
    function _seedTreeForSpendA() internal {
        uint256[] memory cms = new uint256[](1);
        cms[0] = fxCm;
        uint256 oldRoot       = notes.noteRoot();
        uint32  nextLeafIndex = notes.nextLeafIndex();
        uint256[] memory mode = GatedMint.allPublic(1);
        IdentityRegistry.SchnorrProof memory sig =
            GatedMint.signPublic(SK_PM, K_PM, cms, pubMinter, block.chainid);
        vm.prank(pubMinter);
        notes.mint(
            hex"deadbeef",
            oldRoot, fxNoteRoot, nextLeafIndex,
            fxFace, cms, mode, sig
        );
    }

    function _spendA(address sender) internal {
        vm.prank(sender);
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, fxFace, fxRecipient,
            fxEN, fxCP, ""
        );
    }

    // ---- happy path -------------------------------------------------------

    function test_fixtureLoaded() public {
        assertEq(fxChainId, 1, "fixture chainId is 1");
        assertGt(fxFace, 0);
        assertEq(fxProof.length, 256);
        assertTrue(fxRecipient != address(0));
    }

    function test_seed_treeRootMatchesProverExpectation() public {
        _seedTreeForSpendA();
        assertEq(notes.noteRoot(), fxNoteRoot,
            "seeded root did not land on prover-expected root");
        assertTrue(notes.isAcceptedRoot(fxNoteRoot));
        assertEq(notes.nextLeafIndex(), 1);
    }

    function test_spendA_happyPath() public {
        _seedTreeForSpendA();
        uint256 poolRawBefore   = buck.rawBalanceOf(address(notes));
        uint256 recipRawBefore  = buck.rawBalanceOf(fxRecipient);
        uint256 faceSumBefore   = notes.noteFaceSum();

        assertFalse(notes.nullifiers(fxNullifier));

        _spendA(alice);

        assertEq(buck.rawBalanceOf(address(notes)),  poolRawBefore  - fxFace);
        assertEq(buck.rawBalanceOf(fxRecipient),     recipRawBefore + fxFace);
        assertEq(notes.noteFaceSum(),                faceSumBefore  - fxFace);
        assertTrue(notes.nullifiers(fxNullifier),    "A-tag nullifier not burned");
    }

    function test_spendA_emitsSpentAEvent() public {
        _seedTreeForSpendA();
        vm.expectEmit(true, true, false, true, address(notes));
        emit Notes.SpentA(fxNullifier, fxFace, fxRecipient);
        _spendA(alice);
    }

    // ---- nullifier + replay ----------------------------------------------

    function test_spendA_doubleSpendReverts() public {
        _seedTreeForSpendA();
        _spendA(alice);
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: already spent"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, fxFace, fxRecipient,
            fxEN, fxCP, ""
        );
    }

    function test_spendA_aTagDisjointFromBTag() public {
        _seedTreeForSpendA();
        _spendA(alice);
        assertTrue(notes.nullifiers(fxNullifier),
            "A-tag nullifier should be burned");
        assertFalse(notes.nullifiers(fxNullifier ^ 1),
            "burning A-tag must not pollute neighbouring slots");
    }

    // ---- tamper / unbinding (SNARK-side rejections) ----------------------

    function test_spendA_rejectedOnUnknownRoot() public {
        _seedTreeForSpendA();
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: unknown root"));
        notes.spendACP(
            fxProof, fxNoteRoot ^ 1, fxNullifier, fxFace, fxRecipient,
            fxEN, fxCP, ""
        );
    }

    function test_spendA_rejectedOnTamperedRecipient() public {
        _seedTreeForSpendA();
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: bad spend proof"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, fxFace, alice,
            fxEN, fxCP, ""
        );
    }

    function test_spendA_rejectedOnTamperedFace() public {
        _seedTreeForSpendA();
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: bad spend proof"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, fxFace + 1, fxRecipient,
            fxEN, fxCP, ""
        );
    }

    function test_spendA_rejectedOnTamperedNullifier() public {
        _seedTreeForSpendA();
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: bad spend proof"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier ^ 1, fxFace, fxRecipient,
            fxEN, fxCP, ""
        );
    }

    function test_spendA_rejectedOnChainIdMismatch() public {
        _seedTreeForSpendA();
        vm.chainId(fxChainId + 1);
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: bad spend proof"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, fxFace, fxRecipient,
            fxEN, fxCP, ""
        );
    }

    /// @notice Tampering E_n.R must break the SNARK's (I) Poseidon-8 idHash
    ///         binding -- the verifier rejects the changed public input
    ///         before the off-chain CP-DLEQ check ever runs.
    function test_spendA_rejectedOnTamperedEN_R() public {
        _seedTreeForSpendA();
        IdentityRegistry.ElGamalCT memory bad = fxEN;
        bad.R.X = bad.R.X ^ 1;
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: bad spend proof"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, fxFace, fxRecipient,
            bad, fxCP, ""
        );
    }

    function test_spendA_rejectedOnTamperedEN_C() public {
        _seedTreeForSpendA();
        IdentityRegistry.ElGamalCT memory bad = fxEN;
        bad.C.X = bad.C.X ^ 1;
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: bad spend proof"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, fxFace, fxRecipient,
            bad, fxCP, ""
        );
    }

    function test_spendA_rejectedOnZeroRecipient() public {
        _seedTreeForSpendA();
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: zero recipient"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, fxFace, address(0),
            fxEN, fxCP, ""
        );
    }

    function test_spendA_rejectedOnZeroFace() public {
        _seedTreeForSpendA();
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: zero face"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, 0, fxRecipient,
            fxEN, fxCP, ""
        );
    }

    // ---- identity-binding rejections (V2 off-chain CP-DLEQ side) ---------

    /// @notice The CP-DLEQ proof is bound to msg.sender's registered E_addr.
    ///         If anyone other than alice tries to spend, the registry
    ///         lookup hits bob's (or an unregistered) E_addr and the DLEQ
    ///         checks fail.
    function test_spendA_rejectedWhenSpenderNotRegisteredOwner() public {
        _seedTreeForSpendA();
        vm.prank(bob);
        vm.expectRevert(bytes("Notes: bad identity proof"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, fxFace, fxRecipient,
            fxEN, fxCP, ""
        );
    }

    function test_spendA_rejectedWhenSpenderUnregistered() public {
        _seedTreeForSpendA();
        vm.prank(address(0xCAFE));
        vm.expectRevert(bytes("Notes: bad identity proof"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, fxFace, fxRecipient,
            fxEN, fxCP, ""
        );
    }

    /// @notice Tampering the CP-DLEQ challenge `e` must break the FS check.
    function test_spendA_rejectedOnTamperedCP_e() public {
        _seedTreeForSpendA();
        IdentityRegistry.SpendCPProof memory bad = fxCP;
        bad.e = bad.e ^ 1;
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: bad identity proof"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, fxFace, fxRecipient,
            fxEN, bad, ""
        );
    }

    function test_spendA_rejectedOnTamperedCP_s() public {
        _seedTreeForSpendA();
        IdentityRegistry.SpendCPProof memory bad = fxCP;
        bad.s = bad.s ^ 1;
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: bad identity proof"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, fxFace, fxRecipient,
            fxEN, bad, ""
        );
    }

    // ---- governance + lockdown -------------------------------------------

    function test_setSpendAVerifier_governanceOnly() public {
        vm.expectRevert(bytes("not governance"));
        notes.setSpendAVerifier(address(spendAAdapter));
    }

    function test_setIdentityRegistry_governanceOnly() public {
        vm.expectRevert(bytes("not governance"));
        notes.setIdentityRegistry(address(reg));
    }

    function test_setSpendAVerifier_emitsEvent() public {
        SpendAVerifierAdapter alt = new SpendAVerifierAdapter(address(spendAG16));
        vm.expectEmit(true, true, false, true, address(notes));
        emit Notes.SpendAVerifierUpdated(address(spendAAdapter), address(alt));
        vm.prank(GOV);
        notes.setSpendAVerifier(address(alt));
        assertEq(address(notes.spendAVerifier()), address(alt));
    }

    function test_setIdentityRegistry_emitsEvent() public {
        IdentityRegistry alt = new IdentityRegistry(GOV);
        vm.expectEmit(true, true, false, true, address(notes));
        emit Notes.IdentityRegistryUpdated(address(reg), address(alt));
        vm.prank(GOV);
        notes.setIdentityRegistry(address(alt));
        assertEq(address(notes.identityRegistry()), address(alt));
    }

    function test_spendA_disabledWhenVerifierZeroed() public {
        vm.prank(GOV);
        notes.setSpendAVerifier(address(0));
        _seedTreeForSpendA();
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: A-spend disabled"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, fxFace, fxRecipient,
            fxEN, fxCP, ""
        );
    }

    function test_spendA_disabledWhenIdentityRegistryZeroed() public {
        _seedTreeForSpendA();            // seed first (the gated mint needs the registry)
        vm.prank(GOV);
        notes.setIdentityRegistry(address(0));
        vm.prank(alice);
        vm.expectRevert(bytes("Notes: identity registry not set"));
        notes.spendACP(
            fxProof, fxNoteRoot, fxNullifier, fxFace, fxRecipient,
            fxEN, fxCP, ""
        );
    }

    function test_setSpendAVerifier_canRotate() public {
        SpendAVerifierAdapter alt = new SpendAVerifierAdapter(address(spendAG16));
        vm.prank(GOV);
        notes.setSpendAVerifier(address(alt));
        assertEq(address(notes.spendAVerifier()), address(alt));
        vm.prank(GOV);
        notes.setSpendAVerifier(address(spendAAdapter));
        assertEq(address(notes.spendAVerifier()), address(spendAAdapter));
    }
}
