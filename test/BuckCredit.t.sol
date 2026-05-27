// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "../src/BuckCredit.sol";
import {BuckCreditHarness} from "./harness/BuckCreditHarness.sol";

contract BuckCreditTest is Test {
    BuckCreditHarness public credit;

    address insurer = makeAddr("insurer");
    address alice   = makeAddr("alice");

    uint256 constant FACE_VALUE = 500_000e6;  // $500K
    uint256 constant FLOOR      = 100_000e6;  // $100K floor

    function setUp() public {
        credit = new BuckCreditHarness();
    }

    function test_createCredit() public {
        vm.prank(insurer);
        uint256 tokenId = credit.createCredit(
            alice,
            1,                                          // assetClass: residential
            FACE_VALUE,
            FLOOR,
            BuckCredit.DepreciationType.LINEAR,
            200,                                        // 2% annual
            uint48(block.timestamp),                    // depreciation starts now
            100                                         // 1% annual premium
        );

        assertEq(credit.ownerOf(tokenId), alice);
        assertEq(credit.balanceOf(alice), 1);

        // Not activated yet — current value is 0
        assertEq(credit.currentValue(tokenId), 0);
    }

    function test_activate() public {
        vm.prank(insurer);
        uint256 tokenId = credit.createCredit(
            alice, 1, FACE_VALUE, FLOOR,
            BuckCredit.DepreciationType.NONE, 0,
            uint48(block.timestamp), 100
        );

        // Activate half
        vm.prank(alice);
        credit.forceActivate(tokenId, 250_000e6);
        assertEq(credit.currentValue(tokenId), 250_000e6);

        // Activate rest
        vm.prank(alice);
        credit.forceActivate(tokenId, 250_000e6);
        assertEq(credit.currentValue(tokenId), 500_000e6);
    }

    function test_activate_reverts_if_exceeds_face() public {
        vm.prank(insurer);
        uint256 tokenId = credit.createCredit(
            alice, 1, FACE_VALUE, FLOOR,
            BuckCredit.DepreciationType.NONE, 0,
            uint48(block.timestamp), 100
        );

        vm.prank(alice);
        vm.expectRevert("Exceeds face value");
        credit.forceActivate(tokenId, FACE_VALUE + 1);
    }

    function test_activate_reverts_if_not_owner() public {
        vm.prank(insurer);
        uint256 tokenId = credit.createCredit(
            alice, 1, FACE_VALUE, FLOOR,
            BuckCredit.DepreciationType.NONE, 0,
            uint48(block.timestamp), 100
        );

        vm.prank(insurer);  // insurer is not the owner
        vm.expectRevert("Not credit owner");
        credit.forceActivate(tokenId, 100e6);
    }

    function test_linear_depreciation() public {
        vm.prank(insurer);
        uint256 tokenId = credit.createCredit(
            alice, 1, FACE_VALUE, FLOOR,
            BuckCredit.DepreciationType.LINEAR,
            200,                               // 2% per year
            uint48(block.timestamp),
            100
        );

        // Activate fully
        vm.prank(alice);
        credit.forceActivate(tokenId, FACE_VALUE);

        // At t=0: full value
        assertEq(credit.currentValue(tokenId), FACE_VALUE);

        // After 10 years: lost 20% of depreciable portion ($400K * 0.20 = $80K)
        vm.warp(block.timestamp + 365.25 days * 10);
        uint256 val10 = credit.currentValue(tokenId);
        // Expected: 500K - 80K = 420K
        assertApproxEqRel(val10, 420_000e6, 0.001e18);  // within 0.1%

        // After 50 years: fully depreciated to floor
        vm.warp(block.timestamp + 365.25 days * 40);  // total 50 years
        assertEq(credit.currentValue(tokenId), FLOOR);
    }

    function test_no_depreciation() public {
        vm.prank(insurer);
        uint256 tokenId = credit.createCredit(
            alice, 1, 200_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0,
            uint48(block.timestamp), 100
        );

        vm.prank(alice);
        credit.forceActivate(tokenId, 200_000e6);

        // 100 years later: still full value
        vm.warp(block.timestamp + 365.25 days * 100);
        assertEq(credit.currentValue(tokenId), 200_000e6);
    }

    function test_declining_balance_depreciation() public {
        vm.prank(insurer);
        uint256 tokenId = credit.createCredit(
            alice, 1, 100_000e6, 5_000e6,  // $100K face, $5K floor
            BuckCredit.DepreciationType.DECLINING_BALANCE,
            1500,                              // 15% per year
            uint48(block.timestamp),
            100
        );

        vm.prank(alice);
        credit.forceActivate(tokenId, 100_000e6);

        // Discrete annual compounding: depreciable * (BP - rate)/BP per full year,
        // linearly interpolated across the trailing partial year.
        // After 1 year: 5K + 95K * 0.85 = 5K + 80,750 = 85,750
        vm.warp(block.timestamp + 365.25 days);
        uint256 val1 = credit.currentValue(tokenId);
        assertApproxEqRel(val1, 85_750e6, 0.001e18);  // within 0.1%

        // After 5 years total: 5K + 95K * 0.85^5 = 5K + 42,152 = 47,152
        vm.warp(block.timestamp + 365.25 days * 4);  // total 5 years
        uint256 val5 = credit.currentValue(tokenId);
        assertApproxEqRel(val5, 47_152e6, 0.001e18);  // within 0.1%

        // After 10 years total: 5K + 95K * 0.85^10 = 5K + 18,703 = 23,703
        vm.warp(block.timestamp + 365.25 days * 5);  // total 10 years
        uint256 val10 = credit.currentValue(tokenId);
        assertApproxEqRel(val10, 23_703e6, 0.001e18);  // within 0.1%
    }

    function test_totalCurrentValue() public {
        // Create two credits for alice (land + structure)
        vm.startPrank(insurer);
        uint256 landId = credit.createCredit(
            alice, 1, 200_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0,
            uint48(block.timestamp), 50
        );
        uint256 structId = credit.createCredit(
            alice, 2, 300_000e6, 60_000e6,
            BuckCredit.DepreciationType.LINEAR, 200,
            uint48(block.timestamp), 100
        );
        vm.stopPrank();

        // Activate both fully
        vm.startPrank(alice);
        credit.forceActivate(landId, 200_000e6);
        credit.forceActivate(structId, 300_000e6);
        vm.stopPrank();

        // At t=0: total = 200K + 300K = 500K
        assertEq(credit.totalCurrentValue(alice), 500_000e6);

        // After 5 years: land unchanged, structure depreciated
        vm.warp(block.timestamp + 365.25 days * 5);
        uint256 total = credit.totalCurrentValue(alice);
        // Structure: 300K - (240K * 0.02 * 5) = 300K - 24K = 276K
        // Total: 200K + 276K = 476K
        assertApproxEqRel(total, 476_000e6, 0.001e18);
    }

    function test_insurer_update() public {
        vm.prank(insurer);
        uint256 tokenId = credit.createCredit(
            alice, 1, FACE_VALUE, FLOOR,
            BuckCredit.DepreciationType.LINEAR, 200,
            uint48(block.timestamp), 100
        );

        vm.prank(alice);
        credit.forceActivate(tokenId, 300_000e6);

        // Insurer reappraises: reduce face value below activated
        vm.prank(insurer);
        credit.updateCredit(
            tokenId,
            250_000e6,     // new face value (below 300K activated)
            50_000e6,
            BuckCredit.DepreciationType.LINEAR,
            250,
            uint48(block.timestamp),
            120
        );

        // Activated should be capped to new face value
        (, uint256 activated, ) = credit.creditInfo(tokenId);
        assertEq(activated, 250_000e6);
    }

    function test_insurer_update_reverts_if_not_insurer() public {
        vm.prank(insurer);
        uint256 tokenId = credit.createCredit(
            alice, 1, FACE_VALUE, FLOOR,
            BuckCredit.DepreciationType.NONE, 0,
            uint48(block.timestamp), 100
        );

        vm.prank(alice);
        vm.expectRevert("Not insurer");
        credit.updateCredit(
            tokenId, FACE_VALUE, FLOOR,
            BuckCredit.DepreciationType.NONE, 0,
            uint48(block.timestamp), 100
        );
    }

    function test_depStartAt_future_delaysDepreciation() public {
        vm.prank(insurer);
        uint256 tokenId = credit.createCredit(
            alice, 1, FACE_VALUE, FLOOR,
            BuckCredit.DepreciationType.LINEAR, 200,
            uint48(block.timestamp + 365 days),  // starts in 1 year
            100
        );

        vm.prank(alice);
        credit.forceActivate(tokenId, FACE_VALUE);

        // At t=0: no depreciation (start is in the future).
        assertEq(credit.currentValue(tokenId), FACE_VALUE);

        // After 2 years: only 1 year of depreciation has elapsed.
        vm.warp(block.timestamp + 365 days * 2);
        uint256 val = credit.currentValue(tokenId);
        // 500K - (400K * 0.02 * 1) = 500K - 8K = 492K
        assertApproxEqRel(val, 492_000e6, 0.001e18);
    }

    function test_DECLINING_BALANCE_zeroRate_noDepreciation() public {
        vm.prank(insurer);
        uint256 tokenId = credit.createCredit(
            alice, 1, 100_000e6, 5_000e6,
            BuckCredit.DepreciationType.DECLINING_BALANCE, 0,  // rate=0
            uint48(block.timestamp),
            100
        );
        vm.prank(alice);
        credit.forceActivate(tokenId, 100_000e6);

        vm.warp(block.timestamp + 365 days * 50);
        // rate=0 means no decline — value stays at face.
        assertEq(credit.currentValue(tokenId), 100_000e6);
    }

    function test_updateCredit_newFaceBelowActivated_capsActivated() public {
        vm.prank(insurer);
        uint256 tokenId = credit.createCredit(
            alice, 1, FACE_VALUE, FLOOR,
            BuckCredit.DepreciationType.NONE, 0, 0, 100
        );
        vm.prank(alice);
        credit.forceActivate(tokenId, FACE_VALUE);

        // Insurer reduces face value below activation.
        vm.prank(insurer);
        credit.updateCredit(
            tokenId, 250_000e6, FLOOR,
            BuckCredit.DepreciationType.NONE, 0, 0, 100
        );
        (, uint256 activated,) = credit.creditInfo(tokenId);
        assertEq(activated, 250_000e6, "activated capped to new face");
    }

    function test_updateCredit_newFaceZero_clearsActivated() public {
        vm.prank(insurer);
        uint256 tokenId = credit.createCredit(
            alice, 1, FACE_VALUE, FLOOR,
            BuckCredit.DepreciationType.NONE, 0, 0, 100
        );
        vm.prank(alice);
        credit.forceActivate(tokenId, 100_000e6);

        // Insurer zeros the face value (total write-off).
        vm.prank(insurer);
        credit.updateCredit(
            tokenId, 0, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 100
        );
        (, uint256 activated,) = credit.creditInfo(tokenId);
        assertEq(activated, 0, "activated cleared on total write-off");
    }

    function test_DECLINING_BALANCE_maxRate_immediateDepreciation() public {
        vm.prank(insurer);
        uint256 tokenId = credit.createCredit(
            alice, 1, 100_000e6, 10_000e6,
            BuckCredit.DepreciationType.DECLINING_BALANCE,
            uint32(10000),  // rate = 100% -> immediate decline to floor
            uint48(block.timestamp),
            100
        );
        vm.prank(alice);
        credit.forceActivate(tokenId, 100_000e6);

        vm.warp(block.timestamp + 1);
        assertEq(credit.currentValue(tokenId), 10_000e6, "rate=100% -> immediate floor");
    }
}
