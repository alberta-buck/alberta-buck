// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {NetDemurrageMath} from "../src/lib/NetDemurrageMath.sol";

contract NetDemurrageMathHarness {
    function decay(uint256 amount, uint256 elapsed) external pure returns (uint256) {
        return NetDemurrageMath.decay(amount, elapsed);
    }
    function fundNetAfter(uint256 issuance, uint256 fund, uint256 elapsed) external pure returns (uint256) {
        return NetDemurrageMath.fundNetAfter(issuance, fund, elapsed);
    }
    function targetRelief(uint256 principal, uint256 seconds_, uint256 reduction) external pure returns (uint256) {
        return NetDemurrageMath.targetRelief(principal, seconds_, reduction);
    }
    function nominalNet(uint256 nominal, uint256 gross, uint256 fee) external pure returns (uint256) {
        return NetDemurrageMath.nominalNet(nominal, gross, fee);
    }
    function nominalForNet(uint256 net, uint256 gross, uint256 fee) external pure returns (uint256) {
        return NetDemurrageMath.nominalForNet(net, gross, fee);
    }
}

contract NetDemurrageMathTest is Test {
    uint256 constant YEAR = 365 days + 6 hours;
    uint256 constant SCALE = 1e36;
    NetDemurrageMathHarness internal math;

    function setUp() public { math = new NetDemurrageMathHarness(); }

    /// Generated independently using Python Decimal at 100 digits: floor of
    /// (2**80 - 1) * exp(-0.02 * years). A one-raw-unit tolerance includes
    /// both the per-second factor rounding and the final integer division.
    function test_decayAgreesWithHighPrecisionAcrossLongIdleIntervals() public view {
        uint256[6] memory years_ = [uint256(1), 49, 50, 51, 100, 1000];
        uint256[6] memory expected = [
            uint256(1184987484512541293592508),
            453723277789395297581471,
            444738954937557665364702,
            435932533597626853982698,
            163610318209599982778093,
            2491781832358195
        ];
        for (uint256 i; i < years_.length; ++i) {
            assertApproxEqAbs(math.decay(type(uint80).max, years_[i] * YEAR), expected[i], 1);
        }
    }

    function test_millionUnspentAndFundMatchReferenceAtOneAndFiftyYears() public view {
        uint256 issued = 1_000_000e6;
        assertEq(math.decay(issued, 0), issued);
        assertApproxEqAbs(math.decay(issued, YEAR), 980198673306, 1);
        assertApproxEqAbs(math.fundNetAfter(issued, 0, YEAR), 19801326694, 1);
        assertApproxEqAbs(math.decay(issued, 50 * YEAR), 367879441171, 1);
        assertApproxEqAbs(math.fundNetAfter(issued, 0, 50 * YEAR), 632120558829, 1);
        assertEq(math.targetRelief(issued, issued * YEAR, issued), 20_000e6);
        assertEq(math.targetRelief(issued, issued * 50 * YEAR, issued), issued);
    }

    function test_fractionalAccountCheckpointsKeepSmallBalances() public view {
        uint256 scaled = SCALE;
        uint256 aged = math.decay(scaled, YEAR);
        assertGt(aged, 0);
        assertLt(aged, scaled);
        uint256 newlyReceived = 2 * SCALE;
        assertEq((aged + newlyReceived) / SCALE, 2, "old fees do not consume fresh BUCK");
    }

    function test_checkpointFrequencyCostsLessThanOneExternalRawUnit() public view {
        uint256 initial = uint256(type(uint80).max) * SCALE;
        uint256 lazy = math.decay(initial, YEAR);
        uint256 touched = initial;
        uint256 previous;
        for (uint256 i = 1; i <= 365; ++i) {
            uint256 now_ = i * YEAR / 365;
            touched = math.decay(touched, now_ - previous);
            previous = now_;
        }
        assertApproxEqAbs(touched / SCALE, lazy / SCALE, 1);
    }

    function test_veryOldLargeBalancesDoNotClampAtTwoHundredYears() public view {
        uint256 scaled = uint256(type(uint80).max) * SCALE;
        // Python Decimal, 120 digits: floor((2**80 - 1) * SCALE * exp(-80)).
        uint256 expected = 21819314433336195015611879;
        assertApproxEqRel(math.decay(scaled, 4000 * YEAR), expected, 1);
        assertEq(math.decay(type(uint256).max, 8000 * YEAR), 37719140);
        assertEq(math.decay(type(uint256).max, 9000 * YEAR), 0);
    }

    function test_partialReliefUsesThisFacilitiesOwnAgeAndCapsAtPrincipal() public {
        uint256 p = 100e6;
        uint256 old = p * 25 * YEAR;
        assertEq(math.targetRelief(p, old, p), 50e6);
        assertEq(math.targetRelief(p, old, 25e6), 12500000);
        assertEq(math.targetRelief(p, p * 100 * YEAR, 25e6), 25e6);
        vm.expectRevert(NetDemurrageMath.InvalidPrincipal.selector);
        math.targetRelief(p, old, p + 1);
        vm.expectRevert(NetDemurrageMath.InvalidPrincipal.selector);
        math.targetRelief(0, 0, 0);
    }

    function test_nominalRefundAndFullValuePayoutHaveDifferentSemantics() public view {
        assertEq(math.nominalNet(10e6, 100e6, 20e6), 8e6);
        uint256 gross = math.nominalForNet(40e6, 100e6, 20e6);
        assertEq(gross, 49999999);
        assertEq(math.nominalNet(gross, 100e6, 20e6), 40e6);
        assertLt(math.nominalNet(gross - 1, 100e6, 20e6), 40e6);
    }

    function test_rejectsInvalidFundAndCarryingAmounts() public {
        vm.expectRevert(NetDemurrageMath.FundExceedsIssuance.selector);
        math.fundNetAfter(99, 100, YEAR);
        vm.expectRevert(NetDemurrageMath.FeeExceedsNominal.selector);
        math.nominalNet(1, 10, 11);
        vm.expectRevert(NetDemurrageMath.InsufficientNet.selector);
        math.nominalForNet(81, 100, 20);
        assertEq(math.nominalForNet(0, 0, 0), 0);
    }

    function testFuzz_nominalGrossUpIsSufficientAndMinimal(uint80 gross_, uint80 fee_, uint80 net_) public view {
        uint256 gross = bound(gross_, 1, type(uint80).max);
        uint256 fee = bound(fee_, 0, gross);
        uint256 net = bound(net_, 0, gross - fee);
        uint256 nominal = math.nominalForNet(net, gross, fee);
        assertLe(nominal, gross);
        assertGe(math.nominalNet(nominal, gross, fee), net);
        if (nominal > 0) assertLt(math.nominalNet(nominal - 1, gross, fee), net);
    }

    function testFuzz_fundAndOutsideRemainConserved(uint80 issued_, uint80 fund_, uint40 elapsed_) public view {
        uint256 issued = issued_;
        uint256 fund = bound(fund_, 0, issued);
        uint256 elapsed = bound(elapsed_, 0, 1000 * YEAR);
        uint256 outside = math.decay(issued - fund, elapsed);
        uint256 after_ = math.fundNetAfter(issued, fund, elapsed);
        assertGe(after_, fund);
        assertEq(after_ + outside, issued);
    }
}
