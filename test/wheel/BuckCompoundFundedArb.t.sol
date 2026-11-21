// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckCompoundArbFixture} from "./BuckCompoundWheelArb.t.sol";
import {IBasketArb} from "../../src/basket/IBasketArb.sol";
import {BasketMarketOps} from "../../src/basket/BasketMarketOps.sol";
import {BuckBasketEquity} from "../../src/basket/BuckBasketEquity.sol";
import {BuckBasketStorage} from "../../src/basket/BuckBasketStorage.sol";
import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";
import {BasketMachineWheel} from "../../src/wheel/BasketMachineWheel.sol";
import {EqToken, IPool} from "../basket/BuckBasketEquity.t.sol";
import {IBasketUpkeep} from "../../src/basket/IBasketUpkeep.sol";
import {ArbKind} from "../../src/wheel/ArbKind.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {UniswapV3OracleLib as V3} from "../../src/lib/UniswapV3OracleLib.sol";
import {Vm} from "forge-std/Vm.sol";

contract BuckCompoundFundedArbTest is BuckCompoundArbFixture {
    function _carryingBasket() internal pure override returns (bool) { return true; }
    function setUp() public override {
        super.setUp();
        _configureArb(new BasketMachineWheel(address(buck), address(usdc), GOV, 0, 0));
        // Existing equity, not credit issued for the arb. Every trade leaves
        // the basket's liquidity target untouched and never uses wheel funds.
        vm.prank(ALICE); buck.transfer(address(basket), 100_000e6);
        vm.prank(GOV); wheel.setArb(ub, address(basket), 100, 0);
    }

    function _plan() private view returns (IBasketArb.ArbPlan memory) {
        return IBasketArb(address(basket)).basketArbPlan(0);
    }

    function _execute(IBasketArb.ArbPlan memory p, uint256 minimum)
        private returns (IBasketArb.ArbPlan memory result)
    {
        vm.prank(address(wheel));
        return IBasketArb(address(basket)).executeBasketArb(0, p.direction, p.amount, minimum);
    }

    function _shock(bool buckSale) private {
        _trade(ub, buckSale ? address(buck) : address(usdc), int256(100_000e6));
        IBasketArb.ArbPlan memory p = _plan();
        assertGt(p.amount, 0); assertGt(p.totalReturn, 0);
        uint256 supply = buck.totalIssued();
        uint256 principal = basket.lien();
        uint256 shares = basket.totalShares();
        uint256 reserve = buck.netBalanceOf(address(wheel));
        IBasketArb.ArbPlan memory actual = _execute(p, 2);
        assertGt(actual.totalReturn, 0);
        assertEq(actual.totalReturn, actual.cashReturn + actual.lpReturn);
        assertEq(buck.totalIssued(), supply);
        assertEq(basket.lien(), principal);
        assertEq(basket.totalShares(), shares);
        assertEq(buck.netBalanceOf(address(wheel)), reserve);
        assertGe(buck.netBalanceOf(address(basket)), basket.liquidityTarget());
        assertApproxEqAbs(actual.totalReturn, p.totalReturn, 12);
    }

    function test_fundedCycleRecoversBuckUsdcSaleShock() public { _shock(true); }
    function test_fundedCycleRecoversOppositeBuckUsdcShock() public { _shock(false); }

    function test_agedPoolsUseNetOutputAndNewFeesOnly() public {
        vm.warp(t0 + YEAR);
        _shock(true);
    }

    function test_atomicProfitCanImproveCashAlreadyBelowItsReserveTarget() public {
        uint256 nominal = buck.balanceOf(address(basket));
        vm.prank(address(basket)); buck.transfer(ALICE, nominal - 5e6);
        uint256 beforeNet = buck.netBalanceOf(address(basket));
        assertLt(beforeNet, basket.liquidityTarget());
        _trade(ub, address(buck), int256(100_000e6));
        IBasketArb.ArbPlan memory p = _plan();
        assertGt(p.amount, 0); assertLe(p.amount, 5e6);
        IBasketArb.ArbPlan memory actual = _execute(p, 2);
        assertGt(actual.totalReturn, 0);
        assertGe(buck.netBalanceOf(address(basket)), beforeNet);
    }

    function test_carryingBasketTransportsFeesBetweenAgedAndFreshPools() public {
        vm.warp(t0 + YEAR);
        // Real full-value capital freshens this pool's average fee basis;
        // traffic alone is not assumed to guarantee a particular ordering.
        buck.transfer(ub, 2_000_000e6);
        _trade(ub, address(usdc), int256(100_000e6));
        (uint256 og, uint256 onet) = buck.accountBalancesScaled(own);
        (uint256 ug, uint256 unet) = buck.accountBalancesScaled(ub);
        uint256 oldOwnRatio = Math.mulDiv(onet, Q, og);
        uint256 freshUbRatio = Math.mulDiv(unet, Q, ug);
        assertLt(oldOwnRatio, freshUbRatio);
        IBasketArb.ArbPlan memory p = _plan();
        assertGt(p.amount, 0); assertEq(p.direction, 1);
        vm.recordLogs();
        IBasketArb.ArbPlan memory actual = _execute(p, 2);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bytes32 signature = keccak256("ArbBuckFlow(address,bool,uint256,uint256,uint256,uint256,uint256,uint256)");
        bool sentAged;
        bool receivedAged;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter != address(basket) || logs[i].topics[0] != signature) continue;
            address pool = address(uint160(uint256(logs[i].topics[1])));
            (bool input, uint256 nominal, uint256 net, uint256 wb, uint256 wa, uint256 pb, uint256 pa) =
                abi.decode(logs[i].data, (bool, uint256, uint256, uint256, uint256, uint256, uint256));
            assertGt(nominal, 0); assertLt(net, nominal * Q);
            assertLt(wb, Q); assertLt(wa, Q);
            if (pool == ub && input) {
                assertLt(wb, pb); assertLt(pa, pb);
                sentAged = true;
            }
            if (pool == own && !input) {
                assertApproxEqAbs(pb, oldOwnRatio, 2);
                assertApproxEqAbs(pa, oldOwnRatio, 2);
                receivedAged = true;
            }
        }
        assertTrue(sentAged && receivedAged);
        assertGt(actual.totalReturn, 0);
    }

    function test_normalWheelTickUsesFundedBasketRoiAndKeepsTheReserveSeparate() public {
        _trade(ub, address(buck), int256(100_000e6));
        IBasketArb.ArbPlan memory p = _plan();
        (uint256 input, uint8 dir, uint256 profit) = wheel.plan(0);
        assertEq(input, p.amount); assertEq(dir, p.direction);
        assertEq(profit, uint256(p.totalReturn));
        uint256 supply = buck.totalIssued();
        uint256 reserve = buck.netBalanceOf(address(wheel));
        (uint256 work,) = wheel.tick(1, 0);
        assertEq(work, 1);
        assertEq(buck.totalIssued(), supply);
        assertEq(buck.netBalanceOf(address(wheel)), reserve);
    }

    function _cross(bool up) private {
        EqToken second = new EqToken("Second", "TWO", 18);
        second.mint(address(this), 4_000_000e18);
        second.mint(ALICE, 100_000e18);
        vm.prank(GOV);
        address own2 = basket.addBasketToken(address(second), 18, 1e6, 5000, 3000);
        _bind(own2, true, address(this));
        (,,,,,, int24 lo, int24 hi,,,) = basket.constituents(1);
        IPool(own2).mint(address(this), lo, hi, 1e18, "");
        address usd2 = _pool(address(second), 18, address(usdc), 6, 3000);
        _mintPool(usd2, 60, 1e18);
        vm.warp(block.timestamp + 601);
        vm.startPrank(ALICE);
        second.approve(address(basket), type(uint256).max);
        basket.deposit(address(second), 100_000e18, 0);
        buck.transfer(address(basket), 100_000e6);
        vm.stopPrank();
        vm.warp(block.timestamp + 1800);                 // the response interval since setUp's
        vm.prank(address(wheel));
        assertEq(IBasketUpkeep(address(basket)).wheelStep(2, 0), 1);
        vm.prank(GOV);
        wheel.setTriangle(1, ArbKind.Triangle(address(second), own2, usd2, 1));
        assertEq(BasketMachineWheel(address(wheel)).arbRouteCount(), 3);
        _trade(usd2, up ? address(usdc) : address(second), int256(up ? uint256(100_000e6) : uint256(100_000e18)));
        IBasketArb.ArbPlan memory p = IBasketArb(address(basket)).basketArbPlan(2);
        assertGt(p.amount, 0); assertGt(p.totalReturn, 0);
        uint256 supply = buck.totalIssued();
        vm.prank(address(wheel));
        IBasketArb.ArbPlan memory actual = IBasketArb(address(basket)).executeBasketArb(2, p.direction, p.amount, 2);
        assertGt(actual.totalReturn, 0);
        assertEq(buck.totalIssued(), supply);
        assertApproxEqAbs(actual.totalReturn, p.totalReturn, 20);
    }

    function test_fourPoolCrossTokenCycle() public { _cross(true); }
    function test_reverseFourPoolCrossTokenCycle() public { _cross(false); }

    function _alignUb() private {
        (, int24 ownTick,,,,,) = IUniswapV3Pool(own).slot0();
        (, int24 marketTick,,,,,) = IUniswapV3Pool(market).slot0();
        uint256 b = V3.getQuoteAtTick(ownTick, 1e18, address(asset), address(buck));
        uint256 u = V3.getQuoteAtTick(marketTick, 1e18, address(asset), address(usdc));
        bool buck0 = IUniswapV3Pool(ub).token0() == address(buck);
        uint160 target = uint160(Math.sqrt(Math.mulDiv(buck0 ? u : b, 2 ** 192, buck0 ? b : u)));
        (uint160 current,,,,,,) = IUniswapV3Pool(ub).slot0();
        if (target != current) IUniswapV3Pool(ub).swap(address(this), target < current, int256(1_000_000e6), target, "");
        vm.warp(block.timestamp + 601);
    }

    function test_positiveBasketReturnCanExecuteWithADirectCashLoss() public {
        // Search the narrow external fee band after nominal parity. No new
        // LP ownership or optimistic fee rebate is fabricated.
        _alignUb();
        bool found;
        for (uint256 i; i < 32; i++) {
            uint256 snapshot = vm.snapshotState();
            _trade(ub, address(buck), int256((3100 + i * 10) * 1e6));
            IBasketArb.ArbPlan memory p = _plan();
            if (p.amount != 0 && p.cashReturn < 0 && p.totalReturn > 0) {
                IBasketArb.ArbPlan memory actual = _execute(p, 2);
                assertLt(actual.cashReturn, 0);
                assertGt(actual.lpFees, 0);
                assertGt(actual.totalReturn, 0);
                assertEq(actual.cashReturn + actual.lpReturn, actual.totalReturn);
                found = true;
                break;
            }
            vm.revertToState(snapshot);
        }
        assertTrue(found, "expected a funded, fee-supported basket return");
    }

    function test_onlyTheConfiguredWheelCanSpend() public {
        vm.expectRevert(BuckBasketStorage.NotWheel.selector);
        IBasketArb(address(basket)).executeBasketArb(0, 0, 100e6, 2);
    }

    function test_minimumGainRefusalPreservesAllMoneyAndPositions() public {
        _trade(ub, address(buck), int256(100_000e6));
        IBasketArb.ArbPlan memory p = _plan();
        uint256 cash = buck.balanceOf(address(basket));
        uint256 tokens = asset.balanceOf(address(basket));
        uint128 liquidity = basket.liquidityOf(0);
        vm.expectRevert(BasketMarketOps.NoPortfolioGain.selector);
        _execute(p, 1_000_000e6);
        assertEq(buck.balanceOf(address(basket)), cash);
        assertEq(asset.balanceOf(address(basket)), tokens);
        assertEq(basket.liquidityOf(0), liquidity);
    }

    function test_zeroAndOverBudgetInputsCannotBorrowOrExceedActualCash() public {
        vm.expectRevert(BasketMarketOps.InvalidArbRoute.selector);
        vm.prank(address(wheel));
        IBasketArb(address(basket)).executeBasketArb(0, 0, 0, 0);
        vm.expectRevert(BasketMarketOps.ArbBudget.selector);
        vm.prank(address(wheel));
        IBasketArb(address(basket)).executeBasketArb(0, 0, 1_000_000e6, 0);
    }

    function test_sharedLibraryAndExistingFacetsFitRuntimeLimit() public {
        BasketUpkeepFacet machine = new BasketUpkeepFacet(address(0), address(0), 0, 0);
        assertLe(address(machine).code.length, 24_576);
        assertLe(address(basket).code.length, 24_576);
        assertLe(address(BasketMarketOps).code.length, 24_576);
    }
}
