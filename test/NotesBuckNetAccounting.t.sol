// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckNet} from "../src/BuckNet.sol";
import {JubileeFund, IJubileeBasket} from "../src/JubileeFund.sol";
import {Notes} from "../src/Notes.sol";
import {StubMintVerifier} from "../src/StubMintVerifier.sol";
import {StubSpendVerifier} from "../src/StubSpendVerifier.sol";
import {IDepositFoldVerifier} from "../src/IDepositFoldVerifier.sol";
import {GatedMint} from "./helpers/GatedMint.sol";
import {NetK} from "./BuckNet.t.sol";
import {JubileeBasketMock} from "./JubileeFund.t.sol";

/// @dev Test-only gate: this exercise establishes accounting behavior, not
///      the soundness of the real mint/spend/deposit proofs.
contract NotesBuckNetAcceptFold is IDepositFoldVerifier {
    function verifyFoldA1(bytes calldata, uint256, uint256, uint256,
        IdentityRegistry.ElGamalCT calldata, address) external pure returns (bool) { return true; }
    function verifyFoldA2(bytes calldata, uint256, uint256,
        IdentityRegistry.ElGamalCT calldata, address) external pure returns (bool) { return true; }
}

contract NotesBuckNetAccountingTest is Test {
    uint256 constant YEAR = 365 days + 6 hours;
    uint256 constant SK = 0x12345;
    uint256 constant NONCE = 0x23456;
    address constant GOV = address(0xA0);
    address constant ALICE = address(0xA11CE);
    address constant BOB = address(0xB0B);
    address constant INSURER = address(0x1551E1);

    IdentityRegistryHarness identity;
    BuckCredit credit;
    BuckNet buck;
    JubileeFund fund;
    Notes notes;
    uint256 t0;

    function setUp() public {
        vm.warp(1_000_000);
        t0 = block.timestamp;
        identity = new IdentityRegistryHarness(GOV);
        credit = new BuckCredit();
        fund = new JubileeFund(GOV);
        buck = new BuckNet(address(credit), address(new NetK()), address(identity), address(fund));
        JubileeBasketMock basket = new JubileeBasketMock(address(buck));
        notes = new Notes(address(buck), address(new StubMintVerifier(GOV)), address(new StubSpendVerifier(GOV)), GOV);
        credit.setBuck(address(buck));
        vm.startPrank(GOV);
        identity.setBuck(address(buck));
        notes.setIdentityRegistry(address(identity));
        notes.setDepositFoldVerifier(address(new NotesBuckNetAcceptFold()));
        vm.stopPrank();
        _bind(ALICE, BN254.mul(BN254.g1(), SK), false);
        _bind(BOB, BN254.g1(), false);
        _bind(INSURER, BN254.g1(), false);
        _bind(address(fund), BN254.g1(), true);
        _bind(address(basket), BN254.g1(), false);
        _bind(address(notes), BN254.g1(), true);
        vm.prank(GOV);
        fund.configure(buck, IJubileeBasket(address(basket)));
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER, true);
        vm.prank(INSURER);
        uint256 id = credit.createCredit(ALICE, 0, 100e6, 100e6, BuckCredit.DepreciationType.NONE, 0, 0, 0);
        vm.prank(ALICE);
        buck.issue(id, 100e6, 100e6);
    }

    function _bind(address target, BN254.G1Point memory pk, bool carrying) private {
        if (target.code.length == 0) vm.etch(target, hex"60006000fd");
        identity.bindContract(target, pk,
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, carrying);
    }

    function _mintNote(uint256 commitment, uint256 root, uint32 index, uint256 face) private {
        uint256[] memory commitments = new uint256[](1);
        commitments[0] = commitment;
        uint256[] memory modes = GatedMint.allPublic(1);
        IdentityRegistry.SchnorrProof memory sig = GatedMint.signPublic(SK, NONCE + index, commitments, ALICE, block.chainid);
        vm.startPrank(ALICE);
        buck.approve(address(notes), face);
        notes.mint(hex"01", notes.noteRoot(), root, index, face, commitments, modes, sig);
        vm.stopPrank();
    }

    function test_agedNoteFaceAndTransferEventAreNominalButReceiverGetsNet() public {
        _mintNote(1234, 123, 0, 100e6);
        assertEq(notes.noteFaceSum(), 100e6);
        vm.warp(t0 + 10 * YEAR);
        identity.fixturePostRoot(456);
        assertEq(buck.balanceOf(address(notes)), 100e6);
        uint256 netBefore = buck.netBalanceOf(address(notes));
        assertApproxEqAbs(netBefore, 81_873_075, 1);
        (, uint256 fundBefore) = buck.fundBalancesScaled();

        vm.recordLogs();
        vm.expectEmit(true, true, true, true, address(notes));
        emit Notes.SpentCoupledA1(42, 100e6, BOB);
        vm.prank(BOB);
        notes.spendCoupledA1(hex"01", 123, 456, 42, 100e6, BOB,
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), hex"01");
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bool found;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter != address(buck) || logs[i].topics.length != 3
                || logs[i].topics[0] != keccak256("Transfer(address,address,uint256)")) continue;
            if (address(uint160(uint256(logs[i].topics[1]))) != address(notes)
                || address(uint160(uint256(logs[i].topics[2]))) != BOB) continue;
            assertEq(abi.decode(logs[i].data, (uint256)), 100e6);
            found = true;
        }
        assertTrue(found);
        assertTrue(notes.nullifiers(42));
        assertEq(notes.noteFaceSum(), 0);
        assertEq(buck.balanceOf(address(notes)), 0);
        assertEq(buck.balanceOf(BOB), netBefore);
        assertLt(buck.balanceOf(BOB), 100e6);
        (, uint256 fundAfter) = buck.fundBalancesScaled();
        assertEq(fundAfter, fundBefore); // The carried fee was already credited.
        assertEq(buck.totalSupply(), 100e6);
    }

    function test_mixedAgeNotesShareThePoolsAverageCarriedFee() public {
        _mintNote(1234, 123, 0, 100e6);
        vm.warp(t0 + 10 * YEAR);
        uint256 oldNet = buck.netBalanceOf(address(notes));
        vm.prank(INSURER);
        uint256 id = credit.createCredit(ALICE, 0, 100e6, 100e6, BuckCredit.DepreciationType.NONE, 0, 0, 0);
        vm.prank(ALICE);
        buck.issue(id, 100e6, 100e6);
        _mintNote(1235, 124, 1, 100e6);
        identity.fixturePostRoot(456);
        assertEq(notes.noteFaceSum(), 200e6);
        assertEq(buck.balanceOf(address(notes)), 200e6);
        assertApproxEqAbs(buck.netBalanceOf(address(notes)), oldNet + 100e6, 1);
        vm.prank(BOB);
        notes.spendCoupledA1(hex"01", 124, 456, 42, 100e6, BOB,
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), hex"01");
        uint256 paid = buck.balanceOf(BOB);
        assertApproxEqAbs(paid, (oldNet + 100e6) / 2, 1);
        assertGt(paid, oldNet);
        assertLt(paid, 100e6);
        assertEq(notes.noteFaceSum(), 100e6);
        assertApproxEqAbs(buck.netBalanceOf(address(notes)), paid, 1);
        assertEq(buck.totalSupply(), 200e6);
    }
}
