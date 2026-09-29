// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {BuckBasketEquityTest} from "./BuckBasketEquity.t.sol";
import {BuckBasketEquity} from "../../src/basket/BuckBasketEquity.sol";
import {BuckBasketEquityOps} from "../../src/basket/BuckBasketEquityOps.sol";
import {MonetaryDesk} from "../../src/basket/MonetaryDesk.sol";

contract EqMonDir {
    int32 public effort;
    bool public outright;
    uint32 public epoch;
    function set(int32 e, bool o) external { effort = e; outright = o; epoch++; }
    function monetaryEffort() external view returns (int32, bool) { return (effort, outright); }
    function epochNow() external view returns (uint32) { return epoch; }
}

/// @title The equity basket with the monetary desk (BuckBasketEquityOps): every
///        equity test again, and the desk's book kept apart from the equity
///        books -- the liability-class separation, by explicit books.
contract BuckBasketEquityOpsTest is BuckBasketEquityTest {
    EqMonDir internal md;

    function _newBasket(address factory) internal override returns (BuckBasketEquity) {
        return new BuckBasketEquityOps(address(buck), address(ctrl), factory, GOV,
                                       3000, 600, 64, 500, 1e3);
    }

    function _ops() internal view returns (BuckBasketEquityOps) {
        return BuckBasketEquityOps(payable(address(b)));
    }

    function _enableDesk() internal {
        md = new EqMonDir();
        vm.startPrank(GOV);
        _ops().setMonetaryDirector(address(md));
        _ops().setOpsParams(MonetaryDesk.OpsParams({maxLegBp: 40, maxPositionBp: 1000,
                                                     maxOutrightBp: 1000, enabled: true}));
        vm.stopPrank();
    }

    struct Books { uint256 debt; uint256 shares; uint256 idle; uint256 owed; uint256 minted;
                   uint128 l0; uint128 l1; uint128 l2; uint256 t0; }

    function _snapBooks() internal view returns (Books memory k) {
        k = Books(b.debt(), b.totalShares(), b.idleBuck(), b.owed(), b.mintedTotal(),
                  b.liquidityOf(0), b.liquidityOf(1), b.liquidityOf(2), b.idleToken(0));
    }

    function _sameBooks(Books memory a, Books memory c) internal pure {
        assertEq(a.debt, c.debt, "debt");
        assertEq(a.shares, c.shares, "shares");
        assertEq(a.idle, c.idle, "wallet BUCK");
        assertEq(a.owed, c.owed, "owed");
        assertEq(a.minted, c.minted, "equity mints");
        assertEq(a.l0, c.l0, "liquidity");
        assertEq(a.l1, c.l1, "liquidity");
        assertEq(a.l2, c.l2, "liquidity");
        assertEq(a.t0, c.t0, "wallet TOKEN");
    }

    function test_desk_issueAndAbsorbLeaveTheEquityBooksAlone() public {
        _placed();
        _enableDesk();
        Books memory k0 = _snapBooks();
        md.set(50, true);                                // BUCK dear, persistent: issue
        assertEq(_ops().monetaryOperation(), 4, "Q4 issue");
        assertGt(_ops().monetaryOutstanding(), 0);
        _sameBooks(k0, _snapBooks());
        md.set(-50, false);                              // BUCK cheap, transient: absorb
        assertEq(_ops().monetaryOperation(), 1, "Q1 absorb");
        assertGt(_ops().monetaryBuckHeld(), 0);
        _sameBooks(k0, _snapBooks());
        _books();
    }

    function test_desk_bookSurvivesEveryDepositorLeaving() public {
        uint256 id = _placed();
        _enableDesk();
        md.set(50, true);
        _ops().monetaryOperation();
        md.set(-50, false);
        _ops().monetaryOperation();
        uint256 held = _ops().monetaryBuckHeld();
        uint256 t0 = _ops().monetaryTokenHeld(0);
        vm.prank(alice);
        b.redeem(id, 10000);
        assertEq(_ops().monetaryBuckHeld(), held, "the desk's BUCK untouched");
        assertEq(_ops().monetaryTokenHeld(0), t0, "the desk's TOKEN untouched");
        assertGe(IERC20(address(buck)).balanceOf(address(b)), b.idleBuck() + held,
                 "both books backed");
        _books();
    }
}
