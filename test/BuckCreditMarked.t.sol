// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {BN254}                   from "../src/BN254.sol";
import {IdentityRegistry}        from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {Buck}                    from "../src/Buck.sol";
import {BuckCredit}              from "../src/BuckCredit.sol";
import {BuckKControllerStatic}   from "../src/BuckKControllerStatic.sol";
import {bindCarryingPool}        from "./harness/CarryingPool.sol";

/// @title BuckCreditMarked.t.sol -- MARKED credits: valued at their insurer's
///        mark, self-issued only, MARKED from creation or never.
///
/// @notice The collateral a marked credit stands for is revalued on-chain
///         every block (a BuckBasket's equity), so it carries a mark in place
///         of a depreciation schedule.  The test contract is the holder and
///         its own insurer, as a basket is: bound public and non-Carrying, it
///         activates its credit with an ordinary Buck.mint and draws on it.
contract BuckCreditMarkedTest is Test {

    Buck                  internal buck;
    BuckCredit            internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;

    address internal constant GOV   = address(0xA0);
    address internal constant POOL  = address(0xBA51C);    // insurance pool; a Carrying sink
    address internal constant ALICE = address(0xA11CE);

    uint256 internal constant FACE = 1e23;                  // the issuance ceiling
    uint256 internal tid;

    function setUp() public {
        reg    = new IdentityRegistryHarness(GOV);
        credit = new BuckCredit();
        kCtrl  = new BuckKControllerStatic(0.75e18, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        bindCarryingPool(reg, POOL);
        reg.bindContract(address(this), BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, false);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));

        credit.setCreditIssuer(address(this), true);        // self-issuance opts in too
        tid = credit.createCredit(address(this), 0, FACE, 0,
                                  BuckCredit.DepreciationType.MARKED, 0, 0, 0);
    }

    function _ids() internal view returns (uint256[] memory ids) {
        ids = new uint256[](1);
        ids[0] = tid;
    }

    /// @dev Mark, then activate the whole face once: the limit is K x mark.
    function _open(uint256 m) internal {
        credit.mark(tid, m);
        buck.mint(m, _ids());
    }

    function test_marked_valueFollowsTheMark() public {
        _open(1_000e6);
        (, uint256 active,) = credit.creditInfo(tid);
        assertEq(active, FACE, "one mint activates the whole face");
        assertEq(buck.creditLimit(address(this)), 750e6, "K x the mark");

        credit.mark(tid, 2_000e6);
        assertEq(buck.creditLimit(address(this)), 1_500e6, "rises with the mark");
        credit.mark(tid, 400e6);
        assertEq(buck.creditLimit(address(this)), 300e6,
                 "and falls with it, below the coverage activated");
        (, active,) = credit.creditInfo(tid);
        assertEq(active, FACE, "the coverage is untouched");
        credit.mark(tid, FACE * 2);
        assertEq(credit.depreciatedFaceValue(tid), FACE, "capped by the face");
    }

    function test_marked_underWaterStopsIssuingAndCallsNothing() public {
        _open(1_000e6);
        buck.transfer(POOL, 750e6);                         // draw to the limit
        assertEq(buck.signedRawBalanceOf(address(this)), -750e6);
        credit.mark(tid, 800e6);                            // equity falls
        assertEq(buck.balanceOf(address(this)), 0, "no headroom: under water");
        vm.expectRevert(bytes("BUCK: amount exceeds spendable"));
        buck.transfer(POOL, 1);
        assertEq(buck.signedRawBalanceOf(address(this)), -750e6, "no margin call: the lien stands");
    }

    function test_marked_cannotActivateBeforeItIsMarked() public {
        vm.expectRevert(bytes("BUCK: insufficient credit allocation"));
        buck.mint(1e6, _ids());
    }

    function test_marked_isSelfIssuedOnly() public {
        vm.prank(ALICE);
        credit.setCreditIssuer(address(this), true);
        vm.expectRevert(bytes("BuckCredit: a marked credit is self-issued"));
        credit.createCredit(ALICE, 0, FACE, 0, BuckCredit.DepreciationType.MARKED, 0, 0, 0);
    }

    function test_marked_onlyItsInsurerMarks() public {
        vm.prank(ALICE);
        vm.expectRevert(bytes("Not insurer"));
        credit.mark(tid, 1);

        vm.prank(ALICE);
        credit.setCreditIssuer(address(this), true);
        uint256 plain = credit.createCredit(ALICE, 0, FACE, 0,
                                            BuckCredit.DepreciationType.NONE, 0, 0, 0);
        vm.expectRevert(bytes("BuckCredit: not marked"));
        credit.mark(plain, 1);
    }

    function test_marked_isFixedAtCreation() public {
        vm.expectRevert(bytes("BuckCredit: MARKED is fixed at creation"));
        credit.updateCredit(tid, FACE, 0, BuckCredit.DepreciationType.NONE, 0, 0, 0);

        vm.prank(ALICE);
        credit.setCreditIssuer(address(this), true);
        uint256 plain = credit.createCredit(ALICE, 0, FACE, 0,
                                            BuckCredit.DepreciationType.NONE, 0, 0, 0);
        vm.expectRevert(bytes("BuckCredit: MARKED is fixed at creation"));
        credit.updateCredit(plain, FACE, 0, BuckCredit.DepreciationType.MARKED, 0, 0, 0);
    }

    function test_marked_liensEarnReliefLikeAnyOther() public {
        _open(1_000e6);
        buck.transfer(POOL, 500e6);
        vm.warp(block.timestamp + 365 days + 6 hours);
        assertApproxEqRel(buck.reliefOf(address(this)), 10e6, 0.001e18, "2% of the 500 issued");
    }
}
