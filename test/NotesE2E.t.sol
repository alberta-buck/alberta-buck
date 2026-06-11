// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test, console2} from "forge-std/Test.sol";

import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
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
import {IdentityMembershipG1TieVerifierAdapter} from "../src/IdentityMembershipG1TieVerifierAdapter.sol";
import {NoteBindingVerifierAdapter} from "../src/NoteBindingVerifierAdapter.sol";

/// @title NotesE2E -- the full Note lifecycle, every verifier REAL.
/// @notice One mutually-consistent fixture per flavor
///         (test/vectors/e2e/{a1,a2,b1}.json, from
///         scripts/snark/gen_e2e_fixtures.sh): real batch-mint Groth16, real
///         spend Groth16 against the replayed note tree, real deposit sigma
///         pinned to (depositor, chainid=1), real G1-tie membership proof
///         against the registry's incrementally-built identityRoot, and (A2)
///         the real note<->eEnc binding proof.  Confirms operation end to end
///         and measures the per-phase on-chain cost that the docs' cost
///         model quotes.
///
///         A1 BINDING GAP: the note-binding circuit opens
///         idHash = Poseidon8(eNote, eIss) -- the A2 payload layout.  A1's
///         idHash commits (eNote, m_issuer, sigma) instead, so no binding
///         proof is constructible for A1 notes today; the A1 spend passes an
///         empty proof (the documented backward-compat skip), meaning A1's
///         addressed-binding rests on the coupling sigma alone until an
///         A1-layout binding circuit variant lands.
abstract contract NotesE2EBase is Test {
    address internal constant GOV  = address(0x60);

    IdentityRegistry internal reg;
    BuckCreditHarness internal credit;
    Buck  internal buck;
    Notes internal notes;
    SpendVerifierAdapter internal spendAdapter;
    IdentityMembershipG1TieVerifierAdapter internal memAdapter;
    NoteBindingVerifierAdapter internal bindAdapter;

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
        vj = vm.readFile(string.concat("test/vectors/e2e/", _flavor(), ".json"));

        issuer    = _addr(".issuer");
        depositor = _addr(".depositor");
        payout    = _addr(".payout");
        face      = _u(".face");

        // ---- IdentityRegistry with the REAL incremental accumulator ----
        reg = new IdentityRegistry(GOV);
        vm.startPrank(GOV);
        reg.setIdentityPoseidon(PoseidonT3Bytecode.deploy());
        vm.stopPrank();

        // Bind the two world accounts WITH their identity leaves, in fixture
        // order; the on-chain incremental root must replay to the Python
        // tree's root (the membership proofs were generated against it).
        for (uint256 i = 0; i < 2; i++) {
            string memory k = string.concat(".binds[", vm.toString(i), "]");
            address a = _addr(string.concat(k, ".addr"));
            vm.etch(a, hex"60006000fd");
            reg.bindContract(
                a,
                _g1(string.concat(k, ".pk")),
                _ct(string.concat(k, ".E")),
                vm.parseJsonBool(vj, string.concat(k, ".isPublic")),
                false,
                _u(string.concat(k, ".identityLeaf"))
            );
        }
        assertEq(reg.identityRoot(), _u(".identityRoot"),
                 "on-chain incremental identityRoot must replay the fixture tree");

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
        memAdapter   = new IdentityMembershipG1TieVerifierAdapter();
        bindAdapter  = new NoteBindingVerifierAdapter();

        notes = new Notes(address(buck), address(mintAdapter), address(spendAdapter), GOV);
        vm.startPrank(GOV);
        notes.setIdentityRegistry(address(reg));
        notes.setA2MintVerifier(address(a2Adapter));
        notes.setIdentityMembershipVerifier(address(memAdapter));
        notes.setNoteBindingVerifier(address(bindAdapter));
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

    function _dc() internal view returns (IdentityRegistry.DepositCouplingProof memory p) {
        p.e   = _u(".sigma.dc.e");
        p.s_m = _u(".sigma.dc.s_m");
        p.s_s = _u(".sigma.dc.s_s");
        p.s_b = _u(".sigma.dc.s_b");
        p.A2  = _g1(".sigma.dc.A2");
        p.A3  = _g1(".sigma.dc.A3");
        p.A4  = _g1(".sigma.dc.A4");
        p.P_I = _g1(".sigma.dc.P_I");
    }

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
        bytes memory memProof = _b(".membership.proofBytes");
        uint256 idRoot     = _u(".identityRoot");
        string memory pkey = _isBearer() ? ".sigma.db.P_dep" : ".sigma.dc.P_I";
        uint256 px = _u(string.concat(pkey, ".x"));
        uint256 py = _u(string.concat(pkey, ".y"));

        // Note proof (shared by all flavors).
        uint256 g0 = gasleft();
        bool okSpend = spendAdapter.verifySpend(spendProof, noteRoot, nf, spendFace, spendRec, 1);
        uint256 spendGas = g0 - gasleft();
        assertTrue(okSpend, "spend proof must verify");
        console2.log(string.concat("[gas:", _flavor(), "] spend proof:"), spendGas);

        // Membership (G1-tie) of the committed point.
        uint256 g1 = gasleft();
        bool okMem = memAdapter.verifyMembership(memProof, idRoot, px, py);
        uint256 memGas = g1 - gasleft();
        assertTrue(okMem, "membership must verify");
        console2.log(string.concat("[gas:", _flavor(), "] membership:"), memGas);

        // The deposit sigma (verified against the registered payout/depositor).
        if (_isBearer()) {
            IdentityRegistry.ElGamalCT memory eDep = _ct(".sigma.eDepForIss");
            IdentityRegistry.DepositorBindingProof memory db = _db();
            uint256 g2 = gasleft();
            bool okSig = reg.verifyDepositorBinding(payout, issuer, eDep, db);
            uint256 sigGas = g2 - gasleft();
            assertTrue(okSig, "depositor binding must verify");
            console2.log(string.concat("[gas:", _flavor(), "] depositor binding sigma:"), sigGas);
        } else {
            IdentityRegistry.ElGamalCT memory eEnc = _ct(".sigma.eEnc");
            IdentityRegistry.DepositCouplingProof memory dc = _dc();
            uint256 g2 = gasleft();
            bool okSig = reg.verifyDepositCoupling(depositor, eEnc, dc);
            uint256 sigGas = g2 - gasleft();
            assertTrue(okSig, "deposit coupling must verify");
            console2.log(string.concat("[gas:", _flavor(), "] deposit coupling sigma:"), sigGas);
        }

        // The note<->eEnc binding (A2 only; A1 carries the documented skip).
        if (!_isBearer()) {
            bytes memory nb = _b(".noteBinding.proofBytes");
            if (nb.length > 0) {
                uint256 nfOpen = _u(".opening.nullifier");
                uint256 eRx = _u(".sigma.eEnc.R.x");
                uint256 eRy = _u(".sigma.eEnc.R.y");
                uint256 eCx = _u(".sigma.eEnc.C.x");
                uint256 eCy = _u(".sigma.eEnc.C.y");
                uint256 pix = _u(".sigma.dc.P_I.x");
                uint256 piy = _u(".sigma.dc.P_I.y");
                uint256 g3 = gasleft();
                bool okNb = bindAdapter.verifyNoteBinding(nb, nfOpen, eRx, eRy, eCx, eCy, pix, piy);
                uint256 nbGas = g3 - gasleft();
                assertTrue(okNb, "note binding must verify");
                console2.log(string.concat("[gas:", _flavor(), "] note binding:"), nbGas);
            }
        }
    }

    function _isBearer() internal pure returns (bool) {
        return keccak256(bytes(_flavorPure())) == keccak256("b1");
    }
    function _flavorPure() internal pure virtual returns (string memory);
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
        notes.spendCoupledB1(proof, root, nf, face, payout, issuer, eDep, db, memProof);
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
        IdentityRegistry.DepositCouplingProof memory dc = _dc();
        bytes memory memProof = _b(".membership.proofBytes");
        bytes memory nbProof = _b(".noteBinding.proofBytes");   // "0x": documented A1 gap
        vm.prank(depositor);
        uint256 g = gasleft();
        notes.spendCoupledA1(proof, root, nf, face, payout, eEnc, dc, memProof, nbProof);
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
        IdentityRegistry.DepositCouplingProof memory dc = _dc();
        bytes memory memProof = _b(".membership.proofBytes");
        bytes memory nbProof = _b(".noteBinding.proofBytes");
        vm.prank(depositor);
        uint256 g = gasleft();
        notes.spendCoupledA2(proof, root, nf, face, payout, eEnc, dc, memProof, nbProof);
        gasUsed = g - gasleft();
    }
}
