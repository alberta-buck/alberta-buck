// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckCreditHarness} from "./harness/BuckCreditHarness.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";

/// @notice Scratch probes for the CREDIT-IMPROVEMENT proposal.
contract CreditProbeTest is Test {
    Buck internal buck;
    BuckCreditHarness internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry internal reg;

    address internal constant GOV = address(0xA0);
    address internal constant POOL = address(0xBA51C);
    address internal constant INSURER = address(0x1551E1);
    address internal alice = address(0xA11CE);
    address internal bob = address(0xB0B);

    function setUp() public {
        reg = new IdentityRegistry(GOV);
        credit = new BuckCreditHarness();
        kCtrl = new BuckKControllerStatic(1e18, GOV);
        buck = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));
        _bind(alice, false);
        _bind(bob, true);
    }

    function _bind(address t, bool carrying) internal {
        vm.etch(t, hex"60006000fd");
        reg.bindContract(t, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, carrying);
    }

    /// @dev A real purchase through mint(): pays the pool principal, sets
    ///      mintsBacked and activatedValue in lockstep.
    function _mintAgainstNewCredit(uint256 face, uint32 premiumBp, uint256 amount)
        internal returns (uint256 tid)
    {
        vm.prank(INSURER);
        tid = credit.createCredit(
            alice, 0, face, 0, BuckCredit.DepreciationType.NONE, 0, 0, premiumBp
        );
        uint256[] memory ids = new uint256[](1);
        ids[0] = tid;
        vm.prank(alice);
        buck.mint(amount, ids);
    }

    /// @notice Does a downward reappraisal strand the holder's burn?
    function test_probe_reappraisalStrandsBurn() public {
        uint256 tid = _mintAgainstNewCredit(2_000e6, 100, 900e6);

        (uint256 face, uint256 activated,) = credit.creditInfo(tid);
        emit log_named_uint("after mint: faceValue     ", face);
        emit log_named_uint("after mint: activatedValue", activated);
        emit log_named_uint("after mint: mintsBacked   ", buck.mintsBacked(tid));
        emit log_named_int ("after mint: signedRaw     ", buck.signedRawBalanceOf(alice));
        emit log_named_uint("after mint: creditLimit   ", buck.creditLimit(alice));

        // Insurer reappraises the asset downward, below the activated coverage.
        vm.prank(INSURER);
        credit.updateCredit(
            tid, 500e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 100
        );

        (face, activated,) = credit.creditInfo(tid);
        emit log_named_uint("after reappraisal: faceValue     ", face);
        emit log_named_uint("after reappraisal: activatedValue", activated);
        emit log_named_uint("after reappraisal: mintsBacked   ", buck.mintsBacked(tid));
        emit log_named_uint("after reappraisal: creditLimit   ", buck.creditLimit(alice));

        // Alice tries to close her position.
        uint256[] memory ids = new uint256[](1);
        ids[0] = tid;
        vm.prank(alice);
        try buck.burn(100e6, ids) {
            emit log_string("small burn (100) SUCCEEDED");
        } catch Error(string memory reason) {
            emit log_named_string("small burn (100) REVERTED", reason);
        }

        // Closing the whole position needs an unwind larger than the
        // clamped activatedValue.
        vm.prank(alice);
        try buck.burn(800e6, ids) {
            emit log_string("full burn (800) SUCCEEDED");
        } catch Error(string memory reason) {
            emit log_named_string("full burn (800) REVERTED", reason);
        }

        // Burn down in chunks for as long as the clamped coverage allows,
        // then look at what is left stranded.
        uint256 burned;
        for (uint256 i = 0; i < 60; i++) {
            vm.prank(alice);
            try buck.burn(10e6, ids) { burned += 10e6; } catch { break; }
        }
        (, activated,) = credit.creditInfo(tid);
        emit log_named_uint("burned down by            ", burned);
        emit log_named_uint("residual activatedValue   ", activated);
        emit log_named_uint("residual mintsBacked      ", buck.mintsBacked(tid));
        emit log_named_uint("residual creditLimit      ", buck.creditLimit(alice));
    }

    /// @notice The same reappraisal, but after the holder has spent the BUCK:
    ///         used credit now exceeds the shrunken limit and the position
    ///         cannot be unwound.
    function test_probe_reappraisalStrandsSpentPosition() public {
        uint256 tid = _mintAgainstNewCredit(2_000e6, 100, 900e6);

        vm.prank(alice);
        buck.transfer(bob, 900e6);          // spend it: raw -> -1000
        emit log_named_int ("after spend: signedRaw  ", buck.signedRawBalanceOf(alice));
        emit log_named_uint("after spend: creditLimit", buck.creditLimit(alice));

        vm.prank(INSURER);
        credit.updateCredit(
            tid, 500e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 100
        );
        emit log_named_int ("after reappraisal: signedRaw  ", buck.signedRawBalanceOf(alice));
        emit log_named_uint("after reappraisal: creditLimit", buck.creditLimit(alice));
        emit log_named_uint("after reappraisal: balanceOf  ", buck.balanceOf(alice));

        uint256[] memory ids = new uint256[](1);
        ids[0] = tid;
        vm.prank(alice);
        try buck.burn(100e6, ids) {
            emit log_string("burn SUCCEEDED");
        } catch Error(string memory reason) {
            emit log_named_string("burn REVERTED", reason);
        }
    }

    /// @notice With no reappraisal at all: can a holder who has spent their
    ///         whole draw ever burn?  The post-burn solvency check compares
    ///         `used` against a limit that shrinks faster than `used` does.
    function test_probe_fullyDrawnHolderCannotBurn() public {
        uint256 tid = _mintAgainstNewCredit(2_000e6, 100, 900e6);
        uint256[] memory ids = new uint256[](1);
        ids[0] = tid;

        // Fresh from the mint, with headroom left, a burn works.
        vm.prank(alice);
        try buck.burn(10e6, ids) {
            emit log_string("burn with headroom: SUCCEEDED");
        } catch Error(string memory reason) {
            emit log_named_string("burn with headroom: REVERTED", reason);
        }

        // Spend everything: used == limit.
        uint256 all = buck.balanceOf(alice);
        vm.prank(alice);
        buck.transfer(bob, all);
        emit log_named_int ("fully drawn: signedRaw  ", buck.signedRawBalanceOf(alice));
        emit log_named_uint("fully drawn: creditLimit", buck.creditLimit(alice));

        vm.prank(alice);
        try buck.burn(1e6, ids) {
            emit log_string("burn when fully drawn: SUCCEEDED");
        } catch Error(string memory reason) {
            emit log_named_string("burn when fully drawn: REVERTED", reason);
        }
    }

    /// @notice Is the per-block credit-limit cache sound if it were wired?
    ///         buckK can change mid-block via any mint's compute() call, and
    ///         nothing invalidates the credit cache when it does.
    function test_probe_cacheWouldGoStaleOnBuckKChange() public {
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            alice, 0, 1_000e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(alice);
        credit.forceActivate(tid, 1_000e6);

        uint256 before = buck.creditLimit(alice);

        // Same block: BUCK_K moves.  In production this happens inside any
        // mint/burn via IBuckK.compute(), which fires no cache invalidation.
        vm.prank(GOV);
        kCtrl.setBuckK(5e17);

        uint256 afterK = buck.creditLimit(alice);
        emit log_named_uint("creditLimit before K change", before);
        emit log_named_uint("creditLimit after  K change", afterK);
        emit log_named_uint("same block?                ", block.number);
        emit log_named_uint("cache block stamp          ", buck.creditLimitBlock(alice));
    }
}
