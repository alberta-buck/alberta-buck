// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}  from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

import {WorkWheel} from "../../src/wheel/WorkWheel.sol";

/// The chassis (doc/BASKET-WHEEL.org 8): two mock kinds composed the way the
/// real ones are -- each claims the slots after its bases' and hands the rest
/// to super -- so these tests pin the layout, the round-robin, the amortized
/// scan and its idle memo, the re-arm, and the reserve.

contract PayToken is ERC20 {
    constructor() ERC20("Buck", "BUCK") {}
    function mint(address to, uint256 a) external { _mint(to, a); }
}

abstract contract KindA is WorkWheel {
    uint256 public aSlots;
    mapping(uint256 => uint256) public aPending;
    uint256[] public aRan;

    function setA(uint256 n) external { aSlots = n; }
    function setAPending(uint256 i, uint256 p) external { aPending[i] = p; }
    function aRanLength() external view returns (uint256) { return aRan.length; }

    function _slotCount() internal view virtual override returns (uint256) {
        return super._slotCount() + aSlots;
    }
    function _due(uint256 s) internal view virtual override returns (bool) {
        uint256 b = super._slotCount();
        if (s < b) return super._due(s);
        return aPending[s - b] > 0;
    }
    function _run(uint256 s) internal virtual override returns (uint256) {
        uint256 b = super._slotCount();
        if (s < b) return super._run(s);
        aPending[s - b] -= 1;
        aRan.push(s - b);
        return 1;
    }
}

abstract contract KindB is WorkWheel {
    uint256 public bSlots;
    mapping(uint256 => uint256) public bPending;
    uint256[] public bRan;

    function setB(uint256 n) external { bSlots = n; }
    function setBPending(uint256 i, uint256 p) external { bPending[i] = p; }
    function bRanLength() external view returns (uint256) { return bRan.length; }

    function _slotCount() internal view virtual override returns (uint256) {
        return super._slotCount() + bSlots;
    }
    function _due(uint256 s) internal view virtual override returns (bool) {
        uint256 b = super._slotCount();
        if (s < b) return super._due(s);
        return bPending[s - b] > 0;
    }
    function _run(uint256 s) internal virtual override returns (uint256) {
        uint256 b = super._slotCount();
        if (s < b) return super._run(s);
        bPending[s - b] -= 1;
        bRan.push(s - b);
        return 1;
    }
}

contract TestWheel is WorkWheel, KindA, KindB {
    constructor(address pay, address gov) WorkWheel(pay, gov, 1000, 1_000e18) {}

    function _slotCount() internal view override(WorkWheel, KindA, KindB) returns (uint256) {
        return super._slotCount();
    }
    function _due(uint256 s) internal view override(WorkWheel, KindA, KindB) returns (bool) {
        return super._due(s);
    }
    function _run(uint256 s) internal override(WorkWheel, KindA, KindB) returns (uint256) {
        return super._run(s);
    }
}

contract WorkWheelTest is Test {
    PayToken  pay;
    TestWheel w;
    address constant GOV = address(0xA0);
    address constant CALLER = address(0xCA11);

    function setUp() public {
        pay = new PayToken();
        w = new TestWheel(address(pay), GOV);
        vm.roll(100);
    }

    function test_kinds_concatenate_in_base_order() public {
        w.setA(2);
        w.setB(3);
        assertEq(w.slotCount(), 5);
        w.setAPending(1, 1);          // slot 1
        w.setBPending(0, 1);          // slot 2 (after A's two)
        assertEq(w.pending(), 2);
        (uint256 work,) = w.tick(10, 0);
        assertEq(work, 2);
        assertEq(w.aRan(0), 1);
        assertEq(w.bRan(0), 0);
    }

    function test_round_robin_respects_max_work() public {
        w.setA(3);
        w.setAPending(0, 1);
        w.setAPending(1, 1);
        w.setAPending(2, 1);
        (uint256 work,) = w.tick(2, 0);
        assertEq(work, 2);
        assertEq(w.aRanLength(), 2);
        assertEq(w.cursor(), 2);
        (work,) = w.tick(2, 0);
        assertEq(work, 1);
        assertEq(w.aRan(2), 2);
    }

    function test_scan_is_amortized_and_the_idle_block_is_memoized() public {
        w.setA(4);
        (uint256 work,) = w.tick(1, 2);            // half the wheel seen idle
        assertEq(work, 0);
        assertEq(w.idleClock(), 0);
        (work,) = w.tick(1, 2);                    // all four seen idle
        assertEq(w.idleClock(), block.number + 1);
        w.setAPending(2, 1);
        (work,) = w.tick(1, 4);                    // memoized: not even scanned
        assertEq(work, 0);
        assertEq(w.aRanLength(), 0);
        w.rearm();                                  // a basket-touching trade landed
        (work,) = w.tick(1, 4);
        assertEq(work, 1);
        assertEq(w.aRan(0), 2);
    }

    function test_the_memo_is_per_block() public {
        w.setA(1);
        w.tick(1, 0);
        assertEq(w.idleClock(), block.number + 1);
        w.setAPending(0, 1);
        vm.roll(block.number + 1);                  // a new block: scanned afresh
        (uint256 work,) = w.tick(1, 0);
        assertEq(work, 1);
    }

    function test_reserve_pays_kappa_to_working_ticks_only() public {
        w.setA(1);
        pay.mint(address(this), 500e18);
        pay.approve(address(w), type(uint256).max);
        w.fund(500e18);
        assertEq(w.reserve(), 500e18);
        vm.prank(CALLER);
        (, uint256 paid) = w.tick(1, 0);             // idle: nothing
        assertEq(paid, 0);
        w.setAPending(0, 1);
        vm.roll(block.number + 1);
        vm.prank(CALLER);
        (, paid) = w.tick(1, 0);                     // working: kappa (10%) of 500
        assertEq(paid, 50e18);
        assertEq(pay.balanceOf(CALLER), 50e18);
        assertEq(w.reserve(), 450e18);
    }

    function test_reserve_builds_when_undercalled_and_pays_less_when_overcalled() public {
        w.setA(1);
        pay.mint(address(this), 1_000e18);
        pay.approve(address(w), type(uint256).max);
        w.fund(100e18);
        w.fund(100e18);                              // under-called: it piles up
        w.setAPending(0, 3);
        (, uint256 p1) = w.tick(1, 0);
        vm.roll(block.number + 1);
        (, uint256 p2) = w.tick(1, 0);
        vm.roll(block.number + 1);
        (, uint256 p3) = w.tick(1, 0);               // over-called: each pays less
        assertEq(p1, 20e18);
        assertGt(p1, p2);
        assertGt(p2, p3);
    }

    function test_funding_beyond_the_cap_is_refused() public {
        vm.prank(GOV);
        w.setReserveParams(1000, 100e18);
        pay.mint(address(this), 300e18);
        pay.approve(address(w), type(uint256).max);
        w.fund(300e18);
        assertEq(w.reserve(), 100e18);
        assertEq(pay.balanceOf(address(this)), 200e18);
    }

    function test_governance_only() public {
        vm.expectRevert(WorkWheel.NotGovernance.selector);
        w.setReserveParams(1, 1);
    }
}
