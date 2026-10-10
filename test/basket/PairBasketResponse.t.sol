// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityTest, IEquityWheel, IPool} from "./BuckBasketEquity.t.sol";
import {BuckBasketEquityStorage} from "../../src/basket/BuckBasketEquityStorage.sol";
import {PairBasketEquityResponse} from "../../src/basket/PairBasketEquityResponse.sol";
import {IBuckBasketVenue} from "../../src/basket/IBuckBasketVenue.sol";
import {ValidatedBasketVenue} from "../../src/basket/ValidatedBasketVenue.sol";
import {PairBasketMaintenanceControl} from "../harness/PairBasketMaintenanceControl.sol";

contract ControlledPairIntent {
    address public basket;
    uint256 public constant count = 3;
    uint256 public rate = 1e18;
    uint256 public impacts;
    uint64 public lastExecutionAt;
    uint256 public onlyRecipient = type(uint256).max;
    constructor(address basket_) { basket = basket_; }
    function signals() external view returns (address) { return address(this); }
    function isDue() external pure returns (bool) { return true; }
    function observe() external {}
    function setRate(uint256 r) external { rate = r; }
    function setRecipient(uint256 j) external { onlyRecipient = j; }
    function desire(uint256, uint256 j) external view returns (uint256) {
        return onlyRecipient == type(uint256).max || onlyRecipient == j ? rate : 0;
    }
    function fraction(uint256 r, uint256 elapsed) external pure returns (uint256) {
        if (elapsed > 1 days) elapsed = 1 days;
        uint256 u = r * elapsed / 1 days;
        return u * 1e18 / (1e18 + u);
    }
    function recordImpact(uint256, int24, int24) external { require(msg.sender == basket); ++impacts; }
    function recordExecution() external { require(msg.sender == basket); lastExecutionAt = uint64(block.timestamp); }
}

