// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Vm} from "forge-std/Vm.sol";
import {BuckNet} from "../src/BuckNet.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {BuckNetTest, MockNetInsurerPool, BuckLockedValuation} from "./BuckNet.t.sol";
import {TestWheel} from "./wheel/WorkWheel.t.sol";

/// @dev The legacy-shaped API now issues actual BUCK; unissued headroom is
///      separate. These tests cover ordinary owners only until approved
///      Carrying facilities have their own full-value premium path.
contract BuckNetMintCompatibilityTest is BuckNetTest {
    function test_mintIncreasesActualBalanceAndEmitsLegacyMintedFields() public {
        uint256 id = _credit(ALICE, INSURER_A, 1000e6, 0);
        vm.recordLogs();
        vm.prank(ALICE);
        buck.mint(100e6);
        assertEq(buck.balanceOf(ALICE), 100e6);
        assertEq(buck.totalIssued(), 100e6);
        assertEq(buck.mintsBacked(id), 100e6);
        assertEq(buck.creditLimit(ALICE), 100e6);
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(id);
        assertEq(principal, 100e6);
        assertEq(seconds_, 0);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bytes32 signature = keccak256("Minted(address,uint256,uint256,uint256,uint256,uint256)");
        bool found;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter != address(buck) || logs[i].topics[0] != signature) continue;
            assertFalse(found);
            assertEq(address(uint160(uint256(logs[i].topics[1]))), ALICE);
            (uint256 coverage, uint256 premium, uint256 beforeValue, uint256 factor, uint256 limit) =
                abi.decode(logs[i].data, (uint256, uint256, uint256, uint256, uint256));
            assertEq(coverage, 100e6);
            assertEq(premium, 0);
            assertEq(beforeValue, 0);
            assertEq(factor, 1e18);
            assertEq(limit, 100e6);
            found = true;
        }
        assertTrue(found);
    }

    function test_mintFundsPremiumAndDeliversTheRequestedNetAmount() public {
        MockNetInsurerPool pool = _pool(INSURER_A);
        uint256 id = _credit(ALICE, INSURER_A, 1000e6, 100);
        k.setK(0.75e18);
        uint256[] memory ids = new uint256[](1);
        ids[0] = id;
        vm.prank(ALICE);
        (uint256 quotedCoverage, uint256 quotedPremium) = buck.quoteMint(100e6, ids);
        assertEq(quotedCoverage, 153_846_154);
        assertEq(quotedPremium, 15_384_615);
        vm.prank(ALICE);
        buck.mint(100e6);
        assertEq(buck.balanceOf(ALICE), 100e6);
        assertEq(buck.mintsBacked(id), 153_846_154);
        assertEq(buck.mintsPrincipal(id), 15_384_615);
        assertEq(buck.balanceOf(address(pool)), 15_384_615);
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(principal, 115_384_615);
        assertEq(buck.totalIssued(), principal);
    }

    function test_mintDefaultsToCheapestAndHonorsExplicitOrder() public {
        _pool(INSURER_A);
        uint256 expensive = _credit(ALICE, INSURER_A, 100e6, 200);
        uint256 cheap = _credit(ALICE, INSURER_A, 100e6, 100);
        uint256 snapshot = vm.snapshotState();
        vm.prank(ALICE);
        buck.mint(110e6);
        assertEq(buck.balanceOf(ALICE), 110e6);
        assertEq(buck.mintsBacked(cheap), 100e6 - 1);
        assertEq(buck.mintsBacked(expensive), 25e6 - 1);
        assertEq(buck.mintsPrincipal(cheap), 10e6 - 1);
        assertEq(buck.mintsPrincipal(expensive), 5e6 - 1);
        assertTrue(vm.revertToState(snapshot));

        uint256[] memory ids = new uint256[](2);
        ids[0] = expensive;
        ids[1] = cheap;
        vm.prank(ALICE);
        (uint256 coverage, uint256 premium) = buck.quoteMint(90e6, ids);
        vm.prank(ALICE);
        buck.mint(90e6, ids);
        assertEq(buck.balanceOf(ALICE), 90e6);
        assertEq(buck.mintsBacked(expensive), 100e6 - 1);
        assertGt(buck.mintsBacked(cheap), 0);
        assertEq(coverage, buck.mintsBacked(expensive) + buck.mintsBacked(cheap));
        assertEq(premium, buck.mintsPrincipal(expensive) + buck.mintsPrincipal(cheap));
    }

    function test_mintUsesOnePreexistingReserveForTheEntireBatch() public {
        MockNetInsurerPool pool = _pool(INSURER_A);
        uint256 seed = _credit(ALICE, INSURER_A, 15e6, 0);
        vm.prank(ALICE);
        buck.issue(seed, 15e6, 15e6, 0, address(0));
        uint256 first = _credit(ALICE, INSURER_A, 100e6, 100);
        uint256 second = _credit(ALICE, INSURER_A, 100e6, 100);
        k.setFactor(1e18);
        uint256[] memory ids = new uint256[](2);
        ids[0] = first;
        ids[1] = second;
        vm.prank(ALICE);
        (, uint256 quotedPremium) = buck.quoteMint(180e6, ids);
        assertEq(quotedPremium, 20e6 - 2); // quote does not pretend the reserve is funded
        vm.expectRevert(BuckNet.FundingInsufficient.selector);
        vm.prank(ALICE);
        buck.mint(180e6);
        assertEq(buck.balanceOf(ALICE), 15e6);
        assertEq(buck.totalIssued(), 15e6);
        assertEq(buck.balanceOf(address(pool)), 0);
        assertEq(buck.mintsBacked(first), 0);
        assertEq(buck.mintsBacked(second), 0);

        uint256 bobId = _credit(BOB, INSURER_B, 5e6, 0);
        vm.prank(BOB);
        buck.issue(bobId, 5e6, 5e6, 0, address(0));
        vm.prank(BOB);
        buck.transfer(ALICE, 5e6);
        vm.prank(ALICE);
        buck.mint(180e6);
        assertEq(buck.balanceOf(ALICE), 200e6);
        assertEq(buck.balanceOf(address(pool)), 20e6 - 2);
    }

    function test_mintPaysEachFacilitysOriginalInsurerPool() public {
        MockNetInsurerPool poolA = _pool(INSURER_A);
        MockNetInsurerPool poolB = _pool(INSURER_B);
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER_B, true);
        uint256 first = _credit(ALICE, INSURER_A, 100e6, 100);
        uint256 second = _credit(ALICE, INSURER_B, 100e6, 200);
        vm.prank(ALICE);
        buck.mint(120e6);
        assertEq(buck.balanceOf(ALICE), 120e6);
        assertEq(buck.balanceOf(address(poolA)), 10e6 - 1);
        assertEq(buck.balanceOf(address(poolB)), 7_500_000 - 1);
        assertEq(buck.facilityPool(first), address(poolA));
        assertEq(buck.facilityPool(second), address(poolB));
        assertEq(buck.mintsPrincipal(first), buck.balanceOf(address(poolA)));
        assertEq(buck.mintsPrincipal(second), buck.balanceOf(address(poolB)));
    }

    function test_mintRejectsFundingFactorChangesDuringBatchAtomically() public {
        MockNetInsurerPool pool = _pool(INSURER_A);
        uint256 seed = _credit(ALICE, INSURER_A, 20e6, 0);
        vm.prank(ALICE);
        buck.issue(seed, 20e6, 20e6, 0, address(0));
        uint256 first = _credit(ALICE, INSURER_A, 100e6, 100);
        uint256 second = _credit(ALICE, INSURER_A, 100e6, 100);
        k.setFactor(1e18);
        fund.setFactorOnCheckpoint(k, 2e18);
        vm.expectRevert(BuckNet.FundingInsufficient.selector);
        vm.prank(ALICE);
        buck.mint(180e6);
        assertEq(k.fundingFactor(), 1e18);
        assertEq(buck.balanceOf(ALICE), 20e6);
        assertEq(buck.totalIssued(), 20e6);
        assertEq(buck.balanceOf(address(pool)), 0);
        assertEq(buck.mintsBacked(first), 0);
        assertEq(buck.mintsBacked(second), 0);
    }

    function test_mintRejectsKChangesDuringBatchAtomically() public {
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        fund.setKOnCheckpoint(k, 0.5e18);
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        vm.prank(ALICE);
        buck.mint(50e6);
        assertEq(k.currentBuckK(), 1e18);
        assertEq(buck.balanceOf(ALICE), 0);
        assertEq(buck.totalIssued(), 0);
        assertEq(buck.mintsBacked(id), 0);
    }

    function test_mintKeepsTheTokenGuardDuringFundValuation() public {
        TestWheel wheel = new TestWheel(address(buck), GOV);
        BuckLockedValuation source = new BuckLockedValuation(buck, ALICE);
        vm.prank(GOV);
        wheel.setValuationSource(address(source));
        fund.setValuationWheel(wheel);
        _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        buck.mint(50e6);
        assertTrue(wheel.valuationReady());
        assertEq(wheel.valuationMark(), 50e6);
        assertEq(buck.balanceOf(ALICE), 50e6);
    }

    function test_mintRestoresAKShortfallBeforeIncreasingWalletBalance() public {
        uint256 id = _credit(ALICE, INSURER_A, 1000e6, 0);
        vm.prank(ALICE);
        buck.issue(id, 400e6, 200e6, 0, address(0));
        k.setK(0.25e18);
        vm.prank(ALICE);
        buck.mint(40e6);
        assertEq(buck.balanceOf(ALICE), 240e6);
        assertEq(buck.mintsBacked(id), 960e6);
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(principal, 240e6);
        assertEq(buck.creditLimit(ALICE), 240e6);
    }

    function test_maximumMintFindsTheBestNonMonotoneRawCoverage() public {
        uint256 id = _credit(ALICE, INSURER_A, 5, 200);
        k.setK(0.75e18);
        uint256[] memory ids = new uint256[](1);
        ids[0] = id;
        vm.prank(ALICE);
        (uint256 coverage, uint256 premium) = buck.quoteMint(type(uint256).max, ids);
        assertEq(coverage, 4);
        assertEq(premium, 0);
        vm.prank(ALICE);
        buck.mint(type(uint256).max);
        assertEq(buck.balanceOf(ALICE), 3);
        assertEq(buck.mintsBacked(id), 4);
        assertEq(buck.mintsPrincipal(id), 0);
    }

    function test_mintRejectsInsufficientDuplicateAndForeignSelectionsAtomically() public {
        uint256 own = _credit(ALICE, INSURER_A, 100e6, 0);
        uint256 foreign = _credit(BOB, INSURER_B, 100e6, 0);
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        vm.prank(ALICE);
        buck.mint(101e6);
        assertEq(buck.mintsBacked(own), 0);
        assertEq(buck.totalIssued(), 0);

        uint256[] memory ids = new uint256[](2);
        ids[0] = own;
        ids[1] = own;
        vm.expectRevert(BuckNet.BadAmount.selector);
        vm.prank(ALICE);
        buck.mint(1e6, ids);
        ids[1] = foreign;
        vm.expectRevert(BuckNet.NotOwner.selector);
        vm.prank(ALICE);
        buck.mint(1e6, ids);
        assertEq(buck.mintsBacked(own), 0);
        assertEq(buck.totalIssued(), 0);
    }

    function test_mintPremiumDoesNotBypassPrivateIdentityReceipt() public {
        MockNetInsurerPool pool = _pool(INSURER_A);
        address privateOwner = address(0xBEEF);
        vm.etch(privateOwner, hex"60006000fd");
        identity.bindContract(privateOwner, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), false, false);
        vm.prank(privateOwner);
        credit.setCreditIssuer(INSURER_A, true);
        uint256 id = _credit(privateOwner, INSURER_A, 100e6, 100);
        vm.expectRevert(BuckNet.UnauthorizedTransfer.selector);
        vm.prank(privateOwner);
        buck.mint(50e6);
        assertEq(buck.balanceOf(privateOwner), 0);
        assertEq(buck.balanceOf(address(pool)), 0);
        assertEq(buck.mintsBacked(id), 0);
        assertEq(buck.totalIssued(), 0);
    }

    function test_mintZeroAndCarryingRestrictionRemainExplicit() public {
        vm.prank(ALICE);
        buck.mint(0);
        assertEq(buck.balanceOf(ALICE), 0);
        identity.setIsCarrying(BOB, true);
        _credit(BOB, INSURER_B, 100e6, 0);
        vm.expectRevert(BuckNet.NotCarryingPool.selector);
        vm.prank(BOB);
        buck.mint(1e6);
    }

    function test_mintSkipsFullyMaturedFacilitiesAfterProcessingClaims() public {
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        buck.mint(25e6);
        vm.warp(t0 + 50 * YEAR);
        buck.processMaturities(1);
        uint256 oldBalance = buck.balanceOf(ALICE);
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        vm.prank(ALICE);
        buck.mint(1);
        vm.prank(ALICE);
        buck.mint(type(uint256).max);
        assertEq(buck.balanceOf(ALICE), oldBalance);
        assertEq(buck.totalIssued(), 25e6);
        assertEq(buck.mintsBacked(id), 25e6);
    }

    function test_mintWorkLimitDoesNotPretendThereIsNoCapacity() public {
        uint256 id = _credit(ALICE, INSURER_A, 10_000_000_000, 40);
        // Isolate the planner's near-equal K/premium case with activated
        // coverage and no issuance; a real activated facility normally also
        // carries its original premium deposit.
        vm.prank(address(buck));
        credit.activateFromBuck(id, ALICE, 238_720_887);
        k.setK(39_998_000_000_000_000);
        uint256[] memory ids = new uint256[](1);
        ids[0] = id;
        vm.expectRevert(BuckNet.AllocationWorkLimit.selector);
        vm.prank(ALICE);
        buck.quoteMint(9_548_359, ids);
        vm.expectRevert(BuckNet.AllocationWorkLimit.selector);
        vm.prank(ALICE);
        buck.mint(9_548_359);
        assertEq(buck.totalIssued(), 0);
        assertEq(buck.mintsBacked(id), 238_720_887);
        assertEq(buck.balanceOf(ALICE), 0);
    }

    function testFuzz_mintMatchesExhaustiveSingleFacilityIssuance(
        uint8 f, uint8 kValue, uint16 r, uint8 requested
    ) public {
        MockNetInsurerPool pool = _pool(INSURER_A);
        uint256 face = uint256(f) % 40 + 1;
        uint256 factor = uint256(kValue) * 1e16;
        uint32 rate = uint32(uint256(r) % 1000);
        uint256 amount = uint256(requested) % 60 + 1;
        k.setK(factor);
        uint256 id = _credit(ALICE, INSURER_A, face, rate);
        bool feasible;
        uint256 expectedCoverage;
        uint256 expectedPremium;
        for (uint256 coverage; coverage <= face; ++coverage) {
            uint256 premium = coverage * uint256(rate) * 10 / 10_000;
            if (coverage * factor / 1e18 >= amount + premium) {
                feasible = true;
                expectedCoverage = coverage;
                expectedPremium = premium;
                break;
            }
        }
        if (!feasible) {
            vm.expectRevert(BuckNet.CreditExceeded.selector);
            vm.prank(ALICE);
            buck.mint(amount);
            assertEq(buck.totalIssued(), 0);
            assertEq(buck.mintsBacked(id), 0);
            return;
        }
        vm.prank(ALICE);
        buck.mint(amount);
        assertEq(buck.balanceOf(ALICE), amount);
        assertEq(buck.mintsBacked(id), expectedCoverage);
        assertEq(buck.mintsPrincipal(id), expectedPremium);
        assertEq(buck.balanceOf(address(pool)), expectedPremium);
        assertEq(buck.totalIssued(), amount + expectedPremium);
    }
}
