// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCash} from "../src/lib/BuckCash.sol";
import {BuckFacilities} from "../src/lib/BuckFacilities.sol";
import {BuckTransfer} from "../src/lib/BuckTransfer.sol";
import {BuckRenewal, BuckRenewalConsent} from "../src/BuckRenewalConsent.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckRenewalOps} from "../src/lib/BuckRenewalOps.sol";
import {BuckCompoundFixture, MockCompoundInsurerPool} from "./BuckCompound.t.sol";
import {Vm} from "forge-std/Vm.sol";

contract BuckCompoundOperationsTest is BuckCompoundFixture {
    address constant SERVICE = address(0x5E271CE);

    function test_coverageOnlyPreservesCashSupplyAndExistingClaimAge() public {
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE); buck.issue(id, 20e6, 20e6, 0, address(0));
        vm.warp(t0 + YEAR);
        uint256 cash = buck.netBalanceScaled(ALICE);
        uint256 seconds_ = buck.claimSeconds();
        vm.recordLogs();
        vm.prank(ALICE);
        (uint256 principal, uint256 premium) = buck.issue(id, 80e6, 0, 0, address(0));
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == address(buck) && logs[i].topics[0] == keccak256("Transfer(address,address,uint256)")) {
                assertTrue(logs[i].topics[1] != bytes32(0), "no currency mint on free backing activation");
            }
        }
        assertEq(principal, 0);
        assertEq(premium, 0);
        assertEq(buck.netBalanceScaled(ALICE), cash);
        assertEq(buck.totalIssued(), 20e6);
        assertEq(buck.claimSeconds(), seconds_);
        (uint256 issued, uint256 age) = credit.issuanceOf(id);
        assertEq(issued, 20e6);
        assertEq(age, 20e6 * YEAR);
        (, uint256 active,) = credit.creditInfo(id);
        assertEq(active, 100e6);
    }

    function test_coverageOnlyPremiumKeepsFundingAndPremiumBoundsAndMintsToItsPool() public {
        _issueFree(ALICE, INSURER_A, 49e6);
        MockCompoundInsurerPool pool = _pool(INSURER_A);
        uint256 id = _credit(ALICE, INSURER_A, 1000e6, 100);
        k.setFactor(0.5e18);
        vm.expectRevert(Buck.PremiumLimitExceeded.selector);
        vm.prank(ALICE); buck.issue(id, 1000e6, 0, 99e6, address(pool));
        vm.expectRevert(Buck.InsufficientMintFunding.selector);
        vm.prank(ALICE); buck.issue(id, 1000e6, 0, 100e6, address(pool));
        (, uint256 active,) = credit.creditInfo(id);
        assertEq(active, 0);
        assertEq(buck.totalIssued(), 49e6);
        _issueFree(ALICE, INSURER_A, 1e6);
        vm.prank(ALICE);
        (uint256 principal, uint256 premium) = buck.issue(id, 1000e6, 0, 100e6, address(pool));
        assertEq(principal, 100e6);
        assertEq(premium, 100e6);
        assertEq(buck.balanceOf(ALICE), 50e6);
        assertEq(buck.netBalanceScaled(address(pool)), 100e6 * Q);
        assertEq(buck.premiumPrincipal(id), 100e6);
        assertEq(buck.totalIssued(), 150e6);
        (uint256 issued, uint256 age) = credit.issuanceOf(id);
        assertEq(issued, 100e6);
        assertEq(age, 0);
    }

    function test_freeCoverageCanImproveALowKShortfallButCannotCreateNewCash() public {
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE); buck.issue(id, 20e6, 20e6, 0, address(0));
        k.setK(0.1e18);
        assertEq(credit.currentValue(id), 20e6);
        vm.prank(ALICE); buck.issue(id, 80e6, 0, 0, address(0));
        assertEq(credit.currentValue(id), 100e6);
        assertEq(buck.balanceOf(ALICE), 20e6);
        assertEq(buck.totalIssued(), 20e6);
        (uint256 issued,) = credit.issuanceOf(id);
        assertGt(issued, credit.currentValue(id) / 10);
        vm.expectRevert(Buck.CreditExceeded.selector);
        vm.prank(ALICE); buck.issue(id, 0, 1, 0, address(0));
    }

    function test_coverageOnlyRequiresOwnerAndDoesNotChangeNumericMintZero() public {
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.expectRevert(Buck.NotCreditOwner.selector);
        vm.prank(BOB); buck.issue(id, 100e6, 0, 0, address(0));
        vm.expectRevert(Buck.BadAmount.selector);
        vm.prank(ALICE); buck.issue(id, 0, 0, 0, address(0));
        uint256[] memory ids = new uint256[](1); ids[0] = id;
        vm.prank(ALICE); buck.mint(0, ids);
        (, uint256 active,) = credit.creditInfo(id);
        assertEq(active, 0);
        assertEq(buck.balanceOf(ALICE), 0);
        assertEq(buck.totalIssued(), 0);
    }

    function test_coverageOnlyCannotRefreshAFullyAgedLien() public {
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE); buck.issue(id, 20e6, 20e6, 0, address(0));
        vm.warp(t0 + 50 * YEAR);
        buck.processMaturities(1);
        vm.expectRevert(Buck.FacilityMatured.selector);
        vm.prank(ALICE); buck.issue(id, 80e6, 0, 0, address(0));
        (, uint256 active,) = credit.creditInfo(id);
        assertEq(active, 20e6);
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(id);
        assertEq(principal, 20e6);
        assertEq(seconds_, 20e6 * 50 * YEAR);
    }

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
        assertLe(address(BuckRenewalOps).code.length, 24_576);
    }

    function test_carryingRenewalPaysTheNewPremiumAtFullValue() public {
        _service();
        MockCompoundInsurerPool pool = _pool(INSURER_A);
        pool.approveToken(IERC20(address(buck)));
        uint256 id = _credit(SERVICE, INSURER_A, 2000e6, 100);
        vm.prank(SERVICE); buck.issue(id, 1000e6, 900e6, 100e6, address(pool));
        _issueFree(BOB, INSURER_B, 20000e6);
        vm.warp(t0 + YEAR);
        BuckRenewal.Terms memory terms = BuckRenewal.Terms({face:2500e6, floor:2500e6,
            activatedFace:2000e6, additionalIssuance:500e6, maxPremium:100e6,
            expectedPool:address(pool), minimumRelief:1e6, expectedPrincipal:1000e6,
            expectedCreditHash:buck.renewalConsent().currentCreditTermsHash(id),
            depType:BuckCredit.DepreciationType.NONE, depRate:0, depStartAt:0,
            premiumRate:50, deadline:uint64(block.timestamp + 1 days)});
        bytes32 digest = buck.renewalConsent().renewalDigest(SERVICE, id, terms);
        BuckRenewalConsent consent = buck.renewalConsent();
        vm.prank(INSURER_A); consent.propose(id, digest);
        vm.prank(SERVICE); consent.authorize(id, digest);
        vm.prank(address(fund));
        (uint256 refund, uint256 premium) = buck.renewFromJubilee(SERVICE, id, terms, 1e6);
        assertEq(refund, 100e6);
        assertEq(premium, 100e6);
        assertEq(buck.netBalanceScaled(address(pool)), 100e6 * Q);
        assertEq(buck.balanceOf(address(pool)), 100e6);
        (uint256 principal, uint256 age) = credit.issuanceOf(id);
        assertEq(principal, 1499e6);
        assertEq(age, 0);
        assertEq(buck.totalIssued(), 21499e6);
    }
}
