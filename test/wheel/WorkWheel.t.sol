// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}  from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

import {WorkWheel, CompletionMemo} from "../../src/wheel/WorkWheel.sol";

/// The chassis (doc/BASKET-WHEEL.org 8): two mock kinds composed the way the
/// real ones are -- each claims the slots after its bases' and hands the rest
/// to super -- so these tests pin the layout, the round-robin, the amortized
/// scan and its idle memo, the re-arm, and the reserve.

contract PayToken is ERC20 {
    constructor() ERC20("Buck", "BUCK") {}
    function mint(address to, uint256 a) external { _mint(to, a); }
}

contract WheelValuationMock {
    uint256 public mark;
    bytes32 public key;
    bool public fail;
    function set(uint256 mark_, bytes32 key_, bool fail_) external { mark = mark_; key = key_; fail = fail_; }
    function wheelValuation() external view returns (uint256, bytes32) {
        require(!fail, "unavailable");
        return (mark, key);
    }
}

/// @dev Its ABI matches the source but attempts a state write. The wheel's
///      view interface must force STATICCALL and reject this implementation.
contract MutatingWheelValuation {
    uint256 public calls;
    function wheelValuation() external returns (uint256, bytes32) {
        calls++;
        return (1, bytes32(uint256(2)));
    }
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

/// @dev Slots due on the view that may do nothing when run (a cycle that lost its edge, a desk
///      that declines), over settable inputs: the completion view's subject.
contract IdleWheel is WorkWheel, CompletionMemo {
    uint256 public slots;
    mapping(uint256 => bool) public due;
    mapping(uint256 => uint256) public works;      // runs left that do work; then idle
    uint256 public runs;
    bytes32 public inputs = bytes32(uint256(1));
    bool public clockInputs;

    constructor(address pay, address gov) WorkWheel(pay, gov, 1000, 1_000e18) {}

    function set(uint256 n) external { slots = n; }
    function setDue(uint256 i, bool d, uint256 w) external { due[i] = d; works[i] = w; }
    function setInputs(bytes32 x) external { inputs = x; }
    function useClock() external { clockInputs = true; }

    function _slotCount() internal view override returns (uint256) { return slots; }
    function _due(uint256 s) internal view override returns (bool) { return due[s]; }
    function _run(uint256 s) internal override returns (uint256) {
        runs++;
        if (works[s] == 0) return 0;
        works[s]--;
        if (works[s] == 0) due[s] = false;
        return 1;
    }
    function _settled(uint256 s) internal view override(WorkWheel, CompletionMemo) returns (bool) {
        return super._settled(s);
    }
    function _ranIdle(uint256 s) internal override(WorkWheel, CompletionMemo) { super._ranIdle(s); }
    function _moved() internal override(WorkWheel, CompletionMemo) { super._moved(); }
    function _rearmed() internal override(WorkWheel, CompletionMemo) { super._rearmed(); }
    function _inputs() internal view override returns (bytes32) {
        return clockInputs ? super._inputs() : inputs;
    }
}

/// @dev A market/funding callback inside a work job tries another scan and
///      reserve pull. Only the independent valuation publication may proceed.
contract CallbackWheel is WorkWheel {
    bool public ran;
    constructor(address pay, address gov) WorkWheel(pay, gov, 1000, 1_000e18) {}
    function _slotCount() internal pure override returns (uint256) { return 1; }
    function _due(uint256) internal view override returns (bool) { return !ran; }
    function _run(uint256) internal override returns (uint256) {
        (bool ok, bytes memory reason) = address(this).call(abi.encodeCall(this.tick, (1, 0)));
        require(!ok && bytes4(reason) == WheelReentrant.selector, "nested tick accepted");
        (ok, reason) = address(this).call(abi.encodeCall(this.fund, (0)));
        require(!ok && bytes4(reason) == WheelReentrant.selector, "nested funding accepted");
        this.refreshValuation();
        ran = true;
        return 1;
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

    function test_workCallbacksCannotReenterButCanPublishStaticValuation() public {
        CallbackWheel cb = new CallbackWheel(address(pay), GOV);
        WheelValuationMock source = new WheelValuationMock();
        source.set(42e6, bytes32(uint256(7)), false);
        vm.prank(GOV); cb.setValuationSource(address(source));
        pay.mint(address(this), 100e18); pay.approve(address(cb), 100e18); cb.fund(100e18);
        (uint256 work, uint256 paid) = cb.tick(1, 0);
        assertTrue(cb.ran()); assertEq(work, 1); assertEq(paid, 10e18);
        assertEq(cb.reserve(), 90e18); assertEq(cb.valuationMark(), 42e6);
        assertTrue(cb.valuationReady());
    }

    function test_kinds_concatenate_in_base_order() public {
        w.setA(2);
        w.setB(3);
        assertEq(w.slotCount(), 5);
        w.setAPending(1, 1);          // slot 1
        w.setBPending(0, 1);          // slot 2 (after A's two)
        assertTrue(_pend(w));
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

    // --- Completion: pending() says exactly whether, and where, a tick would work ------------ //

    function _pend(WorkWheel x) internal view returns (bool due) { (due,) = x.pending(); }

    function _next(WorkWheel x) internal view returns (bool found, uint256 steps) { return x.pending(); }

    function test_a_slot_that_ran_idle_is_settled_until_its_inputs_move() public {
        IdleWheel iw = new IdleWheel(address(pay), GOV);
        iw.set(3);
        iw.setDue(1, true, 0);                       // due on the view, nothing on the run
        assertTrue(_pend(iw));
        (bool found, uint256 steps) = _next(iw);
        assertTrue(found);
        assertEq(steps, 1);
        (uint256 work,) = iw.tick(1, steps + 1);
        assertEq(work, 0);
        assertEq(iw.runs(), 1);
        assertFalse(_pend(iw), "settled: a tick now finds nothing");
        (found,) = _next(iw);
        assertFalse(found);
        vm.roll(200);                                // later blocks, unchanged inputs: still settled
        vm.warp(block.timestamp + 1 days);
        assertFalse(_pend(iw));
        iw.tick(10, 0);
        assertEq(iw.runs(), 1, "not run again in the same state");
        vm.roll(300);                                // (the whole wheel was found idle in that block)
        iw.setInputs(bytes32(uint256(2)));           // a trade on a market it reads
        assertTrue(_pend(iw));
        iw.tick(10, 0);
        assertEq(iw.runs(), 2);
        assertFalse(_pend(iw));
        iw.rearm();                                  // a re-arm: tried again
        assertTrue(_pend(iw));
    }

    function test_work_reopens_the_slots_found_idle() public {
        IdleWheel iw = new IdleWheel(address(pay), GOV);
        iw.set(2);
        iw.setDue(0, true, 0);                       // idle on the run
        iw.setDue(1, true, 2);                       // two runs of work
        (uint256 work,) = iw.tick(10, 0);            // 0 idle, 1 works: the state moved
        assertEq(work, 1);
        assertTrue(_pend(iw), "the idle slot is tried again after work");
        uint256 turns;
        for (; turns < 10; turns++) {               // the keeper: tick toward each ready slot
            (bool found, uint256 steps) = _next(iw);
            if (!found) break;
            iw.tick(1, steps + 1);
            vm.roll(200 + turns);
        }
        assertFalse(_pend(iw));
        assertEq(iw.works(1), 0, "all the work done");
        assertLt(turns, 10, "and it settles");
    }

    function test_by_default_an_idle_slot_waits_for_the_next_block() public {
        IdleWheel iw = new IdleWheel(address(pay), GOV);
        iw.useClock();
        iw.set(1);
        iw.setDue(0, true, 0);
        iw.tick(1, 0);
        assertFalse(_pend(iw));
        vm.roll(200);
        assertTrue(_pend(iw));
    }

    function test_next_due_points_a_one_slot_tick_at_the_next_ready_slot() public {
        w.setA(4);
        w.setAPending(2, 1);
        w.setAPending(3, 1);
        (bool found, uint256 steps) = _next(w);
        assertTrue(found);
        assertEq(steps, 2);
        (uint256 work,) = w.tick(1, steps + 1);
        assertEq(work, 1);
        assertEq(w.aRan(0), 2);
        assertEq(w.cursor(), 3);
        (found, steps) = _next(w);
        assertEq(steps, 0);
        w.tick(1, steps + 1);
        (found,) = _next(w);
        assertFalse(found);
        assertFalse(_pend(w));
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

    function test_valuationPublishesWithoutRunningSlotsOrPayingRewards() public {
        WheelValuationMock source = new WheelValuationMock();
        source.set(42e6, bytes32(uint256(7)), false);
        vm.expectRevert(WorkWheel.NotGovernance.selector);
        w.setValuationSource(address(source));
        vm.prank(GOV);
        w.setValuationSource(address(source));
        w.setA(1);
        w.setAPending(0, 1);
        pay.mint(address(this), 100e18);
        pay.approve(address(w), 100e18);
        w.fund(100e18);
        vm.prank(CALLER);
        (uint256 mark, bytes32 key) = w.refreshValuation();
        assertEq(mark, 42e6);
        assertEq(key, bytes32(uint256(7)));
        assertEq(w.valuationMark(), mark);
        assertEq(w.valuationPositionKey(), key);
        assertEq(w.valuationBlock(), 100);
        assertEq(w.valuationTimestamp(), block.timestamp);
        assertTrue(w.valuationReady());
        assertEq(w.aPending(0), 1);
        assertEq(w.cursor(), 0);
        assertEq(w.reserve(), 100e18);
        assertEq(pay.balanceOf(CALLER), 0);

        // A same-block market change must be sampled on the next refresh.
        source.set(37e6, bytes32(uint256(7)), false);
        w.refreshValuation();
        assertEq(w.valuationMark(), 37e6);
    }

    function test_valuationFailuresDoNotPublishAndSourceChangeInvalidates() public {
        WheelValuationMock source = new WheelValuationMock();
        source.set(42e6, bytes32(uint256(7)), false);
        vm.prank(GOV);
        w.setValuationSource(address(source));
        w.refreshValuation();
        source.set(5, bytes32(uint256(8)), true);
        vm.expectRevert(bytes("unavailable"));
        w.refreshValuation();
        assertEq(w.valuationMark(), 42e6);
        assertEq(w.valuationPositionKey(), bytes32(uint256(7)));
        assertTrue(w.valuationReady());

        MutatingWheelValuation mutating = new MutatingWheelValuation();
        vm.prank(GOV);
        w.setValuationSource(address(mutating));
        assertFalse(w.valuationReady());
        (bool ok,) = address(w).call{gas: 100_000}(abi.encodeCall(w.refreshValuation, ()));
        assertFalse(ok);
        assertEq(mutating.calls(), 0);
        assertFalse(w.valuationReady());
    }
}
