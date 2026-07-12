// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}    from "forge-std/Test.sol";
import {IERC20}  from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {BuckBasketProRata}   from "../../src/basket/BuckBasketProRata.sol";
import {BuckBasketUniswapV3} from "../../src/basket/BuckBasketUniswapV3.sol";
import {BuckBasketStorage}   from "../../src/basket/BuckBasketStorage.sol";
import {MockBuck, MockController, BBToken} from "./BuckBasketProRata.t.sol";

/// @dev Scriptable stand-in for an IRebalanceDirector: the basket consumes
///      only poke / depositHint / redeemHint / effortOf / epochNow.
contract MockDirector {
    uint256 public depositHintV = type(uint256).max;
    uint256 public redeemHintV = type(uint256).max;
    mapping(uint256 => int256) public effortV;
    uint32  public epoch;
    uint256 public pokes;

    function setHints(uint256 dep, uint256 red) external {
        depositHintV = dep;
        redeemHintV = red;
    }
    function setEffort(uint256 i, int256 e) external { effortV[i] = e; }
    function setEpoch(uint32 e) external { epoch = e; }

    function poke(uint256) external returns (uint256) { pokes++; return 1; }
    function depositHint() external view returns (uint256) { return depositHintV; }
    function redeemHint() external view returns (uint256) { return redeemHintV; }
    function effortOf(uint256 i) external view returns (int256) { return effortV[i]; }
    function epochNow() external view returns (uint32) { return epoch; }
}

/// @dev A director whose every call reverts -- the advisory layer must never
///      block basket flows.
contract RevertingDirector {
    fallback() external { revert("nope"); }
}

