// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BlockPairSignalsTest} from "./BlockPairSignals.t.sol";
import {BlockPairSignals} from "../../src/basket/BlockPairSignals.sol";
import {PairRebalancePolicy} from "../../src/basket/PairRebalancePolicy.sol";

contract PairBookView {
    BlockPairSignals immutable s;
    constructor(BlockPairSignals s_) { s = s_; }
    function constituentsLength() external view returns (uint256) { return s.tokenCount(); }
    function constituents(uint256 i) external view returns (address, uint8, uint256, uint256, uint24, address, int24, int24, bool, uint256, uint128) {
        (address pool, bool token0,) = s.routes(i);
        return (s.tokens(i), 18, 0, 0, 3000, pool, 0, 0, !token0, 3333, 0);
    }
}

contract PairRebalancePolicyTest is BlockPairSignalsTest {
    function test_policy_departureQuenchesBrakingReleasesAndClosureStrengthens() public {
        PairRebalancePolicy p = new PairRebalancePolicy(address(s), address(new PairBookView(s)));
        p.observe(); assertEq(p.desire(0, 1), 0, "unready is not stationary");
        warm(); p.observe();
        for (uint256 k = 1; k <= 100; ++k) {
            setTicks(int24(int256(k * 12)), 0, 0, 0); step(12); p.observe();
        }
        assertEq(p.desire(0, 1), 0, "steady departure is quenched");
        for (uint256 k; k < 10; ++k) { step(12); p.observe(); }
        assertGt(s.pair(0, 1).rate, 0);
        uint256 braking = p.desire(0, 1);
        assertGt(braking, 0, "participate before the rate crosses zero");
        for (uint256 k = 1; k <= 60; ++k) {
            setTicks(1200 - int24(int256(k * 12)), 0, 0, 0); step(12); p.observe();
        }
        assertGt(p.desire(0, 1), braking, "closure increases participation");
        uint256 desire = p.desire(0, 1);
        p.observe(); p.observe(); assertEq(p.desire(0, 1), desire, "duplicate reads do not reset evidence");
    }

    function test_policy_ownImpactWidensUncertaintyAndRequiresBasketAuthority() public {
        address book = address(new PairBookView(s));
        PairRebalancePolicy p = new PairRebalancePolicy(address(s), book);
        warm(); p.observe();
        for (uint256 k = 1; k <= 100; ++k) {
            setTicks(-int24(int256(k * 12)), 0, 0, 0); step(12); p.observe();
        }
        uint256 before = p.desire(0, 1); assertGt(before, 0);
        vm.expectRevert(bytes("basket only")); p.recordImpact(0, 0, 4000);
        vm.prank(book); p.recordImpact(0, 0, 4000);
        assertLt(p.desire(0, 1), before);
        assertEq(p.remainingImpact(0), 4000e18);
        vm.warp(vm.getBlockTimestamp() + 2 days);
        assertEq(p.remainingImpact(0), 0);
    }

    function test_policy_fractionIsTimeScaledAndCatchUpIsBounded() public {
        PairRebalancePolicy p = new PairRebalancePolicy(address(s), address(new PairBookView(s)));
        assertEq(p.fraction(1e18, 0), 0);
        assertLt(p.fraction(1e18, 12), p.fraction(1e18, 3600));
        assertEq(p.fraction(1e18, 1 days), 0.5e18);
        assertEq(p.fraction(1e18, 30 days), p.fraction(1e18, 1 days));
    }

    function test_policy_onlyExecutedTransfersResetElapsedBudget() public {
        address book = address(new PairBookView(s));
        PairRebalancePolicy p = new PairRebalancePolicy(address(s), book);
        uint64 deployedAt = p.lastExecutionAt();
        warm(); p.observe();
        assertGt(block.timestamp, deployedAt);
        assertEq(p.lastExecutionAt(), deployedAt, "sampling preserves unspent elapsed time");
        vm.prank(book); p.recordImpact(0, 0, 40);
        assertEq(p.lastExecutionAt(), deployedAt, "other basket operations preserve the pair clock");
        vm.expectRevert(bytes("basket only")); p.recordExecution();
        vm.prank(book); p.recordExecution();
        assertEq(p.lastExecutionAt(), block.timestamp);
    }
}
