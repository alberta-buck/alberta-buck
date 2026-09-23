// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test, console2} from "forge-std/Test.sol";

import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {PoseidonT3Bytecode} from "../src/PoseidonT3Bytecode.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckCreditHarness} from "./harness/BuckCreditHarness.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";
import {Notes} from "../src/Notes.sol";
import {MintVerifierAdapter} from "../src/MintVerifierAdapter.sol";
import {MintVerifierA2Adapter} from "../src/MintVerifierA2Adapter.sol";
import {MintBatchN1Groth16Verifier} from "../src/MintBatchN1Groth16Verifier.sol";
import {MintBatchA2N1Groth16Verifier} from "../src/MintBatchA2N1Groth16Verifier.sol";
import {SpendGroth16Verifier} from "../src/SpendGroth16Verifier.sol";
import {SpendVerifierAdapter} from "../src/SpendVerifierAdapter.sol";
import {IdentityMembershipB1VerifierAdapter} from "../src/IdentityMembershipB1VerifierAdapter.sol";
import {DepositFoldVerifierAdapter} from "../src/DepositFoldVerifierAdapter.sol";

/// @title NotesE2E -- the full Note lifecycle, every verifier REAL.
/// @notice One mutually-consistent fixture per flavor
///         (alberta_buck/test/vectors/e2e/{a1,a2,b1}.json, from
///         scripts/snark/gen_e2e_fixtures.sh): real batch-mint Groth16, real
///         spend Groth16 against the replayed note tree, real deposit sigma
///         pinned to (depositor, chainid=1), real G1-tie membership proof
///         against the registry's incrementally-built identityRoot, and (A1,
///         A2) the real note<->eEnc binding proof -- A2 via the
///         re-encryption-tie circuit (note_binding.circom), A1 via the
///         A1-layout circuit (note_binding_a1.circom, face public).
///         Confirms operation end to end and measures the per-phase on-chain
///         cost that the docs' cost model quotes.
abstract contract NotesE2EBase is Test {
    address internal constant GOV  = address(0x60);

    IdentityRegistry internal reg;
    BuckCreditHarness internal credit;
    Buck  internal buck;
    Notes internal notes;
    SpendVerifierAdapter internal spendAdapter;
    IdentityMembershipB1VerifierAdapter internal b1MemAdapter;
    DepositFoldVerifierAdapter internal foldAdapter;

    string  internal vj;
    address internal issuer;
    address internal depositor;
    address internal payout;
    uint256 internal face;

    function _flavor() internal pure virtual returns (string memory);

    // ---- vector helpers -----------------------------------------------------

    function _u(string memory key) internal view returns (uint256) {
        return vm.parseJsonUint(vj, key);
    }
    function _addr(string memory key) internal view returns (address) {
        return vm.parseJsonAddress(vj, key);
    }
    function _b(string memory key) internal view returns (bytes memory) {
        return vm.parseJsonBytes(vj, key);
    }
    function _g1(string memory key) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point(_u(string.concat(key, ".x")), _u(string.concat(key, ".y")));
    }
    function _ct(string memory key) internal view returns (IdentityRegistry.ElGamalCT memory c) {
        c.R = _g1(string.concat(key, ".R"));
        c.C = _g1(string.concat(key, ".C"));
    }

    function setUp() public {
        vm.chainId(1);                       // every fixture transcript binds chainid=1
        // The fixture worlds live in the Python package tree (they ship as
        // alberta_buck package data so the wallet's E2E receipt tests run
        // from an installed wheel); forge reads the same files here.
        vj = vm.readFile(string.concat("alberta_buck/test/vectors/e2e/", _flavor(), ".json"));

        issuer    = _addr(".issuer");
        depositor = _addr(".depositor");
        payout    = _addr(".payout");
        face      = _u(".face");

        // ---- IdentityRegistry, and the root the world's aggregator posts ----
        reg = new IdentityRegistryHarness(GOV);
        vm.startPrank(GOV);
        reg.setIdentityPoseidon(PoseidonT3Bytecode.deploy());
        reg.setRootAuthority(GOV);
        reg.setAggregator(GOV);
        vm.stopPrank();

        // Bind the two world accounts.  Their leaves are not a registry call:
        // the world's identity registry admitted them to its own subtree, and
        // the aggregator posts the composed root, which every gate's
        // 32-level path (subtree, then aggregator) folds to.
        for (uint256 i = 0; i < 2; i++) {
            string memory k = string.concat(".binds[", vm.toString(i), "]");
            address a = _addr(string.concat(k, ".addr"));
            vm.etch(a, hex"60006000fd");
            reg.bindContract(
                a,
                _g1(string.concat(k, ".pk")),
                _ct(string.concat(k, ".E")),
                vm.parseJsonBool(vj, string.concat(k, ".isPublic")),
                false
            );
        }
        vm.prank(GOV);
        reg.postIdentityRoot(_u(".identityRoot"), bytes32(0));

        // ---- Buck stack ----
        credit = new BuckCreditHarness();
        buck   = new Buck(address(credit), address(new BuckKControllerStatic(1e18, GOV)),
                          address(reg), address(0xB00C));
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));

        // ---- Real verifier stack ----
        MintVerifierAdapter mintAdapter = new MintVerifierAdapter(GOV);
        MintVerifierA2Adapter a2Adapter = new MintVerifierA2Adapter(GOV);
        vm.startPrank(GOV);
        mintAdapter.registerVerifier(1, address(new MintBatchN1Groth16Verifier()));
        a2Adapter.registerVerifier(1, address(new MintBatchA2N1Groth16Verifier()));
        vm.stopPrank();
        spendAdapter = new SpendVerifierAdapter(address(new SpendGroth16Verifier()));

        notes = new Notes(address(buck), address(mintAdapter), address(spendAdapter), GOV);
        vm.startPrank(GOV);
        notes.setIdentityRegistry(address(reg));
        notes.setA2MintVerifier(address(a2Adapter));
        // B1's membership goes through the REPAIRED circuit: its blind is
        // proven rather than witnessed, and its generator has no known
        // logarithm, without which a depositor could shift the blind onto
        // another registered Identity and spend while unregistered.
        b1MemAdapter = new IdentityMembershipB1VerifierAdapter();
        notes.setIdentityMembershipVerifier(address(b1MemAdapter));

        // The addressed flavours spend through the FOLDED gate: one proof
        // carrying every relation, in place of the coupling sigma, the
        // P-bound membership proof and the note<->eEnc tie.  Those three
        // shared the public point P_I, and an equality inferred across proofs
        // that merely share a point is what a payload thief exploits when the
        // two halves rest on two different secrets.
        foldAdapter = new DepositFoldVerifierAdapter(reg);
        notes.setDepositFoldVerifier(address(foldAdapter));
        vm.stopPrank();

        // Notes pool: a Public-Identity carrying contract.
        reg.bindContract(address(notes), BN254.g1(),
                         IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1()), true, true);

        // The payout recipient must be a registered (verified) identity for the
        // Buck transfer; for B1 it is the depositor account (already bound with
        // its identity leaf above).
        if (payout != depositor) {
            vm.etch(payout, hex"60006000fd");
            reg.bindContract(payout, BN254.g1(),
                             IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1()),
                             true, false);
        }

        // Mutual-decryptability fragments: issuer<->notes (mint escrow pull)
        // and payout<->notes (spend payout).  _receiptFragments is Buck slot 5.
        _storeFragment(issuer);
        _storeFragment(payout);

        // Fund the issuer with BUCK for the mint escrow.
        _grantCredit(issuer, 10 * face);
        vm.prank(issuer);
        buck.mint(2 * face);
        bytes32 slot = keccak256(
            abi.encode(address(notes), keccak256(abi.encode(issuer, uint256(2)))));
        vm.store(address(buck), slot, bytes32(2 * face));   // allowance
    }

    function _storeFragment(address party) internal {
        bytes32 fragSlot = keccak256(
            abi.encode(address(notes), keccak256(abi.encode(party, uint256(5)))));
        vm.store(address(buck), fragSlot, bytes32(uint256(1)));
    }

    function _grantCredit(address client, uint256 faceValue) internal {
        uint256 tokenId = credit.createCredit(
            client, 0, faceValue, faceValue,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(client);
        credit.forceActivate(tokenId, faceValue);
    }

    // ---- lifecycle steps ----------------------------------------------------

    function _mintArgs()
        internal view
        returns (bytes memory proof, uint256 oldRoot, uint256 newRoot,
                 uint32 nextLeafIndex, uint256 totalFace, uint256[] memory cms)
    {
        proof         = _b(".mint.proofBytes");
        oldRoot       = _u(".mint.public.oldRoot");
        newRoot       = _u(".mint.public.newRoot");
        nextLeafIndex = uint32(_u(".mint.public.nextLeafIndex"));
        totalFace     = _u(".mint.public.totalFace");
        uint256[] memory raw = vm.parseJsonUintArray(vj, ".mint.public.cm");
        cms = raw;
    }

    /// @dev Execute the flavor's real mint; returns gas used by the call.
    function _mint() internal virtual returns (uint256 gasUsed);

    /// @dev Execute the flavor's real coupled spend; returns gas used.
    function _spend() internal virtual returns (uint256 gasUsed);

    function _db() internal view returns (IdentityRegistry.DepositorBindingProof memory p) {
        p.e     = _u(".sigma.db.e");
        p.s_m   = _u(".sigma.db.s_m");
        p.s_s   = _u(".sigma.db.s_s");
        p.s_r   = _u(".sigma.db.s_r");
        p.s_b   = _u(".sigma.db.s_b");
        p.A2    = _g1(".sigma.db.A2");
        p.A4    = _g1(".sigma.db.A4");
        p.B1    = _g1(".sigma.db.B1");
        p.B2    = _g1(".sigma.db.B2");
        p.A_p   = _g1(".sigma.db.A_p");
        p.P_dep = _g1(".sigma.db.P_dep");
    }

    // ---- the E2E test ---------------------------------------------------------

    function test_e2e_lifecycle() public {
        uint256 escrowBefore = buck.balanceOf(address(notes));

        uint256 mintGas = _mint();

        assertEq(buck.balanceOf(address(notes)), escrowBefore + face,
                 "mint must escrow exactly face");
        assertEq(notes.roots(notes.currentRootIndex()), _u(".mint.public.newRoot"),
                 "mint must record the SNARK-attested newRoot");

        uint256 payoutBefore = buck.balanceOf(payout);
        uint256 spendGas = _spend();

        assertEq(buck.balanceOf(payout), payoutBefore + face,
                 "spend must pay the full face to the recipient");
        assertTrue(notes.nullifiers(_u(".opening.nullifier")),
                   "spend must burn the nullifier");

        console2.log(string.concat("[e2e:", _flavor(), "] mint  gas:"), mintGas);
        console2.log(string.concat("[e2e:", _flavor(), "] spend gas:"), spendGas);
    }

    function test_e2e_doubleSpend_reverts() public {
        _mint();
        _spend();
        vm.expectRevert(bytes("Notes: already spent"));
        _spend();
    }

    /// @dev An honest proof of this flavor must not redeem through any other
    ///      spendCoupled* entry point: the spend SNARK's public flavor is
    ///      the rejecting check (finding 7), even with nonempty membership.
    function test_e2e_wrongFlavorEntryPoint_reverts() public {
        _mint();
        bytes memory proof = _b(".spend.proofBytes");
        uint256 root = _u(".spend.public.noteRoot");
        uint256 nf   = _u(".spend.public.nullifier");
        // Whichever gate proof this world carries: B1 a membership proof,
        // the addressed flavours a folded one.  Either way the spend SNARK's
        // public flavor is what rejects a wrong entry point, so the gate proof
        // only has to be nonempty.
        bytes memory memProof = _isBearer()
            ? _b(".membership.proofBytes")
            : _b(".depositFold.proofBytes");
        IdentityRegistry.ElGamalCT memory zct;
        IdentityRegistry.DepositorBindingProof memory zdb;

        vm.startPrank(depositor);
        if (_flavorCode() != 3) {
            vm.expectRevert(bytes("Notes: bad spend proof"));
            notes.spendCoupledB1(
                proof, root, _u(".identityRoot"), nf, face, payout, _u(".opening.cm"), issuer,
                zct, zdb, memProof
            );
        }
        if (_flavorCode() != 1) {
            vm.expectRevert(bytes("Notes: bad spend proof"));
            notes.spendCoupledA1(proof, root, _u(".identityRoot"), nf, face, payout, zct, memProof);
        }
        if (_flavorCode() != 2) {
            vm.expectRevert(bytes("Notes: bad spend proof"));
            notes.spendCoupledA2(proof, root, _u(".identityRoot"), nf, face, payout, zct, memProof);
        }
        vm.stopPrank();
    }

    /// @dev Call-level gas of each verification phase in isolation -- the
    ///      numbers the docs' per-flavor cost table quotes.  All JSON parsing
    ///      is hoisted OUT of the gas windows (cheatcode calls cost gas).
    function test_e2e_phase_gas() public {
        _mint();   // tree + escrow state for context

        // ---- hoist every argument ----
        bytes memory spendProof = _b(".spend.proofBytes");
        uint256 noteRoot   = _u(".spend.public.noteRoot");
        uint256 nf         = _u(".spend.public.nullifier");
        uint256 spendFace  = _u(".spend.public.face");
        address spendRec   = _addr(".spend.public.recipient");
        uint256 idRoot     = _u(".identityRoot");

        // Note proof (shared by all flavors; public flavor matches the entry point).
        uint256 g0 = gasleft();
        bool okSpend = spendAdapter.verifySpend(
            spendProof, noteRoot, nf, spendFace, spendRec, 1, _flavorCode(),
            _isBearer() ? _u(".opening.cm") : 0);
        uint256 spendGas = g0 - gasleft();
        assertTrue(okSpend, "spend proof must verify");
        console2.log(string.concat("[gas:", _flavor(), "] spend proof:"), spendGas);

        // The deposit gate.  The shape differs by flavour, and the difference
        // is the architecture: the addressed flavours prove ONE folded
        // statement, because their two facts rest on two different secrets
        // and no sigma can tie those.  B1's rest on one, so its sigma is a
        // genuine tie and it pairs with a membership proof.
        if (_isBearer()) {
            bytes memory memProof = _b(".membership.proofBytes");
            uint256 px = _u(".sigma.db.P_dep.x");
            uint256 py = _u(".sigma.db.P_dep.y");
            uint256 g1 = gasleft();
            bool okMem = b1MemAdapter.verifyMembership(memProof, idRoot, px, py);
            uint256 memGas = g1 - gasleft();
            assertTrue(okMem, "B1 membership must verify");
            console2.log(string.concat("[gas:", _flavor(), "] membership:"), memGas);

            IdentityRegistry.ElGamalCT memory eDep = _ct(".sigma.eDepForIss");
            IdentityRegistry.DepositorBindingProof memory db = _db();
            uint256 g2 = gasleft();
            bool okSig = reg.verifyDepositorBinding(payout, issuer, eDep, db);
            uint256 sigGas = g2 - gasleft();
            assertTrue(okSig, "depositor binding must verify");
            console2.log(string.concat("[gas:", _flavor(), "] depositor binding sigma:"), sigGas);
        } else {
            bytes memory fold = _b(".depositFold.proofBytes");
            IdentityRegistry.ElGamalCT memory eEnc = _ct(".sigma.eEnc");
            bool isA1 = keccak256(bytes(_flavorPure())) == keccak256("a1");
            uint256 g1 = gasleft();
            bool okFold = isA1
                ? foldAdapter.verifyFoldA1(fold, nf, face, idRoot, eEnc, depositor)
                : foldAdapter.verifyFoldA2(fold, nf, idRoot, eEnc, depositor);
            uint256 foldGas = g1 - gasleft();
            assertTrue(okFold, "folded deposit gate must verify");
            console2.log(string.concat("[gas:", _flavor(), "] folded deposit gate:"), foldGas);
        }

        // No separate note-binding phase to profile: the fold absorbed it,
        // along with the membership proof and the coupling sigma.  That the
        // addressed flavours now have ONE gate line instead of three is the
        // architecture showing up in the gas table.
    }

    function _isBearer() internal pure returns (bool) {
        return keccak256(bytes(_flavorPure())) == keccak256("b1");
    }
    function _flavorPure() internal pure virtual returns (string memory);
    function _flavorCode() internal pure returns (uint256) {
        bytes32 h = keccak256(bytes(_flavorPure()));
        if (h == keccak256("a1")) return 1;
        if (h == keccak256("a2")) return 2;
        return 3;
    }
}

