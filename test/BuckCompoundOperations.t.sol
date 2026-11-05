// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCash} from "../src/lib/BuckCash.sol";
import {BuckFacilities} from "../src/lib/BuckFacilities.sol";
import {BuckTransfer} from "../src/lib/BuckTransfer.sol";
import {BuckCompoundFixture, MockCompoundInsurerPool} from "./BuckCompound.t.sol";
import {Vm} from "forge-std/Vm.sol";

contract BuckCompoundOperationsTest is BuckCompoundFixture {
    address constant SERVICE = address(0x5E271CE);

    function _issueFree(address owner, address insurer, uint256 amount) private returns (uint256 id) {
        id = _credit(owner, insurer, amount, 0);
        vm.prank(owner);
        buck.mint(amount);
    }

    function _sponsor() private {
        vm.prank(ALICE); buck.requestDemurragePayer(BOB);
        vm.prank(BOB); buck.acceptDemurragePayer(ALICE);
    }

    function _service() private {
        _bind(SERVICE, true, address(this));
        vm.prank(SERVICE); credit.setCreditIssuer(INSURER_A, true);
        vm.prank(GOV); buck.setApprovedCarryingIssuer(SERVICE, true);
    }

    function test_carryingIssuancePaysFreshPremiumWithoutInheritedFees() public {
        _service();
        MockCompoundInsurerPool pool = _pool(INSURER_A);
        uint256 id = _credit(SERVICE, INSURER_A, 2000e6, 100);
        vm.prank(SERVICE); buck.issue(id, 1000e6, 900e6, 100e6, address(pool));
        vm.warp(t0 + YEAR);
        uint256 beforeOwner = buck.balanceOf(SERVICE);
        uint256 beforePoolNet = buck.netBalanceScaled(address(pool));
        vm.prank(SERVICE); buck.issue(id, 1000e6, 900e6, 100e6, address(pool));
        assertEq(buck.balanceOf(SERVICE) - beforeOwner, 900e6);
        assertEq(buck.netBalanceScaled(address(pool)) - beforePoolNet, 100e6 * Q);
        assertEq(buck.totalIssued(), 2000e6);
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(id);
        assertEq(principal, 2000e6);
        assertEq(seconds_, 1000e6 * YEAR);
    }

    function test_revocationStopsNewCarryingIssuanceButAllowsNominalBurn() public {
        _service();
        uint256 id = _issueFree(SERVICE, INSURER_A, 100e6);
        _issueFree(BOB, INSURER_B, 1000e6);
        vm.warp(t0 + YEAR);
        vm.prank(GOV); buck.setApprovedCarryingIssuer(SERVICE, false);
        vm.expectRevert(Buck.CarryingIssuerNotApproved.selector);
        vm.prank(SERVICE); buck.mint(1);
        uint256[] memory ids = new uint256[](1); ids[0] = id;
        vm.prank(SERVICE); (uint256 quoted,) = buck.quoteBurn(10e6, ids);
        uint256 beforeBalance = buck.balanceOf(SERVICE);
        vm.prank(SERVICE); buck.burn(10e6, ids);
        assertGe(beforeBalance - buck.balanceOf(SERVICE), 10e6);
        assertEq(100e6 - buck.mintsBacked(id), quoted);
        assertEq(buck.totalIssued(), 1000e6 + buck.mintsBacked(id));
    }

    function test_carriedFeesCannotMintUnbackedCreditOnTransfer() public {
        _service();
        uint256 id = _issueFree(SERVICE, INSURER_A, 100e6);
        vm.warp(t0 + YEAR);
        uint256 supply = buck.totalIssued();
        vm.prank(SERVICE); buck.transfer(ALICE, 100e6);
        assertApproxEqAbs(buck.balanceOf(ALICE), 98_019_867, 1);
        assertEq(buck.balanceOf(SERVICE), 0);
        assertEq(buck.totalIssued(), supply);
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(id);
        assertEq(principal, supply);
        assertEq(seconds_, supply * YEAR);
        vm.expectRevert(Buck.InsufficientNominalBalance.selector);
        vm.prank(SERVICE); buck.transfer(ALICE, 1);
    }

    function test_underfundedSponsorSpendRollsBackThenManualDetachRecovers() public {
        _issueFree(ALICE, INSURER_A, 100e6);
        _sponsor();
        vm.warp(t0 + YEAR);
        uint256 net = buck.netBalanceScaled(ALICE);
        (, uint256 fundBefore) = buck.fundBalancesScaled();
        assertEq(buck.balanceOf(ALICE), 100e6);
        vm.expectPartialRevert(Buck.DemurrageSponsorUnderfunded.selector);
        vm.prank(ALICE); buck.transfer(BOB, 1e6);
        assertEq(buck.netBalanceScaled(ALICE), net);
        assertEq(buck.demurragePayer(ALICE), BOB);
        (, uint256 fundAfter) = buck.fundBalancesScaled();
        assertEq(fundAfter, fundBefore);
        vm.prank(ALICE); buck.clearDemurragePayer(ALICE);
        assertEq(buck.demurragePayer(ALICE), address(0));
        assertEq(buck.balanceOf(ALICE), net / Q);
        vm.prank(ALICE); buck.transfer(BOB, 1e6);
        assertEq(buck.totalIssued(), 100e6);
    }

    function test_sponsoredBurnQuoteIncludesFullNominalInsurerRefund() public {
        MockCompoundInsurerPool pool = _pool(INSURER_A);
        pool.approveToken(IERC20(address(buck)));
        uint256 id = _credit(ALICE, INSURER_A, 1000e6, 100);
        vm.prank(ALICE); buck.mint(900e6);
        _issueFree(BOB, INSURER_B, 2000e6);
        _sponsor();
        vm.warp(t0 + YEAR);
        uint256[] memory ids = new uint256[](1); ids[0] = id;
        vm.prank(ALICE); (uint256 quotedCoverage, uint256 quotedRefund) = buck.quoteBurn(100e6, ids);
        uint256 beforeBalance = buck.balanceOf(ALICE);
        uint256 beforeSponsor = buck.netBalanceScaled(BOB);
        uint256 beforePool = buck.balanceOf(address(pool));
        uint256 beforeCoverage = buck.mintsBacked(id);
        vm.prank(ALICE); buck.burn(100e6, ids);
        assertGe(beforeBalance - buck.balanceOf(ALICE), 100e6);
        assertEq(beforeCoverage - buck.mintsBacked(id), quotedCoverage);
        assertEq(beforePool - buck.balanceOf(address(pool)), quotedRefund);
        assertLt(buck.netBalanceScaled(BOB), beforeSponsor);
        assertTrue(buck.isSponsored(ALICE));
        assertEq(buck.balanceOf(ALICE), buck.netBalanceScaled(ALICE) / Q);
    }

    function test_batchQuoteConsumesSharedInsurerAllowanceAndMatchesExecution() public {
        MockCompoundInsurerPool pool = _pool(INSURER_A);
        uint256 first = _credit(ALICE, INSURER_A, 100e6, 200);
        uint256 second = _credit(ALICE, INSURER_A, 100e6, 100);
        vm.prank(ALICE); buck.mint(170e6);
        uint256 firstCoverage = buck.mintsBacked(first);
        uint256 secondCoverage = buck.mintsBacked(second);
        uint256 poolBefore = buck.balanceOf(address(pool));
        uint256[] memory ids = new uint256[](2); ids[0] = first; ids[1] = second;
        vm.prank(address(pool)); buck.approve(address(buck), 22e6);
        vm.expectRevert(Buck.InsufficientCoverage.selector);
        vm.prank(ALICE); buck.quoteBurn(120e6, ids);
        vm.expectRevert(Buck.InsufficientCoverage.selector);
        vm.prank(ALICE); buck.burn(120e6, ids);
        assertEq(buck.mintsBacked(first), firstCoverage);
        assertEq(buck.mintsBacked(second), secondCoverage);
        assertEq(buck.allowance(address(pool), address(buck)), 22e6);
        pool.approveToken(IERC20(address(buck)));
        vm.prank(ALICE); (uint256 coverage, uint256 refund) = buck.quoteBurn(120e6, ids);
        vm.prank(ALICE); buck.burn(120e6, ids);
        assertEq(firstCoverage + secondCoverage - buck.mintsBacked(first) - buck.mintsBacked(second), coverage);
        assertEq(poolBefore - buck.balanceOf(address(pool)), refund);
        assertLe(buck.balanceOf(ALICE), 50e6);
    }

    function test_duplicateMintSelectionAndUnconfiguredFundRevertBeforeIssuance() public {
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        uint256[] memory ids = new uint256[](2); ids[0] = id; ids[1] = id;
        vm.expectRevert(Buck.BadAmount.selector);
        vm.prank(ALICE); buck.mint(10e6, ids);
        assertEq(buck.totalIssued(), 0);
        fund.configureToken(address(0));
        vm.expectRevert(Buck.InvalidConfiguration.selector);
        vm.prank(ALICE); buck.mint(10e6);
        assertEq(buck.mintsBacked(id), 0);
    }

    function test_everyLinkedRuntimeFitsTheEvmLimit() public view {
        assertLe(address(BuckCash).code.length, 24_576);
        assertLe(address(BuckFacilities).code.length, 24_576);
        assertLe(address(BuckTransfer).code.length, 24_576);
    }
}
