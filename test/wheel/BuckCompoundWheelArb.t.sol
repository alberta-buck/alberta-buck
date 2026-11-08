// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {JubileeEquityFixture} from "../basket/JubileeEquityIntegration.t.sol";
import {EqToken, IPool} from "../basket/BuckBasketEquity.t.sol";
import {IBasketUpkeep} from "../../src/basket/IBasketUpkeep.sol";
import {ArbKind} from "../../src/wheel/ArbKind.sol";
import {BasketWheel} from "../../src/wheel/BasketWheel.sol";
import {BasketMachineWheel} from "../../src/wheel/BasketMachineWheel.sol";

/// @dev Canonical unsigned Buck, actual NFT issuance and public basket LP
///      placement, with real V3 pools. The inherited identity harness supplies
///      flags; production wheel identity/keeper tests are separate. This is
///      qualification of the existing direct-profit executor, not yet the
///      fee-aware, funded basket-ROI planner or household economy acceptance.
contract BuckCompoundWheelArbTest is JubileeEquityFixture {
    EqToken asset;
    EqToken usdc;
    BasketMachineWheel wheel;
    address own;
    address market;
    address ub;

    function setUp() public override {
        super.setUp();
        _bind(address(this), false, address(this));
        _credit(ALICE, 8_000_000e6);
        vm.prank(ALICE); buck.transfer(address(this), 6_000_000e6);
        asset = new EqToken("Asset", "AST", 18);
        usdc = new EqToken("USD Coin", "USDC", 6);
        asset.mint(address(this), 8_000_000e18);
        asset.mint(ALICE, 100_000e18);
        usdc.mint(address(this), 20_000_000e6);
        vm.prank(GOV);
        own = basket.addBasketToken(address(asset), 18, 1e6, 10_000, 3000);
        _bind(own, true, address(this));
        (,,,,,, int24 lo, int24 hi,,,) = basket.constituents(0);
        IPool(own).mint(address(this), lo, hi, 1e18, "");
        market = _pool(address(asset), 18, address(usdc), 6, 3000);
        ub = _pool(address(buck), 6, address(usdc), 6, 500);
        _bind(ub, true, address(this));
        // Bind before funding BUCK liquidity; all Carrying mode changes are
        // explicit. _pool initializes only; the helper below mints the pair.
        _mintPool(market, 60, 1e18);
        _mintPool(ub, 10, 1_000_000e6);

        vm.warp(t0 + 601);
        k.setK(0.75e18);
        vm.startPrank(ALICE);
        asset.approve(address(basket), type(uint256).max);
        basket.deposit(address(asset), 100_000e18, 0);
        vm.stopPrank();
        vm.startPrank(GOV);
        basket.setNetMark(true);
        basket.openCredit(address(credit), 1e15);
        vm.stopPrank();
        assertEq(IBasketUpkeep(address(basket)).wheelStep(2, 0), 1);
        assertGt(basket.liquidityOf(0), 0);

        wheel = new BasketMachineWheel(address(buck), address(usdc), GOV, 0, 0);
        _bind(address(wheel), true, address(this));
        vm.startPrank(GOV);
        (bool ok,) = address(basket).call(abi.encodeWithSignature("setWheel(address)", address(wheel)));
        require(ok);
        wheel.setArb(ub, address(basket), 0, 100, 1);
        wheel.setTriangle(0, ArbKind.Triangle(address(asset), own, market, 0));
        vm.stopPrank();
    }

    function _pool(address a, uint8 da, address b, uint8 db, uint24 fee)
        private returns (address p)
    {
        p = basket.v3Factory().createPool(a, b, fee);
        bool a0 = IUniswapV3Pool(p).token0() == a;
        (uint256 num, uint256 den) = a0
            ? (10 ** uint256(db), 10 ** uint256(da))
            : (10 ** uint256(da), 10 ** uint256(db));
        IUniswapV3Pool(p).initialize(uint160(Math.sqrt(Math.mulDiv(num, 2 ** 192, den))));
    }

    function _mintPool(address p, int24 spacing, uint128 liquidity) private {
        int24 top = (887272 / spacing) * spacing;
        IUniswapV3Pool(p).mint(address(this), -top, top, liquidity, "");
    }

    function _trade(address pool, address input, int256 amount) private {
        bool zeroForOne = IUniswapV3Pool(pool).token0() == input;
        IUniswapV3Pool(pool).swap(address(this), zeroForOne, amount,
            zeroForOne ? MIN_SQRT + 1 : MAX_SQRT - 1, "");
    }

    function _shockAndTick(bool buckSale) private {
        _trade(ub, buckSale ? address(buck) : address(usdc), int256(100_000e6));
        (uint256 input,, uint256 quoted) = wheel.plan(0);
        assertGt(input, 0);
        uint256 supply = buck.totalIssued();
        uint256 principal = basket.lien();
        uint256 tokens = asset.balanceOf(address(basket));
        (uint256 work,) = wheel.tick(1, 0);
        assertEq(work, 1);
        assertGt(asset.balanceOf(address(basket)), tokens);
        assertEq(asset.balanceOf(address(wheel)), 0);
        assertEq(buck.totalIssued(), supply);
        assertEq(basket.lien(), principal);
        // The input cap itself can grow as a pool's input reserve grows.
        // Remaining profit, rather than input size, measures recovery here.
        (,, uint256 smaller) = wheel.plan(0);
        assertLt(smaller, quoted);
    }

    function test_buckUsdcWithdrawalShockIsArbitragedWithoutNewIssuance() public {
        _shockAndTick(true);
    }

    function test_oppositeBuckUsdcShockIsArbitragedWithoutCallerReward() public {
        _shockAndTick(false);
    }

    function test_poolCallbackAuthorityEndsWhenTheCycleEnds() public {
        _shockAndTick(true);
        bytes memory callback = abi.encode(uint8(0));
        // No constituent pool retains authority outside an active swap.
        address[3] memory pools = [own, market, ub];
        for (uint256 i; i < pools.length; i++) {
            vm.expectRevert(ArbKind.BadCallback.selector);
            vm.prank(pools[i]);
            wheel.uniswapV3SwapCallback(1, 0, callback);
        }
    }

    function _growth(address token) private view returns (uint256) {
        IUniswapV3Pool p = IUniswapV3Pool(own);
        return p.token0() == token ? p.feeGrowthGlobal0X128() : p.feeGrowthGlobal1X128();
    }

    function _sync() private {
        vm.prank(address(wheel));
        assertEq(IBasketUpkeep(address(basket)).wheelStep(1, 0), 1);
    }

    function _checkCollectedFeeShare(bool protocol) private {
        vm.warp(t0 + YEAR);
        _sync(); // Exclude any earlier uncollected income from this cycle.
        if (protocol) IUniswapV3Pool(own).setFeeProtocol(4, 4);
        _trade(ub, address(buck), int256(100_000e6));
        uint256 buckGrowth = _growth(address(buck));
        uint256 tokenGrowth = _growth(address(asset));
        uint256 liquidity = basket.liquidityOf(0);
        uint256 totalLiquidity = IUniswapV3Pool(own).liquidity();
        assertLt(liquidity, totalLiquidity); // The seed LP also earns fees.
        uint256 supply = buck.totalIssued();
        (uint256 work,) = wheel.tick(1, 0);
        assertEq(work, 1);
        // Full-range liquidity is unchanged and active throughout this cycle.
        uint256 db = _growth(address(buck)) - buckGrowth;
        uint256 dt = _growth(address(asset)) - tokenGrowth;
        uint256 nominalBuckFee = Math.mulDiv(db, liquidity, 2 ** 128);
        uint256 tokenFee = Math.mulDiv(dt, liquidity, 2 ** 128);
        uint256 allLpBuckFee = Math.mulDiv(db, totalLiquidity, 2 ** 128);
        uint256 allLpTokenFee = Math.mulDiv(dt, totalLiquidity, 2 ** 128);
        assertTrue(nominalBuckFee > 0 || tokenFee > 0);
        if (nominalBuckFee > 0) assertLt(nominalBuckFee, allLpBuckFee);
        if (tokenFee > 0) assertLt(tokenFee, allLpTokenFee);
        (uint256 gross, uint256 net) = buck.accountBalancesScaled(own);
        assertLt(net, gross);
        uint256 netBuckFee = Math.mulDiv(nominalBuckFee, net, gross);
        uint256 cashBefore = buck.netBalanceOf(address(basket));
        uint256 tokenBefore = asset.balanceOf(address(basket));
        _sync();
        assertApproxEqAbs(buck.netBalanceOf(address(basket)) - cashBefore, netBuckFee, 1);
        assertApproxEqAbs(asset.balanceOf(address(basket)) - tokenBefore, tokenFee, 1);
        assertEq(buck.totalIssued(), supply);
        if (protocol) {
            (uint128 p0, uint128 p1) = IUniswapV3Pool(own).protocolFees();
            assertGt(uint256(p0) + p1, 0); // Excluded from fee-growth/LP income.
        }
    }

    function test_basketEarnsOnlyItsActualLpShareNetOfCarriedDemurrage() public {
        _checkCollectedFeeShare(false);
    }

    function test_protocolFeesAreNotBasketIncome() public {
        _checkCollectedFeeShare(true);
    }

    function test_carryingPoolMixChangesTheValueOfEqualNominalOutput() public {
        // Equal exact-output purchases can deliver different full-value BUCK
        // at identical nominal prices. Adding fresh cash changes the fee
        // basis, not V3's price/liquidity or the exact-output nominal amount.
        vm.warp(t0 + 10 * YEAR);
        uint256 snapshot = vm.snapshotState();
        uint256 before = buck.netBalanceOf(address(this));
        _trade(ub, address(usdc), -int256(100e6));
        uint256 aged = buck.netBalanceOf(address(this)) - before;
        assertLt(aged, 100e6);
        vm.revertToState(snapshot);
        vm.prank(ALICE); buck.transfer(ub, 500_000e6);
        before = buck.netBalanceOf(address(this));
        _trade(ub, address(usdc), -int256(100e6));
        uint256 diluted = buck.netBalanceOf(address(this)) - before;
        assertGt(diluted, aged);
        assertLt(diluted, 100e6);
    }

    function test_nominalTokenProfitCanConsumeNetBuckInventory() public {
        // Characterization of the still-disabled direct-profit executor:
        // its TOKEN gain alone is not a basket/wheel portfolio return.
        vm.warp(t0 + 10 * YEAR);
        vm.prank(ALICE); buck.transfer(address(wheel), 1_000_000e6);
        uint256 grossBefore = buck.balanceOf(address(wheel));
        uint256 netBefore = buck.netBalanceOf(address(wheel));
        _trade(ub, address(buck), int256(100_000e6));
        uint256 tokenBefore = asset.balanceOf(address(basket));
        (uint256 work,) = wheel.tick(1, 0);
        assertEq(work, 1);
        assertGt(asset.balanceOf(address(basket)), tokenBefore);
        assertEq(buck.balanceOf(address(wheel)), grossBefore);
        assertLt(buck.netBalanceOf(address(wheel)), netBefore);
    }

    function test_bothExistingWheelsFitRuntimeLimit() public {
        assertLe(address(wheel).code.length, 24_576);
        BasketWheel comparison = new BasketWheel(address(buck), address(usdc), GOV, 0, 0);
        assertLe(address(comparison).code.length, 24_576);
    }
}