contract NotesE2E_B1 is NotesE2EBase {
    function _flavor() internal pure override returns (string memory) { return "b1"; }
    function _flavorPure() internal pure override returns (string memory) { return "b1"; }

    function _mint() internal override returns (uint256 gasUsed) {
        (bytes memory proof, uint256 oldRoot, uint256 newRoot,
         uint32 nli, uint256 totalFace, uint256[] memory cms) = _mintArgs();
        uint256[] memory mode = new uint256[](1);
        mode[0] = 1;                                       // MODE_PUBLIC
        IdentityRegistry.SchnorrProof memory sig = IdentityRegistry.SchnorrProof(
            _u(".issuerSchnorr.e"), _u(".issuerSchnorr.s"), _g1(".issuerSchnorr.R"));
        vm.prank(issuer);
        uint256 g = gasleft();
        notes.mint(proof, oldRoot, newRoot, nli, totalFace, cms, mode, sig);
        gasUsed = g - gasleft();
    }

    function _spend() internal override returns (uint256 gasUsed) {
        bytes memory proof = _b(".spend.proofBytes");
        uint256 root = _u(".spend.public.noteRoot");
        uint256 nf   = _u(".spend.public.nullifier");
        IdentityRegistry.ElGamalCT memory eDep = _ct(".sigma.eDepForIss");
        IdentityRegistry.DepositorBindingProof memory db = _db();
        bytes memory memProof = _b(".membership.proofBytes");
        vm.prank(depositor);
        uint256 g = gasleft();
        notes.spendCoupledB1(
            proof, root, _u(".identityRoot"), nf, face, payout, _u(".opening.cm"), issuer,
            eDep, db, memProof
        );
        gasUsed = g - gasleft();
    }
}

