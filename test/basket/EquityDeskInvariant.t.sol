// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {StdInvariant} from "forge-std/StdInvariant.sol";
import {EquityDeskTest} from "./EquityDesk.t.sol";

/// @title Two credit holders, one set of pools: random sequences of deposits,
///        exits, desk grants and operations, wheel rounds, outside trades, K
///        steps and time, with the separation checked after every call.
///
/// @notice The standing guard for the defect of 2026-09-29 (the desk's grant
///         in the depositors' mark).  The contract is its own handler: the
///         fuzzer calls only the h_* functions.  A handler records what it
///         saw in ghost variables rather than asserting -- a failing assert
///         there would revert the call, which the fuzzer skips silently --
///         and the invariants read the ghosts.
contract EquityDeskInvariantTest is EquityDeskTest {
    uint256[] internal ids;
    mapping(uint256 => address) internal ownerOfId;

    uint256 public ghostMaxMarkExcessBp;   // basket mark over its equity(LOW), after its own verbs
    bool    public ghostDeskMovedBasket;   // a desk action changed the basket's books
    bool    public ghostBasketMovedDesk;   // a basket action changed the desk's books
    uint256 public ghostDeskOverLimit;     // desk issuance past K x its own mark
    uint256 public ghostCalls;

    /// @dev The world as EquityDeskTest builds it (an empty basket, so the
    ///      inherited tests still hold); the handlers deposit.
    function setUp() public override {
        super.setUp();
        bytes4[] memory s = new bytes4[](8);
        s[0] = this.h_deposit.selector;
        s[1] = this.h_redeem.selector;
        s[2] = this.h_grant.selector;
        s[3] = this.h_desk.selector;
        s[4] = this.h_wheel.selector;
        s[5] = this.h_push.selector;
        s[6] = this.h_k.selector;
        s[7] = this.h_warp.selector;
        targetSelector(StdInvariant.FuzzSelector({addr: address(this), selectors: s}));
        targetContract(address(this));
    }

    // ---- the desk's books, as a value ---------------------------------------- //

    struct Desk { int256 account; uint256 held; int256 out; uint256 t0; uint256 t1; uint256 t2; uint256 mark; }

    /// @dev The raw account: `signedBalanceOf` nets the demurrage accrued on
    ///      held BUCK, which grows with time alone.
    function _desk() internal view returns (Desk memory k) {
        k = Desk(buck.signedRawBalanceOf(address(desk)), desk.monetaryBuckHeld(),
                 desk.monetaryOutstanding(), desk.monetaryTokenHeld(0),
                 desk.monetaryTokenHeld(1), desk.monetaryTokenHeld(2), desk.markNow());
    }

    function _sameDesk(Desk memory a, Desk memory c) internal pure returns (bool) {
        return a.account == c.account && a.held == c.held && a.out == c.out
            && a.t0 == c.t0 && a.t1 == c.t1 && a.t2 == c.t2 && a.mark == c.mark;
    }

    function _sameBasket(Books memory a, Books memory c) internal pure returns (bool) {
        return a.account == c.account && a.shares == c.shares && a.l0 == c.l0
            && a.l1 == c.l1 && a.l2 == c.l2 && a.t0 == c.t0 && a.mark == c.mark;
    }

    /// @dev After a basket verb that marks at its end (a deposit, an exit):
    ///      the mark it wrote against equity(LOW) in the same state.  (A
    ///      wheel round re-pins the pools after its steps mark, so it is not
    ///      checked here: its steps are pinned by the unit tests.)
    function _checkMark() internal {
        uint256 e = _equityLow();
        uint256 m = b.markNow();
        if (e == 0 || m <= e) return;
        uint256 over = (m - e) * 10000 / e;
        if (over > ghostMaxMarkExcessBp) ghostMaxMarkExcessBp = over;
    }

    // ---- the handlers ------------------------------------------------------------ //

    function h_deposit(uint8 who, uint8 asset, uint96 amt) public {
        ghostCalls++;
        address w = who % 2 == 0 ? alice : bob;
        uint256 a = asset % 4;
        address token = a == 3 ? address(buck) : address(tok[a]);
        uint256 amount = a == 3 ? bound(amt, 100 * B, 100_000 * B) : bound(amt, 100e18, 20_000e18);
        if (IERC20(token).balanceOf(w) < amount) return;
        Desk memory d0 = _desk();
        vm.startPrank(w);
        IERC20(token).approve(address(b), amount);
        try b.deposit(token, amount, 0) returns (uint256 id) {
            ids.push(id);
            ownerOfId[id] = w;
            _checkMark();
        } catch {}
        vm.stopPrank();
        if (!_sameDesk(d0, _desk())) ghostBasketMovedDesk = true;
    }

    function h_redeem(uint256 k, uint16 bp) public {
        ghostCalls++;
        if (ids.length == 0) return;
        uint256 id = ids[k % ids.length];
        uint256 share = bound(bp, 1, 10000);
        Desk memory d0 = _desk();
        vm.prank(ownerOfId[id]);
        try b.redeem(id, share) { _checkMark(); } catch {}
        if (!_sameDesk(d0, _desk())) ghostBasketMovedDesk = true;
    }

    function h_grant(uint8 i, uint96 amt) public {
        ghostCalls++;
        Books memory k0 = _snapBooks();
        _grantDesk(i % 3, bound(amt, 1e18, 500_000e18));
        if (!_sameBasket(k0, _snapBooks())) ghostDeskMovedBasket = true;
    }

    function h_desk(int32 effort, bool outright) public {
        ghostCalls++;
        int32 e = int32(int256(bound(int256(effort), -100, 100)));
        if (e == 0) e = 1;
        md.set(e, outright);
        Books memory k0 = _snapBooks();
        uint256 lien0 = _deskLien();
        try desk.monetaryOperation() {} catch {}
        if (!_sameBasket(k0, _snapBooks())) ghostDeskMovedBasket = true;
        uint256 lien1 = _deskLien();
        if (lien1 > lien0 && lien1 > ctrl.k() * desk.markNow() / 1e18 + 1) ghostDeskOverLimit++;
    }

    function h_wheel(uint8 r) public {
        ghostCalls++;
        Desk memory d0 = _desk();
        _wheel(1 + r % 2, false);
        if (!_sameDesk(d0, _desk())) ghostBasketMovedDesk = true;
    }

    function h_push(uint8 i, bool buckIn, uint96 amt) public {
        ghostCalls++;
        uint256 amount = buckIn ? bound(amt, 1_000 * B, 50_000 * B) : bound(amt, 100e18, 20_000e18);
        try this.pushExt(i % 3, buckIn, amount) {} catch {}
    }

    function pushExt(uint256 i, bool buckIn, uint256 amount) external {
        require(msg.sender == address(this), "self");
        _push(i, buckIn, amount);
    }

    function h_k(uint16 kbp) public {
        ghostCalls++;
        ctrl.setK(bound(kbp, 5000, 9500) * 1e14);
    }

    function h_warp(uint32 s) public {
        ghostCalls++;
        vm.warp(block.timestamp + bound(s, 700, 2 days));
        vm.roll(block.number + 1);
    }

    // ---- the invariants ------------------------------------------------------------ //

    /// forge-config: default.invariant.runs = 6
    /// forge-config: default.invariant.depth = 24
    function invariant_theBasketsMarkIsItsOwnEquity() public view {
        assertLe(ghostMaxMarkExcessBp, 100, "the basket's mark never carries the desk (<= 1%)");
    }

    /// forge-config: default.invariant.runs = 6
    /// forge-config: default.invariant.depth = 24
    function invariant_theDeskNeverMovesTheBasket() public view {
        assertFalse(ghostDeskMovedBasket, "a desk action moved the basket's books");
    }

    /// forge-config: default.invariant.runs = 6
    /// forge-config: default.invariant.depth = 24
    function invariant_theBasketNeverMovesTheDesk() public view {
        assertFalse(ghostBasketMovedDesk, "a basket action moved the desk's books");
    }

    /// forge-config: default.invariant.runs = 6
    /// forge-config: default.invariant.depth = 24
    function invariant_theDeskIssuesWithinItsOwnLimit() public view {
        assertEq(ghostDeskOverLimit, 0, "the desk issued past K x its own mark");
    }

    /// forge-config: default.invariant.runs = 6
    /// forge-config: default.invariant.depth = 24
    function invariant_theBasketsBooks() public view {
        _books();
    }
}
