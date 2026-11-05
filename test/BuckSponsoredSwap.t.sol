// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IUniswapV3Factory} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Factory.sol";
import {IUniswapV3SwapCallback} from "@uniswap/v3-core/contracts/interfaces/callback/IUniswapV3SwapCallback.sol";
import {MockERC20} from "./mocks/MockERC20.sol";
import {BuckNetSwapGuard} from "../src/BuckNetSwapGuard.sol";
import {BuckCompoundFixture} from "./BuckCompound.t.sol";

/// @dev One fixed nominal outflow with the canonical swap/callback shape.
///      Full V3 execution in both token orders is covered by platform tests.
contract SponsoredSwapPool {
    address public immutable token0;
    address public immutable token1;
    uint24 public constant fee = 500;
    constructor(address settlement, address buck) { token0 = settlement; token1 = buck; }
    function swap(address recipient, bool zeroForOne, int256 amount, uint160, bytes calldata)
        external returns (int256, int256)
    {
        require(zeroForOne && amount == 1e6);
        IERC20(token1).transfer(recipient, 1e6);
        IUniswapV3SwapCallback(msg.sender).uniswapV3SwapCallback(1e6, -1e6, "");
        return (1e6, -1e6);
    }
}

contract SponsoredSwapFactory {
    address immutable pool;
    constructor(address pool_) { pool = pool_; }
    function getPool(address, address, uint24) external view returns (address) { return pool; }
}

contract BuckSponsoredSwapTest is BuckCompoundFixture {
    function test_sponsorCompensationCannotSatisfyThePurchasedBuckMinimum() public {
        MockERC20 usdc = new MockERC20("USD Coin", "USDC", 6);
        SponsoredSwapPool pool = new SponsoredSwapPool(address(usdc), address(buck));
        _bind(address(pool), true, address(this));
        uint256 aliceId = _credit(ALICE, INSURER_A, 100e6, 0);
        uint256 bobId = _credit(BOB, INSURER_B, 100e6, 0);
        vm.prank(ALICE); buck.issue(aliceId, 100e6, 100e6, 0, address(0));
        vm.prank(BOB); buck.issue(bobId, 100e6, 100e6, 0, address(0));
        vm.prank(BOB); buck.transfer(address(pool), 10e6);
        vm.prank(ALICE); buck.requestDemurragePayer(BOB);
        vm.prank(BOB); buck.acceptDemurragePayer(ALICE);
        SponsoredSwapFactory factory = new SponsoredSwapFactory(address(pool));
        BuckNetSwapGuard guard = new BuckNetSwapGuard(buck, usdc, IUniswapV3Factory(address(factory)));
        usdc.mint(ALICE, 2e6);
        vm.prank(ALICE); usdc.approve(address(guard), 2e6);
        vm.warp(t0 + YEAR);
        uint256 beforeOwner = buck.netBalanceScaled(ALICE);
        uint256 beforeSponsor = buck.netBalanceScaled(BOB);
        // A 1 nominal BUCK trade delivers <1 net BUCK. Its recipient also
        // receives >1 BUCK of sponsor compensation for its prior balance.
        // Counting that compensation as a purchase would wrongly pass 2 BUCK.
        vm.expectRevert(BuckNetSwapGuard.NetMinimum.selector);
        vm.prank(ALICE); guard.buyExactInput(address(pool), 1e6, 2e6, ALICE, 0);
        assertEq(buck.netBalanceScaled(ALICE), beforeOwner);
        assertEq(buck.netBalanceScaled(BOB), beforeSponsor);
        assertEq(buck.balanceOf(address(pool)), 10e6);
        assertEq(usdc.balanceOf(ALICE), 2e6);
        vm.prank(ALICE);
        (uint256 paid, uint256 nominal, uint256 net) = guard.buyExactInput(address(pool), 1e6, 900000, ALICE, 0);
        assertEq(paid, 1e6);
        assertEq(nominal, 1e6);
        assertLt(net, 1e6 * Q);
        assertGt(buck.netBalanceScaled(ALICE) - beforeOwner, 2e6 * Q);
        assertEq(buck.balanceOf(ALICE), 101e6);
        assertLt(buck.netBalanceScaled(BOB), beforeSponsor);
        assertEq(buck.demurragePayer(ALICE), BOB);
    }
}