contract NotesE2E_A1 is NotesE2EBase {
    function _flavor() internal pure override returns (string memory) { return "a1"; }
    function _flavorPure() internal pure override returns (string memory) { return "a1"; }

    function _mint() internal override returns (uint256 gasUsed) {
        (bytes memory proof, uint256 oldRoot, uint256 newRoot,
         uint32 nli, uint256 totalFace, uint256[] memory cms) = _mintArgs();
        uint256[] memory mode = new uint256[](1);
        mode[0] = 1;                                       // MODE_PUBLIC
        IdentityRegistry.SchnorrProof memory sig = IdentityRegistry.SchnorrProof(
            _u(".issuerSchnorr.e"), _u(".issuerSchnorr.s"), _g1(".issuerSchnorr.R"));
        vm.prank(issuer);
        uint256 g = gasleft();
        notes.mint(proof, oldRoot, newRoot, nli, totalFace, cms, mode, sig);
        gasUsed = g - gasleft();
    }

    function _spend() internal override returns (uint256 gasUsed) {
        bytes memory proof = _b(".spend.proofBytes");
        uint256 root = _u(".spend.public.noteRoot");
        uint256 nf   = _u(".spend.public.nullifier");
        IdentityRegistry.ElGamalCT memory eEnc = _ct(".sigma.eEnc");
        // The folded gate: ONE proof.  The coupling sigma and the membership
        // argument are unused now -- the fold subsumed both -- so they go in
        // empty, and the entry point ignores them when a fold verifier is set.
        bytes memory fold = _b(".depositFold.proofBytes");
        vm.prank(depositor);
        uint256 g = gasleft();
        notes.spendCoupledA1(proof, root, _u(".identityRoot"), nf, face, payout, eEnc, fold);
        gasUsed = g - gasleft();
    }
}

