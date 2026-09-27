// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}   from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math}   from "@openzeppelin/contracts/utils/math/Math.sol";

import {BuckBasketProRata}   from "../../src/basket/BuckBasketProRata.sol";
import {BuckBasketUniswapV3} from "../../src/basket/BuckBasketUniswapV3.sol";
import {BuckBasketStorage}   from "../../src/basket/BuckBasketStorage.sol";
import {BasketWheel}         from "../../src/wheel/BasketWheel.sol";
import {ArbKind}             from "../../src/wheel/ArbKind.sol";
import {MockBuck, MockController, BBToken} from "../basket/BuckBasketProRata.t.sol";

interface IFactoryW {
    function createPool(address a, address b, uint24 fee) external returns (address);
}

interface IPoolW {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function initialize(uint160 sqrtPriceX96) external;
    function mint(address recipient, int24 tickLower, int24 tickUpper, uint128 amount,
                  bytes calldata data) external returns (uint256, uint256);
    function swap(address recipient, bool zeroForOne, int256 amountSpecified,
                  uint160 sqrtPriceLimitX96, bytes calldata data) external returns (int256, int256);
    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, uint8, bool);
}

/// The consistency arbitrage on real V3 pools (doc/BASKET-WHEEL.org 3-5): a
/// ProRata basket with one constituent (PAXG) and a depositor, the market's
/// PAXG/USDC and BUCK/USDC pools, and the wheel beside the shell.  A gap is
/// opened by moving PAXG's USD price; a tick goes round the triangle with no
/// inventory, pays the caller its share, and credits the rest to the
/// depositors (TOKEN start) or the reserve and treasury (BUCK start).
contract BasketWheelArbTest is Test {
    uint160 constant MIN_SQRT = 4295128739;
    uint160 constant MAX_SQRT = 1461446703485210103287273052203988822378723970342;
    address constant GOV = address(0xA0);
    address constant ALICE = address(0xA11CE);
    address constant CALLER = address(0xCA11);

    MockBuck buck;
    BBToken  usdc;
    BBToken  paxg;
    BuckBasketProRata basket;
    BasketWheel wheel;
    address v3Factory;
    address poolOwn;        // PAXG/BUCK, the basket's
    address poolUsdc;       // PAXG/USDC, the market's
    address poolUb;         // BUCK/USDC, the market's

    function setUp() public {
        buck = new MockBuck();
        usdc = new BBToken("USD Coin", "USDC", 6);
        paxg = new BBToken("PAX Gold", "PAXG", 18);
        v3Factory = deployCode("out/UniswapV3Factory.sol/UniswapV3Factory.json");

        basket = new BuckBasketProRata(address(buck), address(new MockController()),
                                       v3Factory, GOV, 500, 600, 64, 500, 1e3);
        BuckBasketUniswapV3 venue = new BuckBasketUniswapV3();
        vm.prank(GOV); basket.setVenue(address(venue));
        buck.setBasket(address(basket));
        vm.prank(GOV);
        poolOwn = basket.addBasketToken(address(paxg), 18, 4000e18, 0, 3000);

        // a depositor: the basket's own pool gets depth (100 PAXG, ~$800k)
        paxg.mint(ALICE, 100e18);
        vm.prank(ALICE); paxg.approve(address(basket), 100e18);
        vm.prank(ALICE); basket.depositToken(address(paxg), 100e18, 0);

        // the market: deep PAXG/USDC (0.30%) and BUCK/USDC (0.05%), ~$10M a side
        paxg.mint(address(this), 1_000_000e18);
        usdc.mint(address(this), 1e18);
        buck.mint(address(this), 1e30);
        poolUsdc = _pool(address(paxg), 18, address(usdc), 6, 4000, 3000, 60, 1.6e17);
        poolUb   = _pool(address(buck), 18, address(usdc), 6, 1, 500, 10, 1e19);

        wheel = new BasketWheel(address(buck), address(usdc), GOV, 1000, 1_000e18);
        vm.startPrank(GOV);
        BasketWheelCaller(address(basket)).setWheel(address(wheel));
        wheel.setArb(poolUb, address(basket), 1000, 500, 1);   // share 10%, cap 5%
        wheel.setTriangle(0, ArbKind.Triangle(address(paxg), poolOwn, poolUsdc, 0));
        vm.stopPrank();
    }

    // --- helpers ----------------------------------------------------------------- //

    /// A pool of `a`/`b` where one `a` is worth `price` `b`, full range, liquidity L.
    function _pool(address a, uint8 da, address b, uint8 db, uint256 price, uint24 fee,
                   int24 spacing, uint128 L) internal returns (address p) {
        p = IFactoryW(v3Factory).createPool(a, b, fee);
        bool aIs0 = IPoolW(p).token0() == a;
        // raw price token1 / token0
        (uint256 num, uint256 den) = aIs0
            ? (price * 10 ** db, 10 ** uint256(da))
            : (10 ** uint256(da), price * 10 ** db);
        IPoolW(p).initialize(uint160(Math.sqrt(Math.mulDiv(num, 2 ** 192, den))));
        int24 top = (887272 / spacing) * spacing;
        IPoolW(p).mint(address(this), -top, top, L, "");
    }

    function uniswapV3MintCallback(uint256 a0, uint256 a1, bytes calldata) external {
        if (a0 > 0) IERC20(IPoolW(msg.sender).token0()).transfer(msg.sender, a0);
        if (a1 > 0) IERC20(IPoolW(msg.sender).token1()).transfer(msg.sender, a1);
    }

    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata) external {
        if (a0 > 0) IERC20(IPoolW(msg.sender).token0()).transfer(msg.sender, uint256(a0));
        if (a1 > 0) IERC20(IPoolW(msg.sender).token1()).transfer(msg.sender, uint256(a1));
    }

    /// Move PAXG's USD price: buy (up) or sell (down) PAXG on PAXG/USDC.
    function _movePaxgUsd(bool up, uint256 amountIn) internal {
        address tokenIn = up ? address(usdc) : address(paxg);
        bool zeroForOne = IPoolW(poolUsdc).token0() == tokenIn;
        IPoolW(poolUsdc).swap(address(this), zeroForOne, int256(amountIn),
                              zeroForOne ? MIN_SQRT + 1 : MAX_SQRT - 1, "");
    }

    // --- tests --------------------------------------------------------------------- //

    function test_consistent_pools_leave_nothing_to_do() public {
        (uint256 x,,) = wheel.plan(0);
        assertEq(x, 0);
        assertEq(wheel.pending(), 0);
        (uint256 work,) = wheel.tick(1, 0);
        assertEq(work, 0);
    }

    function test_a_gap_is_closed_and_credited_to_the_depositors() public {
        _movePaxgUsd(true, 300_000e6);                  // PAXG ~3% dearer in USD
        (uint256 x, uint8 dir, uint256 quoted) = wheel.plan(0);
        assertGt(x, 0);
        uint256 bonus0 = basket.stressBonusPrincipal();
        uint256 out0 = basket.totalOutstandingBuck();
        uint256 caller0 = paxg.balanceOf(CALLER);

        vm.recordLogs();
        vm.prank(CALLER);
        (uint256 work,) = wheel.tick(1, 0);
        assertEq(work, 1);

        uint256 share = paxg.balanceOf(CALLER) - caller0;
        assertGt(share, 0);                              // the caller's 10%
        assertGt(basket.stressBonusPrincipal(), bonus0); // the depositors' credit
        assertEq(basket.totalOutstandingBuck() - out0,
                 basket.stressBonusPrincipal() - bonus0);
        assertEq(paxg.balanceOf(address(wheel)), 0);     // nothing kept in the wheel
        // the realized profit is within a few percent of the closed form's quote
        uint256 profit = share * 10 * 1e18 / 1e18;       // share is 10% of it
        assertApproxEqRel(profit, quoted, 0.05e18);
        dir;
        // and the gap is (mostly) gone: the next plan is far smaller
        (uint256 x2,,) = wheel.plan(0);
        assertLt(x2, x / 4);
    }

    function test_the_other_direction() public {
        _movePaxgUsd(false, 75e18);                      // PAXG ~3% cheaper in USD
        (uint256 x, uint8 dir,) = wheel.plan(0);
        assertGt(x, 0);
        vm.prank(CALLER);
        (uint256 work,) = wheel.tick(1, 0);
        assertEq(work, 1);
        (uint256 x2, uint8 dir2,) = wheel.plan(0);
        assertLt(x2, x / 4);
        dir; dir2;
    }

    function test_a_capped_cycle_closes_the_gap_over_blocks() public {
        // cap 1% of the first leg's input: against the basket's own 100-PAXG
        // pool a cycle takes 1 PAXG, so a ~6% gap closes over several blocks
        vm.prank(GOV); wheel.setArb(poolUb, address(basket), 1000, 100, 1);
        _movePaxgUsd(false, 75e18);
        uint256 cycles = 0;
        for (uint256 b = 0; b < 10; b++) {
            vm.roll(block.number + 1);
            (uint256 work,) = wheel.tick(1, 0);
            if (work == 0) break;
            cycles++;
        }
        assertGt(cycles, 1);
        (uint256 x,,) = wheel.plan(0);
        assertEq(x, 0);                                   // closed, to within the edge
    }

    function test_buck_start_funds_the_reserve_then_the_treasury() public {
        vm.prank(GOV); wheel.setStartMode(1);
        vm.prank(GOV); wheel.setReserveParams(1000, 1e18);   // a small cap: overflow to treasury
        _movePaxgUsd(true, 300_000e6);
        uint256 t0 = basket.treasuryBuckPending();
        uint256 c0 = buck.balanceOf(CALLER);
        vm.prank(CALLER);
        (uint256 work, uint256 reservePay) = wheel.tick(1, 0);
        assertEq(work, 1);
        assertEq(wheel.reserve() + reservePay, 1e18);        // filled to the cap, then kappa paid
        assertGt(basket.treasuryBuckPending(), t0);          // the rest to the treasury
        assertGt(buck.balanceOf(CALLER), c0);                // share + kappa, in BUCK
    }

    function test_only_the_wheel_credits_the_basket() public {
        vm.expectRevert(BuckBasketStorage.NotWheel.selector);
        BasketWheelCaller(address(basket)).creditTreasury(1);
        vm.expectRevert(BuckBasketStorage.NotWheel.selector);
        BasketWheelCaller(address(basket)).creditDepositors(0, 1);
    }

    function test_only_governance_sets_the_wheel() public {
        vm.expectRevert(BuckBasketStorage.NotGovernance.selector);
        BasketWheelCaller(address(basket)).setWheel(address(this));
    }

    function test_cycles_cannot_be_called_or_called_back_from_outside() public {
        vm.expectRevert(ArbKind.NotSelf.selector);
        wheel.arbCycle(0, 0, 1e18);
        vm.expectRevert(ArbKind.BadCallback.selector);
        wheel.uniswapV3SwapCallback(1, 0, abi.encode(uint8(0)));
    }
}

/// The wheel's entry points on the basket: hosted by the venue facet, reached
/// through the shell's fallback.
interface BasketWheelCaller {
    function setWheel(address) external;
    function creditTreasury(uint256) external;
    function creditDepositors(uint256, uint256) external returns (uint128, uint256);
}
