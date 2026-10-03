// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BasketMachineWheel} from "../../src/wheel/BasketMachineWheel.sol";

contract MachineOpsMock {
    bool public ready;
    uint256 public calls;
    function epochNow() external pure returns (uint32) { return 7; }
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
}
