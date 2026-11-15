// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {JubileeEquityFixture} from "./JubileeEquityIntegration.t.sol";
import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";
import {BasketVenueFacet} from "../../src/basket/BasketVenueFacet.sol";
import {IBasketUpkeep} from "../../src/basket/IBasketUpkeep.sol";
import {ControlledPairIntent} from "./BasketUpkeepFacet.t.sol";
import {EqToken, IPool} from "./BuckBasketEquity.t.sol";

contract SinglePoolIntent is ControlledPairIntent {
    constructor(address basket) ControlledPairIntent(basket) {}
    function count() external pure override returns (uint256) { return 1; }
}

/// @dev Actual, unmodified equity shell: public opening, upkeep issuance,
///      real V3 liquidity, actual debt retirement and investor exit. This
///      fixture's registry supplies flags; production credential/binding
///      authorization is separately exercised in BasketCreditSetupTest.
contract BasketPublicLifecycleTest is JubileeEquityFixture {
    function _lifecycle(bool machine) private {
        _bind(address(this), false, address(this));
        _credit(ALICE, 2_000_000e6);
        vm.prank(ALICE); buck.transfer(address(this), 1_500_000e6);
        EqToken asset = new EqToken("Asset", "AST", 18);
        asset.mint(address(this), 2_000_000e18); asset.mint(ALICE, 1000e18);
        vm.prank(GOV);
        address pool = basket.addBasketToken(address(asset), 18, 1e6, 10_000, 3000);
        _bind(pool, true, address(this));
        (,,,,,, int24 lo, int24 hi,,,) = basket.constituents(0);
        IPool(pool).mint(address(this), lo, hi, 1e18, "");
        vm.warp(t0 + 601);
        k.setK(0.75e18);
        vm.startPrank(ALICE);
        asset.approve(address(basket), type(uint256).max);
        uint256 receipt = basket.deposit(address(asset), 1000e18, 0);
        vm.stopPrank();
        if (machine) {
            ControlledPairIntent intent = new SinglePoolIntent(address(basket)); intent.setRate(0);
            vm.startPrank(GOV);
            basket.setVenue(address(new BasketVenueFacet(address(0))));
            basket.setEquityWheel(address(new BasketUpkeepFacet(address(intent), address(0), 0, 0)));
            vm.stopPrank();
        }
        uint256 supply = buck.totalIssued();
        vm.startPrank(GOV);
        basket.setNetMark(true);
        basket.openCredit(address(credit), 1e15);
        vm.stopPrank();
        assertEq(buck.totalIssued(), supply); assertEq(basket.lien(), 0);
        assertEq(IBasketUpkeep(address(basket)).wheelStep(2, 0), 1);
        uint256 principal = basket.lien();
        assertGt(principal, 0); assertGt(basket.liquidityOf(0), 0);
        assertEq(buck.totalIssued(), supply + principal);
        (, uint256 active, uint32 rate) = credit.creditInfo(basket.creditId());
        assertGt(active, 0); assertEq(rate, 0);
        // Funding a later repayment is an explicit donation, not automatic
        // principal retirement and not another measured investor deposit.
        vm.warp(vm.getBlockTimestamp() + YEAR);
        vm.prank(ALICE); buck.transfer(address(basket), principal);
        assertEq(basket.lien(), principal);
        vm.prank(GOV); basket.retireCredit(principal, principal, 0);
        assertEq(basket.lien(), 0); assertEq(buck.totalIssued(), supply);
        assertFalse(basket.creditLive());
        (uint128 shares,) = basket.holdings(receipt);
        vm.prank(ALICE);
        (uint256 remaining,, uint256 retired, uint256 sharesLeft) =
            basket.redeemShares(receipt, shares, 1);
        assertGt(retired, 0); assertEq(remaining, 0); assertEq(sharesLeft, 0);
    }

    function test_publicComponentBasketIssuesRetiresAndExitsThroughRealV3() public { _lifecycle(false); }
    function test_publicMachineBasketUsesTheSameCreditLifecycle() public { _lifecycle(true); }
}
