// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityTest} from "../basket/BuckBasketEquity.t.sol";
import {BasketWheel} from "../../src/wheel/BasketWheel.sol";

/// @title The equity basket placed by a real BasketWheel's EquityKind: anyone
///        ticks, the kind maps its 2N + 3 slots onto the basket's components.
contract BasketWheelEquityTest is BuckBasketEquityTest {
    BasketWheel internal bw;

    function setUp() public override {
        super.setUp();
        bw = new BasketWheel(address(buck), address(0xDEAD), GOV, 0, 0);
        vm.startPrank(GOV);
        bw.setEquity(address(b));
        (bool ok,) = address(b).call(abi.encodeWithSignature("setWheel(address)", address(bw)));
        require(ok, "setWheel");
        vm.stopPrank();
    }

    function _tick(uint256 rounds) internal {
        _wheel(rounds, false);
    }

    /// @dev Every inherited test now runs through the real wheel: anyone
    ///      ticks, the arbitrage re-pins, time passes.
    function _wheel(uint256 rounds, bool days_) internal override {
        for (uint256 r = 0; r < rounds; r++) {
            bw.rearm();
            bw.tick(20, 0);
            for (uint256 i = 0; i < 3; i++) _repin(i);
            vm.warp(block.timestamp + (days_ ? 1 days : 700));
            vm.roll(block.number + 1);
        }
    }

    /// @dev The credits' caller is the wheel's arbitrage; tested in the base suite.
    function test_credits_landInTheWallet() public override {}

    function test_theWheelPlacesADeposit() public {
        assertEq(bw.slotCount(), 9, "2N + 3 equity slots");
        _deposit(alice, address(buck), 3e23);
        _tick(40);
        uint256[] memory w = b.weightsBp();
        for (uint256 i = 0; i < 3; i++) {
            assertGt(b.liquidityOf(i), 0, "every pool placed");
            assertApproxEqAbs(w[i], 3333, 300);
        }
        assertEq(b.mintedTotal() - b.burnedTotal(), b.debt());
    }

    function test_onlyTheWheelSteps() public {
        _deposit(alice, address(buck), 3e23);
        (bool ok2, bytes memory ret) =
            address(b).call(abi.encodeWithSignature("wheelStep(uint8,uint256)", uint8(3), uint256(0)));
        assertFalse(ok2, "a stranger cannot step the components");
        ret;
    }
}