contract NotesE2E_A2 is NotesE2EBase {
    function _flavor() internal pure override returns (string memory) { return "a2"; }
    function _flavorPure() internal pure override returns (string memory) { return "a2"; }

    function _mint() internal override returns (uint256 gasUsed) {
        (bytes memory proof, uint256 oldRoot, uint256 newRoot,
         uint32 nli, uint256 totalFace, uint256[] memory cms) = _mintArgs();
        uint256[] memory mode = new uint256[](1);
        mode[0] = 2;                                       // MODE_PRIVATE
        Notes.A2Binding[] memory binds = new Notes.A2Binding[](1);
        binds[0].eIss = _ct(".a2Binding.eIss");
        binds[0].proof = IdentityRegistry.IssuerReencProof(
            _u(".a2Binding.proof.e"),
            _u(".a2Binding.proof.s_r"), _u(".a2Binding.proof.s_b"),
            _u(".a2Binding.proof.s_s"), _u(".a2Binding.proof.s_g"),
            _g1(".a2Binding.proof.A1"), _g1(".a2Binding.proof.A2"),
            _g1(".a2Binding.proof.A3"), _g1(".a2Binding.proof.A4"),
            _g1(".a2Binding.proof.A5"),
            _g1(".a2Binding.proof.Q"), _g1(".a2Binding.proof.U"),
            _g1(".a2Binding.proof.T"));
        vm.prank(issuer);
        uint256 g = gasleft();
        notes.mint(proof, oldRoot, newRoot, nli, totalFace, cms, mode, binds);
        gasUsed = g - gasleft();
    }

    function _spend() internal override returns (uint256 gasUsed) {
        bytes memory proof = _b(".spend.proofBytes");
        uint256 root = _u(".spend.public.noteRoot");
        uint256 nf   = _u(".spend.public.nullifier");
        IdentityRegistry.ElGamalCT memory eEnc = _ct(".sigma.eEnc");
        bytes memory fold = _b(".depositFold.proofBytes");
        vm.prank(depositor);
        uint256 g = gasleft();
        notes.spendCoupledA2(proof, root, _u(".identityRoot"), nf, face, payout, eEnc, fold);
        gasUsed = g - gasleft();
    }
}
