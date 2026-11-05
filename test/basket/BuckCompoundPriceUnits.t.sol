// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckCompoundDeskFixture} from "./BuckCompoundDesk.t.sol";
import {BuckKControllerDirect} from "../../src/BuckKControllerDirect.sol";
import {IBuckBasketVenue} from "../../src/basket/IBuckBasketVenue.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IPool} from "./BuckBasketEquity.t.sol";

/// @dev Real pools, canonical cash and the existing desk. These vectors
///      distinguish the nominal issuance reference from liquidation value.
///      The PID observes this basket independently; this is a unit/denomination
///      check, not a closed-loop economy or a controller-gain comparison.
contract BuckCompoundPriceUnitsTest is BuckCompoundDeskFixture {
    function test_idleFeesChangeNetCollateralButNotTheNominalReferenceOrPidInput() public {
        _grant(1000e18);
        BuckKControllerDirect observer = new BuckKControllerDirect(
            1e12, 0, 0, 1, 0.1e18, 0.95e18, 0.75e18, GOV);
        vm.prank(GOV); observer.setBasket(address(basket));
        vm.warp(block.timestamp + 1);
        uint256 output = observer.compute();
        int256 nominalReference = IBuckBasketVenue(address(basket)).basketValueInBuck();
        int256 process = observer.lastBasketCost();
        uint256 collateral = desk.collateralValue();
        uint256 supply = buck.totalIssued();
        (uint256 gross, uint256 net) = buck.accountBalancesScaled(pool);

        vm.warp(block.timestamp + YEAR);
        (uint256 afterGross, uint256 afterNet) = buck.accountBalancesScaled(pool);
        assertEq(afterGross, gross);
        assertLt(afterNet, net);
        assertLt(desk.collateralValue(), collateral);
        assertEq(IBuckBasketVenue(address(basket)).basketValueInBuck(), nominalReference);
        assertEq(observer.compute(), output); // P-only loop also isolates tick quantization.
        assertEq(observer.lastBasketCost(), process);
        assertEq(buck.totalIssued(), supply);
        emit log_named_uint("idle pool net/nominal ratio (1e18)", Math.mulDiv(afterNet, 1e18, afterGross));
    }

    function test_freshNetIssuanceUsesNominalTicksAfterTradingCosts() public {
        vm.warp(block.timestamp + YEAR);
        uint256 nominalPrice = IBuckBasketVenue(address(desk)).marks(0, 0).pTwap;
        uint256 principal = _issue();
        uint256 tokenOut = desk.monetaryTokenHeld(0) - 1000e18;
        uint256 nominalValue = Math.mulDiv(tokenOut, nominalPrice, 1e18);
        // Full-value ordinary BUCK enter the pool as that same nominal input.
        // AMM fees and price impact reduce the TOKEN actually delivered.
        assertLt(nominalValue, principal);
        assertGt(nominalValue, principal * 99 / 100);
        assertEq(buck.netBalanceOf(address(desk)), 0);
        emit log_named_uint("full-value principal sold (raw)", principal);
        emit log_named_uint("exported TOKEN at nominal TWAP (raw)", nominalValue);
        _assertAccount();
    }

    function test_buybackCostIncludesThePoolsCarriedFeesAndSwapCosts() public {
        _grant(1000e18);
        vm.warp(block.timestamp + YEAR);
        uint256 nominalPrice = IBuckBasketVenue(address(desk)).marks(0, 0).pTwap;
        (uint256 gross, uint256 net) = buck.accountBalancesScaled(pool);
        uint256 beforeNominal = buck.balanceOf(pool);
        policy.set(-50, false);
        assertEq(desk.monetaryOperation(), 1);
        uint256 spentToken = 1000e18 - desk.monetaryTokenHeld(0);
        uint256 cost = Math.mulDiv(spentToken, nominalPrice, 1e18);
        uint256 nominalBought = beforeNominal - buck.balanceOf(pool);
        uint256 netBought = buck.netBalanceScaled(address(desk));
        assertGt(cost, nominalBought); // AMM fee and price impact.
        assertGt(nominalBought * Q, netBought); // Pool's average carried fee.
        assertApproxEqAbs(netBought, Math.mulDiv(net, nominalBought * Q, gross), Q);
        assertGt(cost, netBought / Q);
        emit log_named_uint("buyback TOKEN cost at nominal TWAP (raw)", cost);
        emit log_named_uint("nominal BUCK bought (raw)", nominalBought);
        emit log_named_uint("net spendable BUCK bought (raw)", netBought / Q);
        _assertAccount();
    }

    function test_freshPairedLiquidityDilutesCarriedFeesWithoutMovingNominalPriceOrSupply() public {
        _grant(1000e18);
        vm.warp(block.timestamp + YEAR);
        int256 nominalReference = IBuckBasketVenue(address(basket)).basketValueInBuck();
        uint256 collateral = desk.collateralValue();
        (uint256 gross, uint256 net) = buck.accountBalancesScaled(pool);
        uint256 supply = buck.totalIssued();
        (,,,,,, int24 lower, int24 upper,,,) = basket.constituents(0);
        // The ordinary fixture pays both sides at full value using existing
        // currency. This is a real LP increase, not a donation or token mint.
        IPool(pool).mint(address(this), lower, upper, 1e17, "");
        (uint256 afterGross, uint256 afterNet) = buck.accountBalancesScaled(pool);
        assertGt(afterGross, gross);
        assertApproxEqAbs(afterGross - gross, afterNet - net, Q);
        assertGt(Math.mulDiv(afterNet, 1e18, afterGross), Math.mulDiv(net, 1e18, gross));
        assertGt(desk.collateralValue(), collateral);
        assertEq(IBuckBasketVenue(address(basket)).basketValueInBuck(), nominalReference);
        assertEq(buck.totalIssued(), supply);
        emit log_named_uint("before LP net/nominal ratio (1e18)", Math.mulDiv(net, 1e18, gross));
        emit log_named_uint("after LP net/nominal ratio (1e18)", Math.mulDiv(afterNet, 1e18, afterGross));
    }
}
