// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckNet} from "../src/BuckNet.sol";
import {JubileeFund, IJubileeBasket} from "../src/JubileeFund.sol";
import {BuckLedger} from "../src/lib/BuckLedger.sol";
import {WideCounter} from "../src/lib/WideCounter.sol";
import {NetK} from "./BuckNet.t.sol";
import {JubileeBasketMock} from "./JubileeFund.t.sol";

contract JubileeMarkedReliefTest is Test {
    uint256 constant YEAR = 365 days + 6 hours;
    uint256 constant Q = 1e36;
    address constant GOV = address(0xA0);
    address constant ALICE = address(0xA11CE);
    address constant BOB = address(0xB0B);
    address constant INSURER = address(0x1551E1);
    IdentityRegistryHarness identity;
    BuckCredit credit;
    BuckNet buck;
    JubileeFund fund;
    JubileeBasketMock basket;
    uint256 t0;

    function setUp() public {
        vm.warp(1_000_000);
        t0 = block.timestamp;
        identity = new IdentityRegistryHarness(GOV);
        credit = new BuckCredit();
        NetK k = new NetK();
        fund = new JubileeFund(GOV);
        buck = new BuckNet(address(credit), address(k), address(identity), address(fund));
        basket = new JubileeBasketMock(address(buck));
        credit.setBuck(address(buck));
        vm.prank(GOV);
        identity.setBuck(address(buck));
        _bind(ALICE, false);
        _bind(BOB, false);
        _bind(INSURER, false);
        _bind(address(fund), true);
        _bind(address(basket), false);
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER, true);
        vm.prank(BOB);
        credit.setCreditIssuer(INSURER, true);
        vm.prank(GOV);
        fund.configure(buck, IJubileeBasket(address(basket)));
    }

    function _bind(address target, bool carrying) private {
        if (target.code.length == 0) vm.etch(target, hex"60006000fd");
        identity.bindContract(target, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, carrying);
    }

    function _credit(address owner, uint256 amount) private returns (uint256 id) {
        vm.prank(INSURER);
        id = credit.createCredit(owner, 0, amount, amount, BuckCredit.DepreciationType.NONE, 0, 0, 0);
        vm.prank(owner);
        buck.issue(id, amount, amount);
    }

    function _authorize(uint256 id, uint256 amount, uint256 minimum) private {
        vm.prank(ALICE);
        buck.authorizeReduction(id, amount, minimum, uint64(block.timestamp + YEAR));
    }

    function test_exactTenPercentCashBoundaryAndOneRawUnitAboveIt() public {
        uint256 id = _credit(ALICE, 2_000_000e6);
        _credit(BOB, 8_000_000e6);
        vm.warp(t0 + YEAR);
        (, uint256 cash) = buck.fundBalancesScaled();
        (uint256 quote,, bool over) = fund.previewRelief(id, 1_000_000e6);
        assertEq(quote, cash / 10);
        assertFalse(over);
        _authorize(id, 1_000_000e6 + 1, 0);
        (uint256 tooMuch,, bool tooLarge) = fund.previewRelief(id, 1_000_000e6 + 1);
        assertGt(tooMuch, cash / 10);
        assertTrue(tooLarge);
        vm.expectRevert(JubileeFund.ReliefLiquidityLimit.selector);
        vm.prank(ALICE);
        fund.reduce(id, 1_000_000e6 + 1);
        (uint256 principal,,) = buck.reductionAuthorization(id);
        assertEq(principal, 1_000_000e6 + 1);
        _authorize(id, 1_000_000e6, quote);
        vm.prank(ALICE);
        (uint256 paid,) = fund.reduce(id, 1_000_000e6);
        assertEq(paid, quote);
    }

    function test_markedGainAndCarriedCashDetermineReliefWithoutSelling() public {
        uint256 id = _credit(ALICE, 100e6);
        _credit(BOB, 1900e6);
        vm.warp(t0 + YEAR);
        vm.prank(GOV);
        (uint256 receipt,,) = fund.invest(20e6, type(uint256).max, 0, 1);
        vm.prank(BOB);
        buck.transfer(address(basket), 10e6);
        basket.setMode(4); // Any attempt to sell would revert.
        (uint128 shares, uint128 basis) = basket.holdings(receipt);
        uint256 mark = basket.valueOf(receipt);
        assertGt(mark, basis);
        uint256 afterGainFee = mark - (mark - basis) / 4;
        (, uint256 cash) = buck.fundBalancesScaled();
        uint256 expected = Math.mulDiv(cash + afterGainFee * Q, 1, 40);
        (uint256 quote,, bool over) = fund.previewRelief(id, 50e6);
        assertEq(quote, expected);
        assertFalse(over);
        _authorize(id, 50e6, quote);
        vm.prank(ALICE);
        (uint256 paid,) = fund.reduce(id, 50e6);
        assertEq(paid, quote);
        (uint128 afterShares,) = basket.holdings(receipt);
        assertEq(afterShares, shares);
        assertEq(fund.positionId(), receipt);
    }

    function test_minimumReliefAndEmptyClaimAreHonored() public {
        uint256 id = _credit(ALICE, 100e6);
        _credit(BOB, 900e6);
        vm.warp(t0 + YEAR);
        (uint256 quote,,) = fund.previewRelief(id, 50e6);
        _authorize(id, 50e6, quote + 1);
        vm.expectRevert(BuckNet.BadAuthorization.selector);
        vm.prank(ALICE);
        fund.reduce(id, 50e6);
        (uint256 authorized,,) = buck.reductionAuthorization(id);
        assertEq(authorized, 50e6);

        uint256 fresh = _credit(ALICE, 1e6);
        (uint256 freshQuote,, bool over) = fund.previewRelief(fresh, 1e6);
        assertEq(freshQuote, 0);
        assertFalse(over);
        _authorize(fresh, 1e6, 0);
        vm.prank(ALICE);
        (uint256 paid,) = fund.reduce(fresh, 1e6);
        assertEq(paid, 0);
    }

    function test_markedFundPremiumAndLossMatchTheSpecifiedEffectiveRates() public {
        uint256 id = _credit(ALICE, 1_000_000e6);
        _credit(BOB, 99_000_000e6);
        vm.warp(t0 + 5 * YEAR);
        (, uint256 cash) = buck.fundBalancesScaled();
        uint256 desired = 10_000_000e6 * Q;
        uint256 donation = Math.ceilDiv(desired - cash, Q);
        vm.prank(BOB);
        buck.transfer(address(fund), donation);
        vm.prank(GOV);
        fund.invest(9_000_000e6, type(uint256).max, 0, 1);
        (uint256 baseline,, bool over) = fund.previewRelief(id, 200_000e6);
        assertFalse(over);
        assertApproxEqAbs(baseline / Q, 20_000e6, 3);

        uint256 snapshot = vm.snapshotState();
        // A 333,333.333334 gross gain less the basket's 25% gain fee
        // increases the net marked fund value by about 250,000 BUCK.
        vm.prank(BOB);
        buck.transfer(address(basket), 333_333_333_334);
        (uint256 gain,,) = fund.previewRelief(id, 200_000e6);
        assertApproxEqAbs(gain / Q, 20_500e6, 3);
        assertTrue(vm.revertToState(snapshot));
        basket.realizeLoss(BOB, 500_000e6);
        (uint256 loss,,) = fund.previewRelief(id, 200_000e6);
        assertApproxEqAbs(loss / Q, 19_000e6, 3);
    }

    function test_issuanceAndReductionCheckpointAccruedFeesExactlyOnce() public {
        uint256 id = _credit(ALICE, 1000e6);
        vm.warp(t0 + YEAR);
        BuckLedger.FundFlows memory first = buck.fundFlows();
        assertEq(_scaled(first.grossDemurrage), 20e6 * Q);
        _credit(BOB, 1000e6);
        BuckLedger.FundFlows memory afterIssuance = buck.fundFlows();
        assertEq(_scaled(afterIssuance.grossDemurrage), 20e6 * Q);
        vm.warp(t0 + 2 * YEAR);
        BuckLedger.FundFlows memory beforeReduction = buck.fundFlows();
        assertEq(_scaled(beforeReduction.grossDemurrage), 60e6 * Q);
        (, uint256 cashBefore) = buck.fundBalancesScaled();
        _authorize(id, 50e6, 0);
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduce(id, 50e6);
        BuckLedger.FundFlows memory afterReduction = buck.fundFlows();
        (, uint256 cashAfter) = buck.fundBalancesScaled();
        assertEq(_scaled(afterReduction.grossDemurrage), 60e6 * Q);
        assertEq(_scaled(afterReduction.relief), relief);
        assertEq(cashAfter, cashBefore - relief);
        vm.warp(t0 + 3 * YEAR);
        BuckLedger.FundFlows memory nextYear = buck.fundFlows();
        assertEq(_scaled(nextYear.grossDemurrage), 99e6 * Q);
    }

    function test_smallDailyRepaymentsUseAccruedCashWhileKeeperInvestsExcess() public {
        uint256 id = _credit(ALICE, 1000e6);
        _credit(BOB, 9000e6);
        vm.warp(t0 + YEAR);
        fund.checkpointReserve();
        BuckLedger.FundFlows memory start = buck.fundFlows();
        basket.setMode(4); // A basket redemption would revert.
        for (uint256 day = 1; day <= 30; ++day) {
            vm.warp(t0 + YEAR + day * 1 days);
            _credit(BOB, 10e6);
            _authorize(id, 1e6, 0);
            vm.prank(ALICE);
            fund.reduce(id, 1e6);
            vm.prank(GOV);
            fund.investScheduled(type(uint256).max, 0, 1);
        }
        assertEq(buck.totalIssued(), 10_270e6);
        BuckLedger.FundFlows memory end = buck.fundFlows();
        uint256 incoming = _scaled(end.netDemurrage) - _scaled(start.netDemurrage);
        uint256 relief = _scaled(end.relief) - _scaled(start.relief);
        assertGt(incoming, relief);
        (WideCounter.Count memory invested, WideCounter.Count memory sold) = fund.investmentFlows();
        assertGt(_scaled(invested), 0);
        assertEq(_scaled(sold), 0);
        assertGt(fund.positionId(), 0);
    }

    function _scaled(WideCounter.Count memory count) private pure returns (uint256) {
        return count.units * Q + count.fraction;
    }
}