contract PairBasketResponseTest is BuckBasketEquityTest {
    ControlledPairIntent intent;
    function _install() private {
        // One overweight donor and two equally underweight recipients.
        _installValues(40_000e18, 30_000e18, 30_000e18);
    }

    function _installValues(uint256 a, uint256 c1, uint256 c2) private {
        _deposit(alice, address(tok[0]), a);
        _deposit(alice, address(tok[1]), c1 / 2);
        _deposit(alice, address(tok[2]), c2 / 3);
        intent = new ControlledPairIntent(address(b));
        vm.startPrank(GOV);
        b.setVenue(address(new ValidatedBasketVenue()));
        b.setEquityWheel(address(new PairBasketEquityResponse(address(intent))));
        b.setResponse(address(0), BuckBasketEquityStorage.ResponseParams(3000, 1800, 0));
        vm.stopPrank();
    }

    function test_pair_oneDonorFundsBothRecipientsAndRecordsOwnImpact() public {
        _install();
        uint256 idle = b.idleToken(0);
        int256 account = buck.signedBalanceOf(address(b));
        assertEq(IEquityWheel(address(b)).wheelStep(2, 0), 1);
        assertLt(b.idleToken(0), idle);
        assertGt(b.liquidityOf(1), 0); assertGt(b.liquidityOf(2), 0);
        IBuckBasketVenue.Marks memory m1 = IBuckBasketVenue(address(b)).marks(1, b.liquidityOf(1));
        IBuckBasketVenue.Marks memory m2 = IBuckBasketVenue(address(b)).marks(2, b.liquidityOf(2));
        assertApproxEqRel(m1.posTwap, m2.posTwap, 0.005e18, "equal deficits receive equal value");
        assertGe(buck.signedBalanceOf(address(b)), account, "transfer did not add a fresh debt draw");
        assertEq(intent.impacts(), 3);
        uint256 remaining = b.idleToken(0);
        assertEq(IEquityWheel(address(b)).wheelStep(2, 0), 0, "same interval cannot repeat allowance");
        assertEq(b.idleToken(0), remaining);
        _books();
    }

    function test_pair_failedRecipientRollsBackDonorAndOtherRecipient() public {
        _install();
        uint256 idle = b.idleToken(0);
        int256 account = buck.signedBalanceOf(address(b));
        (uint160 spot,,,,,,) = IPool(pools[0]).slot0();
        vm.mockCallRevert(address(b), abi.encodeWithSelector(IBuckBasketVenue.monetaryLeg.selector, uint256(2), true), bytes("recipient refusal"));
        vm.expectRevert(bytes("recipient refusal"));
        IEquityWheel(address(b)).wheelStep(2, 0);
        assertEq(b.idleToken(0), idle); assertEq(b.liquidityOf(1), 0); assertEq(b.liquidityOf(2), 0);
        assertEq(buck.signedBalanceOf(address(b)), account);
        (uint160 afterSpot,,,,,,) = IPool(pools[0]).slot0(); assertEq(afterSpot, spot);
        assertEq(intent.impacts(), 0);
    }

    function test_pair_quenchedIntentLeavesDiscretionaryInventoryAlone() public {
        _install(); intent.setRate(0);
        (uint160 spot,,,,,,) = IPool(pools[0]).slot0();
        IEquityWheel(address(b)).wheelStep(2, 0); // May refresh a credit mark.
        (uint160 afterSpot,,,,,,) = IPool(pools[0]).slot0();
        assertEq(afterSpot, spot, "LP pairing can move inventory but does not trade TOKEN");
        assertEq(intent.impacts(), 0);
        assertEq(intent.lastExecutionAt(), 0, "bookkeeping/LP work does not consume pair budget");
    }

    function test_maintenanceControlSuppressesTradesButRetainsLPPlacement() public {
        _install(); // Positive intent would normally exchange the overweight TOKEN.
        vm.startPrank(GOV);
        b.setEquityWheel(address(new PairBasketMaintenanceControl(address(intent))));
        vm.stopPrank();
        (uint160 spot,,,,,,) = IPool(pools[0]).slot0();
        IEquityWheel(address(b)).wheelStep(2, 0);
        (uint160 afterSpot,,,,,,) = IPool(pools[0]).slot0();
        assertEq(afterSpot, spot);
        assertEq(intent.impacts(), 0); assertEq(intent.lastExecutionAt(), 0);
        assertGt(b.liquidityOf(1) + b.liquidityOf(2), 0);
        _books();
    }

    function test_maintenanceControlRetainsLiquidityOverride() public {
        _install();
        vm.startPrank(GOV);
        b.setEquityWheel(address(new PairBasketMaintenanceControl(address(intent))));
        vm.stopPrank();
        ctrl.setK(0);
        int256 account = buck.signedBalanceOf(address(b));
        assertEq(IEquityWheel(address(b)).wheelStep(2, 0), 1);
        assertGe(buck.signedBalanceOf(address(b)), account);
        assertEq(b.liquidityOf(1) + b.liquidityOf(2), 0);
        _books();
    }

    function test_pair_liquidityOverrideDoesNotRequireAClosureSignal() public {
        _install(); intent.setRate(0); ctrl.setK(0);
        int256 account = buck.signedBalanceOf(address(b));
        assertEq(IEquityWheel(address(b)).wheelStep(2, 0), 1);
        assertGe(buck.signedBalanceOf(address(b)), account);
        assertEq(b.liquidityOf(1) + b.liquidityOf(2), 0, "no simultaneous fresh draw");
        _books();
    }

    function test_pair_markOnlyDoesNotStarveALaterExecutableTransfer() public {
        _install(); intent.setRate(0.01e18);
        vm.prank(address(b)); intent.recordExecution();
        uint256 start = vm.getBlockTimestamp();
        uint256 idle = b.idleToken(0);
        // The six-hour request is below the minimum leg size. Bookkeeping
        // must leave that unused time available to the next response.
        vm.warp(start + 6 hours);
        IEquityWheel(address(b)).wheelStep(2, 0);
        assertEq(b.idleToken(0), idle);
        assertEq(intent.lastExecutionAt(), start);
        vm.warp(start + 1 days);
        assertEq(IEquityWheel(address(b)).wheelStep(2, 0), 1);
        assertLt(b.idleToken(0), idle, "one day's released gap now clears the minimum size");
        assertEq(intent.lastExecutionAt(), vm.getBlockTimestamp());
        _books();
    }

    function test_pair_oneMaterialRecipientCanUseADonorInsideItsBand() public {
        _installValues(35_000e18, 31_000e18, 34_000e18);
        uint256 idle = b.idleToken(0);
        IEquityWheel(address(b)).wheelStep(2, 0);
        assertLt(b.idleToken(0), idle, "+1.67pp donor can fund -2.33pp recipient");
        assertGt(b.liquidityOf(1), 0); assertEq(b.liquidityOf(2), 0);
        uint256[] memory weights = b.responseWeightsBp();
        assertGe(weights[0], 3333); assertLe(weights[1], 3333);
        _books();
    }

    function test_pair_oneMaterialDonorCanFundRecipientsInsideTheirBands() public {
        _installValues(36_000e18, 32_000e18, 32_000e18);
        uint256 idle = b.idleToken(0);
        IEquityWheel(address(b)).wheelStep(2, 0);
        assertLt(b.idleToken(0), idle);
        assertGt(b.liquidityOf(1), 0); assertGt(b.liquidityOf(2), 0);
        uint256[] memory weights = b.responseWeightsBp();
        assertGe(weights[0], 3333); assertLe(weights[1], 3333); assertLe(weights[2], 3334);
        _books();
    }

    function test_pair_noMaterialGapCannotAuthorizeATokenSwap() public {
        _installValues(34_500e18, 32_500e18, 33_000e18);
        (uint160 beforeSpot,,,,,,) = IPool(pools[0]).slot0();
        IEquityWheel(address(b)).wheelStep(2, 0);
        (uint160 afterSpot,,,,,,) = IPool(pools[0]).slot0();
        assertEq(afterSpot, beforeSpot);
        assertEq(intent.impacts(), 0); assertEq(intent.lastExecutionAt(), 0);
    }

    function test_pair_intentCannotBuyAnAlreadyOverweightRecipient() public {
        _installValues(40_000e18, 36_000e18, 24_000e18);
        intent.setRecipient(1);
        IEquityWheel(address(b)).wheelStep(2, 0);
        assertEq(intent.impacts(), 0); assertEq(intent.lastExecutionAt(), 0);
        assertEq(b.liquidityOf(1), 0, "at least one error does not justify worsening the other leg");
    }

    function test_pair_idlePlacementChoosesLargestShortageNotLargestCappedLeg() public {
        _installValues(33_333e18, 36_700e18, 29_967e18); intent.setRate(0);
        IEquityWheel(address(b)).wheelStep(2, 0);
        assertEq(b.liquidityOf(0), 0); assertEq(b.liquidityOf(1), 0);
        assertGt(b.liquidityOf(2), 0, "fund the smallest allocation first");
        assertEq(intent.impacts(), 0); assertEq(intent.lastExecutionAt(), 0);
        _books();
    }

    function test_pair_balancedBootstrapDoesNotConcentrateTheNewBook() public {
        _installValues(33_333e18, 33_333e18, 33_334e18); intent.setRate(0);
        for (uint256 k; k < 40; ++k) {
            IEquityWheel(address(b)).wheelStep(2, 0);
            uint256[] memory weights = b.responseWeightsBp();
            for (uint256 i; i < 3; ++i) {
                if (k >= 2) assertGt(b.liquidityOf(i), 0);
                assertApproxEqAbs(weights[i], i == 2 ? 3334 : 3333, 120);
            }
            vm.warp(vm.getBlockTimestamp() + 6 hours);
        }
        assertEq(intent.impacts(), 0); assertEq(intent.lastExecutionAt(), 0);
        _books();
    }

    function test_pair_priceTurnVetoCannotBlockPlacementOfOwnedInventory() public {
        _installValues(33_333e18, 36_700e18, 29_967e18); intent.setRate(0);
        vm.prank(GOV);
        b.setResponse(address(intent), BuckBasketEquityStorage.ResponseParams(3000, 1800, 0));
        vm.mockCall(address(intent), abi.encodeWithSignature("fresh()"), abi.encode(true));
        vm.mockCall(address(intent), abi.encodeWithSignature("running(uint256,int256)", uint256(2), int256(-1)), abi.encode(true));
        IEquityWheel(address(b)).wheelStep(2, 0);
        assertGt(b.liquidityOf(2), 0);
        assertEq(intent.impacts(), 0); assertEq(intent.lastExecutionAt(), 0);
    }

    function test_pair_minimumBudgetMustSurviveSaleFees() public {
        _install(); intent.setRecipient(1);
        uint256 gross = b.equity();
        uint256 grain = gross / 10000;
        IBuckBasketVenue.Marks memory m = IBuckBasketVenue(address(b)).marks(1, 0);
        uint256 gap = gross * 3333 / 10000 - b.idleToken(1) * m.pTwap / 1e18;
        uint256 requested = grain * 1001 / 1000; // 0.1% margin, smaller than the 0.3% fee.
        intent.setRate(requested * 1e18 / (gap - requested) + 1);
        // Nominal request is just over the 10 BUCK minimum. Its sale fees
        // leave a smaller recipient budget: wait without attempting the swap.
        IEquityWheel(address(b)).wheelStep(2, 0);
        assertEq(intent.impacts(), 0); assertEq(intent.lastExecutionAt(), 0);
        vm.warp(vm.getBlockTimestamp() + 1 days);
        intent.setRate(0.1e18);
        IEquityWheel(address(b)).wheelStep(2, 0);
        assertGt(intent.impacts(), 0, "larger allowance survives execution costs");
        assertEq(intent.lastExecutionAt(), vm.getBlockTimestamp());
        _books();
    }
}
