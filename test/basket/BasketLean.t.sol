// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckBasketEquityTest, IBasketUpkeep} from "./BuckBasketEquity.t.sol";
import {ControlledPairIntent} from "./BasketUpkeepFacet.t.sol";
import {BuckBasketEquityStorage} from "../../src/basket/BuckBasketEquityStorage.sol";
import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";
import {BasketVenueFacet} from "../../src/basket/BasketVenueFacet.sol";
import {PortfolioMath as PM} from "../../src/basket/PortfolioMath.sol";

/// @notice A target that leans against price: the shell's tilt, its cap and
/// the reference price each constituent is taken to return to.
contract BasketLeanTest is BuckBasketEquityTest {
    ControlledPairIntent intent;

    // A paired book at its declared weights, and a policy that asks for everything.
    function _balanced(uint16 tiltBp, uint16 capBp) private returns (uint128[3] memory placed) {
        _deposit(alice, address(tok[0]), 33_333e18);
        _deposit(alice, address(tok[1]), uint256(33_333e18) / 2);
        _deposit(alice, address(tok[2]), uint256(33_334e18) / 3);
        intent = new ControlledPairIntent(address(b));
        intent.setUrgent(0.02e18, 7);
        vm.startPrank(GOV);
        b.setVenue(address(new BasketVenueFacet()));
        b.setEquityWheel(address(new BasketUpkeepFacet(address(intent), address(0), 0)));
        b.setResponse(BuckBasketEquityStorage.ResponseParams(3000, 1800, tiltBp, capBp));
        b.setNetMark(true);
        vm.stopPrank();
        assertEq(_response(), 1);
        assertEq(intent.impacts(), 0, "pairing a balanced book trades nothing");
        placed = [b.liquidityOf(0), b.liquidityOf(1), b.liquidityOf(2)];
    }

    function _response() private returns (uint256) {
        vm.warp(vm.getBlockTimestamp() + 1800); vm.roll(vm.getBlockNumber() + 1);
        return IBasketUpkeep(address(b)).wheelStep(2, 0);
    }

    function test_lean_tiltAndCapAreBoundedAndTheReferenceIsGovernances() public {
        vm.startPrank(GOV);
        vm.expectRevert(bytes("response params"));
        b.setResponse(BuckBasketEquityStorage.ResponseParams(3000, 1800, 10001, 0));
        vm.expectRevert(bytes("response params"));
        b.setResponse(BuckBasketEquityStorage.ResponseParams(3000, 1800, 5000, 3000));
        b.setResponse(BuckBasketEquityStorage.ResponseParams(3000, 1800, 5000, 1500));
        vm.expectRevert(bytes("constituent")); b.setReferencePrice(3, B);
        b.setReferencePrice(0, 2 * B);
        vm.stopPrank();
        assertEq(b.referencePrice(0), 2 * B);
        vm.expectRevert(); b.setReferencePrice(0, B);
    }

    function test_lean_withNoTiltABalancedBookIsLeftAlone() public {
        _balanced(0, 1500);
        vm.prank(GOV); b.setReferencePrice(0, price[0] / 2);
        _response();
        assertEq(intent.impacts(), 0, "declared weights, and nothing over them");
    }

    function test_lean_aTokenAboveItsReferenceIsSoldBelowItsDeclaredWeight() public {
        uint128[3] memory placed = _balanced(5000, 1500);
        vm.prank(GOV); b.setReferencePrice(0, price[0] / 2); // TOKEN 0 stands at twice its reference
        assertEq(_response(), 1);
        assertLt(b.liquidityOf(0), placed[0], "a balanced book sells the TOKEN that stands above its reference");
        assertGt(b.liquidityOf(1), placed[1], "and buys the others");
        // A tilt of one half would take 1/sqrt(2) of its weight; the cap holds it to 15% of 33.33 points.
        assertApproxEqAbs(intent.reported(0), 0.05e18, 0.001e18, "its gap is measured from the leaned target");
        assertLt(intent.reported(1), 0); assertLt(intent.reported(2), 0);
        _books();
    }

    function test_lean_theCapIsWhatBindsOnlyBeyondIt() public {
        _balanced(5000, 2900);
        vm.prank(GOV); b.setReferencePrice(0, price[0] / 2);
        _response();
        // 3333 x 0.7071 against 3333 and 3334: 26.1% of the book, 7.2 points under its declared weight.
        assertApproxEqAbs(intent.reported(0), 0.0721e18, 0.001e18);
    }

    function test_lean_aMoveOfEveryPriceTogetherLeansNothing() public {
        _balanced(5000, 1500);
        vm.startPrank(GOV);
        for (uint256 i; i < 3; ++i) b.setReferencePrice(i, price[i] / 2);
        vm.stopPrank();
        _response();
        assertEq(intent.impacts(), 0, "only one price against another leans a target");
    }
}

contract PortfolioMathTest is Test {
    function test_lnWad() public pure {
        assertEq(PM.lnWad(1e18), 0);
        assertApproxEqAbs(PM.lnWad(2e18), 693147180559945309, 1e9);
        assertApproxEqAbs(PM.lnWad(0.5e18), -693147180559945309, 1e9);
        assertApproxEqAbs(PM.lnWad(1.3e18), 262364264467491052, 1e9);
        assertApproxEqAbs(PM.lnWad(1e12), -13815510557964274104, 1e10);
        assertApproxEqAbs(PM.lnWad(1e24), 13815510557964274104, 1e10);
    }

    function testFuzz_expUndoesLn(uint256 x) public pure {
        x = bound(x, 0.05e18, 20e18);
        assertApproxEqRel(PM.expWad(PM.lnWad(x)), x, 1e12);
    }
}
