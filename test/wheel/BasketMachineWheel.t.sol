// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BasketMachineWheel} from "../../src/wheel/BasketMachineWheel.sol";

contract MachineOpsMock {
    bool public ready;
    uint256 public calls;
    function epochNow() external pure returns (uint32) { return 7; }
    function monetaryEffort() external pure returns (int32, bool) { return (100, false); }
    function setReady() external { ready = true; }
    function monetaryOperation() external returns (uint8) { require(ready, "not sampled"); ++calls; return 1; }
}

contract MachineSampleMock {
    uint64 public lastSampleAt;
    MachineOpsMock public ops;
    bool public broken;
    constructor(MachineOpsMock ops_) { ops = ops_; }
    function setBroken(bool b) external { broken = b; }
    function isDue() external view returns (bool) { return lastSampleAt == 0; }
    function observe() external { require(!broken, "unavailable"); lastSampleAt = uint64(block.timestamp); ops.setReady(); }
}

contract MachineEquityMock {
    uint256 public calls;
    function constituentsLength() external pure returns (uint256) { return 3; }
    function wheelDue(uint8 kind, uint256) external pure returns (bool) { return kind == 2; }
    function wheelStep(uint8 kind, uint256 i) external returns (uint256) { require(kind == 2 && i == 0); ++calls; return 1; }
}

/// @dev A desk and its policy in one: the effort it advises, and an operation that may be refused.
contract MachineDeskMock {
    int32 public effort = 100;
    bool public refuse;
    uint256 public calls;
    function epochNow() external view returns (uint32) { return uint32(block.timestamp / 1 days); }
    function monetaryEffort() external view returns (int32, bool) { return (effort, false); }
    function set(int32 effort_, bool refuse_) external { effort = effort_; refuse = refuse_; }
    /// A refusal returns no action (the wheel treats a caught revert the same), so calls still count.
    function monetaryOperation() external returns (uint8) { ++calls; return refuse ? 0 : 1; }
}

contract BasketMachineWheelTest is Test {
    function test_retryAfterSampleSucceedsInSameEpochNextBlock() public {
        vm.warp(10_000); vm.roll(100);
        BasketMachineWheel w = new BasketMachineWheel(address(1), address(2), address(this), 0, 0);
        MachineOpsMock ops = new MachineOpsMock();
        MachineSampleMock sample = new MachineSampleMock(ops);
        w.setOps(address(ops), address(ops));
        w.tick(10, 0); // Missing observation: refusal must not consume the epoch.
        assertEq(ops.calls(), 0); assertEq(w.opsEpochSeen(), 0);
        w.setSample(address(sample));
        vm.roll(101); w.tick(10, 0);
        assertEq(ops.calls(), 1); assertEq(w.opsEpochSeen(), 8);
        vm.roll(102); w.tick(10, 0); assertEq(ops.calls(), 1);
    }

    function test_failedSampleDoesNotRewardOrBlockOtherKinds() public {
        vm.warp(10_000); vm.roll(100);
        BasketMachineWheel w = new BasketMachineWheel(address(1), address(2), address(this), 0, 0);
        MachineOpsMock ops = new MachineOpsMock();
        MachineSampleMock sample = new MachineSampleMock(ops); sample.setBroken(true);
        w.setSample(address(sample));
        (uint256 work,) = w.tick(10, 0); assertEq(work, 0);
        MachineEquityMock equity = new MachineEquityMock(); w.setEquity(address(equity));
        assertEq(w.slotCount(), 6, "N+2 plus sample");
        (work,) = w.tick(10, 0); assertEq(work, 1); assertEq(equity.calls(), 1);
    }

    /// @notice The desk's slot is due only when its policy advises an operation (owner, 2026-11-20):
    ///         without advice its run reverts NoAdvice, and each such run cost a keeper a tick.
    function test_theDesksSlotIsDueOnlyWhenItsPolicyAdvises() public {
        vm.warp(10_000); vm.roll(100);
        BasketMachineWheel w = new BasketMachineWheel(address(1), address(2), address(this), 0, 0);
        MachineDeskMock desk = new MachineDeskMock();
        w.setOps(address(desk), address(desk));
        desk.set(0, false);
        (bool due,) = w.pending();
        assertFalse(due, "no advice: nothing to do");
        w.tick(10, 0);
        assertEq(desk.calls(), 0, "never run without advice");
        desk.set(100, false);
        vm.roll(101);                                   // the wheel remembers a block it found idle
        (due,) = w.pending();
        assertTrue(due);
        (uint256 work,) = w.tick(10, 0);
        assertEq(work, 1); assertEq(desk.calls(), 1);
    }

    /// @notice An advised operation the desk refuses waits for the hour (or Buck's relief budget), not
    ///         for other slots' work: an arbitrage cycle no longer reopens it every rotation.
    function test_aRefusedDeskWaitsForTheHourNotForOtherSlotsWork() public {
        vm.warp(10_000); vm.roll(100);
        BasketMachineWheel w = new BasketMachineWheel(address(1), address(2), address(this), 0, 0);
        MachineDeskMock desk = new MachineDeskMock();
        MachineEquityMock equity = new MachineEquityMock();     // a response slot that always works
        w.setOps(address(desk), address(desk));
        w.setEquity(address(equity));
        desk.set(100, true);
        (uint256 work,) = w.tick(10, 0);
        assertEq(work, 1, "the response worked"); assertEq(desk.calls(), 1, "the desk was refused");
        for (uint256 b = 101; b < 106; ++b) {
            vm.roll(b);
            (work,) = w.tick(10, 0);
            assertEq(work, 1, "the response keeps working");
            assertEq(desk.calls(), 1, "and does not reopen the refused desk");
        }
        vm.warp(10_000 + w.RETRY_SPAN()); vm.roll(106);
        w.tick(10, 0);
        assertEq(desk.calls(), 2, "retried when the hour turns");
    }
}
