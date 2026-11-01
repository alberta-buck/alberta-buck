// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckNet} from "../src/BuckNet.sol";
import {JubileeFund, IJubileeBasket} from "../src/JubileeFund.sol";
import {WideCounter} from "../src/lib/WideCounter.sol";
import {NetK} from "./BuckNet.t.sol";
import {JubileeBasketMock} from "./JubileeFund.t.sol";

/// @dev Rebalancing is deliberately exercised independently of settlement.
///      The mock enables exit failures; the real basket has separate tests.
contract JubileeLiquidityRebalanceTest is Test {
    uint256 constant YEAR = 365 days + 6 hours;
    uint256 constant Q = 1e36;
    address constant GOV = address(0xA0);
    address constant KEEPER = address(0xACCE55);
    address constant ALICE = address(0xA11CE);
    address constant INSURER = address(0x1551E1);

    IdentityRegistryHarness identity;
    BuckCredit credit;
    BuckNet buck;
    JubileeFund fund;
    JubileeBasketMock basket;
    uint256 position;

    function setUp() public {
        vm.warp(1_000_000);
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
        _bind(INSURER, false);
        _bind(address(fund), true);
        _bind(address(basket), false);
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER, true);
        vm.prank(GOV);
        fund.configure(buck, IJubileeBasket(address(basket)));
        vm.prank(GOV);
        fund.setInvestmentKeeper(KEEPER);
        vm.prank(INSURER);
        uint256 id = credit.createCredit(ALICE, 0, 1_000_000e6, 1_000_000e6,
                                         BuckCredit.DepreciationType.NONE, 0, 0, 0);
        vm.prank(ALICE);
        buck.issue(id, 1_000_000e6, 1_000_000e6, type(uint256).max, address(0));
        vm.warp(block.timestamp + YEAR);
        // Year-one target is 2,000 BUCK; leave roughly 801 BUCK liquid.
        vm.prank(GOV);
        (position,,) = fund.invest(19_000e6, type(uint256).max, 0, 1);
    }

    function _bind(address target, bool carrying) private {
        if (target.code.length == 0) vm.etch(target, hex"60006000fd");
        identity.bindContract(target, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, carrying);
    }

    function _routes() private pure returns (JubileeFund.SaleRoute[] memory) {
        return new JubileeFund.SaleRoute[](0);
    }

    function test_keeperSellsBoundedSharesAndAccountsForActualNetProceeds() public {
        (uint128 sharesBefore,) = basket.holdings(position);
        (, uint256 cashBefore) = buck.fundBalancesScaled();
        vm.prank(KEEPER);
        uint256 proceeds = fund.redeemScheduled(1_000e6, 999e6, _routes());
        (, uint256 cashAfter) = buck.fundBalancesScaled();
        (uint128 sharesAfter,) = basket.holdings(position);
        assertEq(cashAfter - cashBefore, proceeds);
        assertEq(proceeds, 1_000e6 * Q);
        assertEq(sharesAfter, sharesBefore - 1_000e6);
        assertEq(fund.positionId(), position);
        (, WideCounter.Count memory sales) = fund.investmentFlows();
        assertEq(sales.units * Q + sales.fraction, proceeds);
    }

    function test_reserveAndAuthorizationBoundTheSale() public {
        vm.expectRevert(JubileeFund.NotInvestmentKeeper.selector);
        vm.prank(ALICE);
        fund.redeemScheduled(1, 0, _routes());
        vm.expectRevert(JubileeFund.BadAmount.selector);
        vm.prank(KEEPER);
        fund.redeemScheduled(0, 0, _routes());
        (uint128 shares,) = basket.holdings(position);
        vm.expectRevert(JubileeFund.BadAmount.selector);
        vm.prank(KEEPER);
        fund.redeemScheduled(uint256(shares) + 1, 0, _routes());
        // A larger bounded sale may overshoot: prohibit another sale while
        // cash is at or above target, rather than assuming a share price.
        vm.prank(KEEPER);
        fund.redeemScheduled(2_000e6, 0, _routes());
        vm.expectRevert(JubileeFund.ReserveNotDeficient.selector);
        vm.prank(KEEPER);
        fund.redeemScheduled(1, 0, _routes());
    }

    function test_minimumAndMissingRouteRevertTheEntireSale() public {
        (uint128 sharesBefore,) = basket.holdings(position);
        (, uint256 cashBefore) = buck.fundBalancesScaled();
        vm.expectRevert(JubileeFund.SaleMinOut.selector);
        vm.prank(KEEPER);
        fund.redeemScheduled(1_000e6, 1_001e6, _routes());
        (uint128 sharesAfter,) = basket.holdings(position);
        (, uint256 cashAfter) = buck.fundBalancesScaled();
        assertEq(sharesAfter, sharesBefore);
        assertEq(cashAfter, cashBefore);

        basket.setMode(3);
        basket.inKind().mint(address(basket), 100e18);
        vm.expectRevert(JubileeFund.MissingRoute.selector);
        vm.prank(KEEPER);
        fund.redeemScheduled(1_000e6, 0, _routes());
        (sharesAfter,) = basket.holdings(position);
        assertEq(sharesAfter, sharesBefore);
        assertEq(basket.inKind().balanceOf(address(fund)), 0);
    }

    function test_underwaterExitIsNotBypassedAndZeroProceedsRequireConsent() public {
        (uint128 sharesBefore,) = basket.holdings(position);
        basket.setMode(1);
        vm.prank(KEEPER);
        assertEq(fund.redeemScheduled(1_000e6, 0, _routes()), 0);
        (uint128 sharesAfter,) = basket.holdings(position);
        assertEq(sharesAfter, sharesBefore);
        vm.expectRevert(JubileeFund.SaleMinOut.selector);
        vm.prank(KEEPER);
        fund.redeemScheduled(1_000e6, 1, _routes());

        basket.setMode(3);
        vm.expectRevert(JubileeFund.SaleMinOut.selector);
        vm.prank(KEEPER);
        fund.redeemScheduled(1_000e6, 1, _routes());
        (sharesAfter,) = basket.holdings(position);
        assertEq(sharesAfter, sharesBefore);
        vm.prank(KEEPER);
        assertEq(fund.redeemScheduled(1_000e6, 0, _routes()), 0);
        (sharesAfter,) = basket.holdings(position);
        assertEq(sharesAfter, sharesBefore - 1_000e6);
    }

    function test_partialBasketExitUsesActualSharesAndProceeds() public {
        (uint128 sharesBefore,) = basket.holdings(position);
        basket.setMode(2);
        vm.prank(KEEPER);
        uint256 proceeds = fund.redeemScheduled(1_000e6, 499e6, _routes());
        (uint128 sharesAfter,) = basket.holdings(position);
        assertEq(sharesAfter, sharesBefore - 500e6);
        assertEq(proceeds, 500e6 * Q);
    }
}