/// @title Increment-0 director wiring: poke-on-activation, hint-routed
///        deposits/sweeps, and the bounded director-guided rebalanceStep.
contract BuckBasketDirectedTest is Test {

    address constant GOV = address(0xA0);

    MockBuck            internal buck;
    MockController      internal ctrl;
    BuckBasketProRata   internal basketC;
    BuckBasketUniswapV3 internal venueFacet;
    address             internal v3Factory;
    MockDirector        internal dir;

    BBToken internal paxg;    // 18-dec
    BBToken internal cbbtc;   // 8-dec

    address internal alice = address(0xA11CE);

    uint256 constant PAXG_PRICE  = 4000e18;
    uint256 constant CBBTC_PRICE = 100000e18;

    function setUp() public {
        buck = new MockBuck();
        ctrl = new MockController();
        v3Factory = deployCode("out/UniswapV3Factory.sol/UniswapV3Factory.json");
        basketC = new BuckBasketProRata(
            address(buck), address(ctrl), v3Factory, GOV,
            500, 600, 64, 500, 1e3);
        venueFacet = new BuckBasketUniswapV3();
        vm.prank(GOV);
        basketC.setVenue(address(venueFacet));
        buck.setBasket(address(basketC));

        paxg = new BBToken("PAX Gold", "PAXG", 18);
        cbbtc = new BBToken("cbBTC", "cbBTC", 8);
        vm.prank(GOV);
        basketC.addBasketToken(address(paxg), 18, PAXG_PRICE, 0, 500);
        vm.prank(GOV);
        basketC.addBasketToken(address(cbbtc), 8, CBBTC_PRICE, 0, 500);

        // Seed both pools with depositor liquidity.
        paxg.mint(alice, 1_000e18);
        cbbtc.mint(alice, 100e8);
        vm.startPrank(alice);
        paxg.approve(address(basketC), type(uint256).max);
        cbbtc.approve(address(basketC), type(uint256).max);
        basketC.depositToken(address(paxg), 100e18, 0);     // 400k BUCK value
        basketC.depositToken(address(cbbtc), 4e8, 0);       // 400k BUCK value
        vm.stopPrank();

        dir = new MockDirector();
        vm.prank(GOV);
        basketC.setDirector(address(dir));
    }

    function _pool(uint256 i) internal view returns (address pool) {
        (,,,,, pool,,,,,) = basketC.constituents(i);
    }

    // --- Wiring ------------------------------------------------------------- //

    function test_setDirector_onlyGov() public {
        vm.expectRevert(BuckBasketStorage.NotGovernance.selector);
        basketC.setDirector(address(0xD1));
        vm.prank(GOV);
        basketC.setDirector(address(0));
        assertEq(basketC.director(), address(0), "cleared");
    }

    function test_depositBuck_pokes_and_routes_to_hint() public {
        // Make pool 0 the underweight default; hint pool 1 instead.
        dir.setHints(1, type(uint256).max);
        buck.mint(alice, 10_000e18);
        vm.startPrank(alice);
        IERC20(address(buck)).approve(address(basketC), type(uint256).max);
        uint256 rid = basketC.depositToken(address(buck), 10_000e18, 0);
        vm.stopPrank();

        (,, address routed,) = basketC.deposits(rid);
        assertEq(routed, address(cbbtc), "BUCK deposit routed to the hint");
        assertGt(dir.pokes(), 0, "activation carried a poke");
    }

    function test_out_of_range_hint_falls_back() public {
        dir.setHints(7, type(uint256).max);          // bogus index
        buck.mint(alice, 10_000e18);
        vm.startPrank(alice);
        IERC20(address(buck)).approve(address(basketC), type(uint256).max);
        uint256 rid = basketC.depositToken(address(buck), 10_000e18, 0);
        vm.stopPrank();
        (,, address routed,) = basketC.deposits(rid);
        assertTrue(routed == address(paxg) || routed == address(cbbtc),
            "fell back to default most-underweight routing");
    }

    function test_reverting_director_never_blocks_flows() public {
        address hostile = address(new RevertingDirector());
        vm.prank(GOV);
        basketC.setDirector(hostile);
        buck.mint(alice, 10_000e18);
        vm.startPrank(alice);
        IERC20(address(buck)).approve(address(basketC), type(uint256).max);
        uint256 rid = basketC.depositToken(address(buck), 10_000e18, 0);
        basketC.redeem(rid, 5000);                   // partial redeem works too
        vm.stopPrank();
        assertGt(rid, 0, "flows unaffected by a hostile/broken director");
    }

    // --- rebalanceStep --------------------------------------------------------- //

    function test_rebalanceStep_moves_value_and_rate_limits() public {
        dir.setHints(1, 0);                          // sell pool 0, buy pool 1
        dir.setEffort(0, -50);                       // 50bp of NAV per epoch
        dir.setEpoch(1);

        uint256 buck0Before = IERC20(address(buck)).balanceOf(_pool(0));
        uint256 buck1Before = IERC20(address(buck)).balanceOf(_pool(1));

        basketC.rebalanceStep();

        assertLt(IERC20(address(buck)).balanceOf(_pool(0)), buck0Before,
            "sell pool drawn down");
        assertGt(IERC20(address(buck)).balanceOf(_pool(1)), buck1Before,
            "buy pool topped up");

        vm.expectRevert(BuckBasketStorage.StepAlreadyDone.selector);
        basketC.rebalanceStep();                     // one step per epoch

        dir.setEpoch(2);
        basketC.rebalanceStep();                     // next epoch: fine
    }

    function test_rebalanceStep_guards() public {
        // No advice: sentinel hints.
        dir.setEpoch(1);
        vm.expectRevert(BuckBasketStorage.NoAdvice.selector);
        basketC.rebalanceStep();

        // Buy-side effort on the sell hint (wrong sign) is not advice.
        dir.setHints(1, 0);
        dir.setEffort(0, 25);
        vm.expectRevert(BuckBasketStorage.NoAdvice.selector);
        basketC.rebalanceStep();

        // Director unset.
        vm.prank(GOV);
        basketC.setDirector(address(0));
        vm.expectRevert(BuckBasketStorage.DirectorUnset.selector);
        basketC.rebalanceStep();
    }
}
