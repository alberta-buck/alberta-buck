// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {BuckBasketEquityTest} from "./BuckBasketEquity.t.sol";
import {BuckBasketUniswapV3} from "../../src/basket/BuckBasketUniswapV3.sol";
import {EquityDesk} from "../../src/basket/EquityDesk.sol";
import {MonetaryDesk} from "../../src/basket/MonetaryDesk.sol";
import {BuckBasketStorage} from "../../src/basket/BuckBasketStorage.sol";

contract EqMonDir {
    int32 public effort;
    bool public outright;
    uint32 public epoch;
    function set(int32 e, bool o) external { effort = e; outright = o; epoch++; }
    function monetaryEffort() external view returns (int32, bool) { return (effort, outright); }
    function epochNow() external view returns (uint32) { return epoch; }
}

/// @title The monetary desk as its own credit holder (EquityDesk) beside the
///        equity basket: every equity test again with a desk present, and the
///        two accounts kept apart -- the basket's mark, limit and books never
///        see the desk's capital or issuance (D2), the desk issues only on its
///        own credit (D1), and its relief is its own (D3).
contract EquityDeskTest is BuckBasketEquityTest {
    EquityDesk internal desk;
    EqMonDir internal md;

    function setUp() public virtual override {
        super.setUp();
        md = new EqMonDir();
        desk = new EquityDesk(address(buck), address(ctrl), address(b), GOV);
        _bind(address(desk), false);                      // its own account: non-Carrying
        vm.startPrank(GOV);
        desk.setVenue(address(new BuckBasketUniswapV3()));
        desk.mirrorConstituents();
        desk.openCredit(address(credit), FACE);
        desk.setMonetaryDirector(address(md));
        desk.setOpsParams(MonetaryDesk.OpsParams({maxLegBp: 40, maxPositionBp: 1000,
                                                  maxOutrightBp: 1000, enabled: true}));
        vm.stopPrank();
    }

    /// @dev Give the desk its founding grant: `amount` of TOKEN i.
    function _grantDesk(uint256 i, uint256 amount) internal {
        tok[i].mint(GOV, amount);
        vm.startPrank(GOV);
        tok[i].approve(address(desk), amount);
        desk.capitalizeMonetary(i, amount);
        vm.stopPrank();
    }

    /// @dev The depositors' equity at the exits' (low) marks: the gross, the
    ///      basket's account at Buck and the relief accrued on it.
    function _equityLow() internal view returns (uint256) {
        int256 e = int256(b.grossAt(2)) + buck.signedBalanceOf(address(b))
                 + int256(buck.reliefOf(address(b)));
        return e > 0 ? uint256(e) : 0;
    }

    /// @dev The basket's books, which nothing the desk does may move.
    struct Books { int256 account; uint256 shares; uint128 l0; uint128 l1; uint128 l2; uint256 t0; uint256 mark; }

    function _snapBooks() internal view returns (Books memory k) {
        k = Books(buck.signedBalanceOf(address(b)), b.totalShares(), b.liquidityOf(0),
                  b.liquidityOf(1), b.liquidityOf(2), b.idleToken(0), b.markNow());
    }

    function _sameBooks(Books memory a, Books memory c) internal pure {
        assertEq(a.account, c.account, "the basket's account");
        assertEq(a.shares, c.shares, "shares");
        assertEq(a.l0, c.l0, "liquidity");
        assertEq(a.l1, c.l1, "liquidity");
        assertEq(a.l2, c.l2, "liquidity");
        assertEq(a.t0, c.t0, "wallet TOKEN");
        assertEq(a.mark, c.mark, "the basket's mark");
    }

    function _deskLien() internal view returns (uint256) {
        int256 s_ = buck.signedBalanceOf(address(desk));
        return s_ < 0 ? uint256(-s_) : 0;
    }

    /// Regression (2026-09-29, the savings sandbox): the desk's founding grant
    /// is the desk's capital, never the depositors' collateral.  The basket's
    /// credit was marked at its equity PLUS the desk's value -- one account
    /// for two parties -- so a grant raised the limit the wheel's components
    /// spend against: the live world levered its depositors to a lien of 6.3M
    /// on 5.8M of equity (K = 0.75).
    ///      A step marks the credit before it acts, so right after one the
    ///      mark and today's equity differ by what the step did (tens of
    ///      BUCK here): the test allows 1%, and grants the desk ~3x the
    ///      basket's equity, so a grant in the mark cannot hide in it.
    function test_desk_grantIsNotTheDepositorsCollateral() public {
        _placed();
        _daily();                                           // a step marks the credit
        assertApproxEqRel(b.markNow(), _equityLow(), 0.01e18, "the mark: the depositors' equity");
        uint256 limit0 = buck.creditLimit(address(b));

        _grantDesk(0, 1_000_000e18);                        // 1,000,000 BUCK of T0
        vm.warp(block.timestamp + 1 days);
        vm.roll(block.number + 1);
        _daily();
        assertApproxEqRel(b.markNow(), _equityLow(), 0.01e18,
                          "the desk's grant is not in the depositors' mark");
        assertApproxEqRel(buck.creditLimit(address(b)), limit0, 0.02e18,
                          "nor in the limit the components spend against");
        assertEq(IERC20(address(tok[0])).balanceOf(address(desk)), 1_000_000e18,
                 "the grant is the desk's, in the desk's contract");
    }

    /// D1: the desk issues on its own credit, marked at its own value, and the
    /// basket's books do not move (D2).
    function test_desk_issuesOnItsOwnCredit() public {
        _placed();
        _grantDesk(0, 100_000e18);
        Books memory k0 = _snapBooks();
        md.set(50, true);                                   // BUCK dear, persistent: issue
        assertEq(desk.monetaryOperation(), 4, "Q4 issue");
        assertGt(desk.monetaryOutstanding(), 0, "issued");
        assertGt(_deskLien(), 0, "on the desk's own lien");
        assertLe(_deskLien(), ctrl.k() * desk.markNow() / 1e18, "within K x the desk's mark");
        assertEq(buck.creditLimit(address(desk)), ctrl.k() * desk.markNow() / 1e18,
                 "Buck enforces the desk's limit");
        _sameBooks(k0, _snapBooks());
        _books();
    }

    /// The other direction of D2: with no capital of its own the desk cannot
    /// issue at all -- the depositors' spare liquidity is not its room.
    function test_desk_cannotIssueOnTheDepositorsRoom() public {
        _placed();
        assertGt(b.liquidity(), 0, "the basket has room to spend");
        Books memory k0 = _snapBooks();
        md.set(50, true);
        vm.expectRevert(BuckBasketStorage.MonetaryBound.selector);
        desk.monetaryOperation();
        assertEq(_deskLien(), 0, "no lien for the desk");
        _sameBooks(k0, _snapBooks());
    }

    function test_desk_absorbsWithItsGrant() public {
        _placed();
        _grantDesk(1, 50_000e18);
        Books memory k0 = _snapBooks();
        md.set(-50, false);                                 // BUCK cheap, transient: absorb
        assertEq(desk.monetaryOperation(), 1, "Q1 absorb");
        assertGt(desk.monetaryBuckHeld(), 0, "held");
        assertLt(desk.monetaryTokenHeld(1), 50_000e18, "paid in the desk's TOKEN");
        _sameBooks(k0, _snapBooks());
    }

    function test_desk_bookSurvivesEveryDepositorLeaving() public {
        uint256 id = _placed();
        _grantDesk(0, 100_000e18);
        md.set(50, true);
        desk.monetaryOperation();
        md.set(-50, false);
        desk.monetaryOperation();
        uint256 held = desk.monetaryBuckHeld();
        int256 out = desk.monetaryOutstanding();
        uint256 t0 = desk.monetaryTokenHeld(0);
        int256 account = buck.signedBalanceOf(address(desk));
        vm.prank(alice);
        b.redeem(id, 10000);
        assertEq(desk.monetaryBuckHeld(), held, "the desk's BUCK untouched");
        assertEq(desk.monetaryOutstanding(), out, "its issuance untouched");
        assertEq(desk.monetaryTokenHeld(0), t0, "the desk's TOKEN untouched");
        assertEq(buck.signedBalanceOf(address(desk)), account, "its account untouched");
        assertGe(tok[0].balanceOf(address(desk)), t0, "its TOKEN book backed");
        _books();
    }

    /// D3: the relief on the desk's lien is the desk's: collected by its next
    /// operation, it melts the desk's outstanding issuance, and the basket's
    /// relief is untouched by it.
    function test_desk_reliefIsTheDesks() public {
        _placed();
        _grantDesk(0, 100_000e18);
        md.set(50, true);
        desk.monetaryOperation();
        int256 out0 = desk.monetaryOutstanding();
        vm.warp(block.timestamp + 365 days);
        vm.roll(block.number + 1);
        uint256 r = buck.reliefOf(address(desk));
        assertGt(r, 0, "the desk's lien earned relief");
        uint256 basketRelief = buck.reliefOf(address(b));
        md.set(-50, false);                                 // an absorb: collects, then buys
        assertEq(desk.monetaryOperation(), 1, "Q1");
        assertEq(buck.reliefOf(address(desk)), 0, "collected");
        assertApproxEqAbs(desk.monetaryOutstanding(), out0 - int256(r), 1,
                          "the relief melted the desk's outstanding issuance");
        assertEq(buck.reliefOf(address(b)), basketRelief, "the basket's relief is its own");
    }
}
