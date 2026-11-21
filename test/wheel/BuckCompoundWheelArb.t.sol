// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {JubileeEquityFixture} from "../basket/JubileeEquityIntegration.t.sol";
import {EqToken, IPool} from "../basket/BuckBasketEquity.t.sol";
import {IBasketUpkeep} from "../../src/basket/IBasketUpkeep.sol";
import {ArbKind} from "../../src/wheel/ArbKind.sol";
import {BasketMachineWheel} from "../../src/wheel/BasketMachineWheel.sol";

/// @dev Canonical unsigned Buck, actual NFT issuance and public basket LP
///      placement, with real V3 pools and the machine wheel. The inherited identity
///      harness supplies flags. The older wheel's own flash-swap arbitrage suite was
///      retired with it (2026-11-20); the funded and flash suites build on this fixture.
abstract contract BuckCompoundArbFixture is JubileeEquityFixture {
    EqToken asset;
    EqToken usdc;
    ArbKind wheel;
    address own;
    address market;
    address ub;

    function setUp() public virtual override {
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

        _configureArb(new BasketMachineWheel(address(buck), address(usdc), GOV, 0, 0));
    }

    function _configureArb(ArbKind next) internal {
        wheel = next;
        _bind(address(wheel), true, address(this));
        vm.startPrank(GOV);
        (bool ok,) = address(basket).call(abi.encodeWithSignature("setWheel(address)", address(wheel)));
        require(ok);
        wheel.setArb(ub, address(basket), 100, 1);
        wheel.setTriangle(0, ArbKind.Triangle(address(asset), own, market, 0));
        vm.stopPrank();
    }

    function _pool(address a, uint8 da, address b, uint8 db, uint24 fee)
        internal returns (address p)
    {
        p = basket.v3Factory().createPool(a, b, fee);
        bool a0 = IUniswapV3Pool(p).token0() == a;
        (uint256 num, uint256 den) = a0
            ? (10 ** uint256(db), 10 ** uint256(da))
            : (10 ** uint256(da), 10 ** uint256(db));
        IUniswapV3Pool(p).initialize(uint160(Math.sqrt(Math.mulDiv(num, 2 ** 192, den))));
    }

    function _mintPool(address p, int24 spacing, uint128 liquidity) internal {
        int24 top = (887272 / spacing) * spacing;
        IUniswapV3Pool(p).mint(address(this), -top, top, liquidity, "");
    }

    function _trade(address pool, address input, int256 amount) internal {
        bool zeroForOne = IUniswapV3Pool(pool).token0() == input;
        IUniswapV3Pool(pool).swap(address(this), zeroForOne, amount,
            zeroForOne ? MIN_SQRT + 1 : MAX_SQRT - 1, "");
    }

}
