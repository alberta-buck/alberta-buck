// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BasketDeskTest} from "./BasketDesk.t.sol";
import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";
import {ControlledPairIntent} from "./BasketUpkeepFacet.t.sol";
import {BuckBasketEquityStorage} from "../../src/basket/BuckBasketEquityStorage.sol";
import {BasketVenueFacet} from "../../src/basket/BasketVenueFacet.sol";
import {BasketReference} from "../../src/basket/BasketReference.sol";
import {OuterBandDeskPolicy} from "../../src/basket/OuterBandDeskPolicy.sol";
import {BasketMachineWheel} from "../../src/wheel/BasketMachineWheel.sol";
import {bindCarryingPool} from "../harness/CarryingPool.sol";
import {MonetaryDesk} from "../../src/basket/MonetaryDesk.sol";
import {BuckBasketStorage} from "../../src/basket/BuckBasketStorage.sol";

contract BasketMachineIntegrationTest is BasketDeskTest {
    BasketReference internal service;
    OuterBandDeskPolicy internal policy;
    BasketMachineWheel internal machine;

    function _machine() internal {
        service = new BasketReference(address(b), 1800, 3600, 500);
        policy = new OuterBandDeskPolicy(address(service), 300, 200, 2500, 40, 3);
        machine = new BasketMachineWheel(address(buck), address(0xDEAD), GOV, 0, 0);
        bindCarryingPool(reg, address(machine));
        vm.startPrank(GOV);
        b.setVenue(address(new BasketVenueFacet()));
        b.setEquityWheel(address(new BasketUpkeepFacet(address(new ControlledPairIntent(address(b))))));
        b.setResponse(BuckBasketEquityStorage.ResponseParams(3000, 1800, 0, 0));
        (bool ok,) = address(b).call(abi.encodeWithSignature("setWheel(address)", address(machine)));
        require(ok);
        desk.setMonetaryDirector(address(policy));
        desk.setExecutionGuard(address(policy));
        machine.setEquity(address(b));
        machine.setOps(address(desk), address(policy));
        machine.setSample(address(policy));
        vm.stopPrank();
    }

    function test_integration_sparseWheelCallsSampleBeforeTrading() public {
        _placed(); _grantDesk(0, 100_000e18); _machine();
        for (uint256 i; i < 3; ++i) _push(i, true, 25_000 * B);
        uint256 budget = desk.turnoverRemaining();
        vm.warp(vm.getBlockTimestamp() + 6 hours); vm.roll(vm.getBlockNumber() + 1);
        uint256 beforeGas = gasleft();
        (uint256 work,) = machine.tick(12, 0);
        emit log_named_uint("three-leg machine tick gas (same transaction fixture)", beforeGas - gasleft());
        assertGt(work, 0);
        assertTrue(service.fresh());
        assertLt(desk.turnoverRemaining(), budget, "ops consumed the newly published sample");
        assertEq(int256(desk.monetaryBuckHeld()) - desk.monetaryOutstanding(), buck.signedBalanceOf(address(desk)));
        assertGt(policy.side(), 0);
        uint256 afterBudget = desk.turnoverRemaining();
        vm.roll(vm.getBlockNumber() + 1); machine.rearm(); machine.tick(12, 0);
        assertEq(desk.turnoverRemaining(), afterBudget, "successful epoch cannot run twice");
    }

    function test_integration_outerBandTerminalGuardRollsBackOvershoot() public {
        _placed();
        for (uint256 i; i < 3; ++i) _grantDesk(i, 1_000_000e18);
        _machine();
        for (uint256 i; i < 3; ++i) _push(i, true, 20_000 * B);
        vm.warp(vm.getBlockTimestamp() + 1800); vm.roll(vm.getBlockNumber() + 1); policy.observe();
        // Deliberately oversized advice. Even a misconfigured director may
        // not spend through the independent release-band execution guard.
        vm.startPrank(GOV);
        desk.setMonetaryDirector(address(md));
        desk.setOpsParams(MonetaryDesk.OpsParams(500, 10000, 10000, true));
        vm.stopPrank();
        uint256 budget = desk.turnoverRemaining();
        md.set(-10000, false);
        vm.expectRevert(); desk.monetaryOperation();
        assertEq(desk.turnoverRemaining(), budget);
        assertEq(desk.monetaryBuckHeld(), 0);
    }

    function testFuzz_integration_responseKeepsFreshBooksAfterExternalChanges(uint64 seed) public {
        _deposit(alice, address(tok[0]), 100_000e18);
        _grantDesk(0, 100_000e18); _machine();
        for (uint256 step; step < 8; ++step) {
            seed = uint64(uint256(keccak256(abi.encode(seed, step))));
            uint256 i = seed % 3;
            _push(i, seed % 2 == 0, seed % 2 == 0 ? (100 + uint256(seed) % 3000) * B : (100 + uint256(seed) % 1000) * 1e18);
            ctrl.setK((20 + seed % 65) * 1e16);
            vm.warp(vm.getBlockTimestamp() + 6 hours); vm.roll(vm.getBlockNumber() + 1);
            int256 account = buck.signedBalanceOf(address(b));
            int256 low = int256(b.grossAt(2)) + account + int256(buck.reliefOf(address(b)));
            bool underwater = account < 0 && uint256(-account) > ctrl.k() * uint256(low > 0 ? low : int256(0)) / 1e18;
            machine.rearm(); machine.tick(12, 0);
            if (underwater) assertGe(buck.signedBalanceOf(address(b)), account, "no draw while underwater");
            for (uint256 leg; leg < 3; ++leg) {
                assertGe(tok[leg].balanceOf(address(b)), b.idleToken(leg));
                assertGe(tok[leg].balanceOf(address(desk)), desk.monetaryTokenHeld(leg));
            }
            assertGe(desk.monetaryOutstanding(), 0, "no synthetic retirement");
        }
    }
}
