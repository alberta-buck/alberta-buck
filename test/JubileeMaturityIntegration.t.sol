// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckNet} from "../src/BuckNet.sol";
import {JubileeFund, IJubileeBasket} from "../src/JubileeFund.sol";
import {JubileeClaims} from "../src/lib/JubileeClaims.sol";
import {BuckLedger} from "../src/lib/BuckLedger.sol";
import {NetK} from "./BuckNet.t.sol";
import {JubileeBasketMock} from "./JubileeFund.t.sol";

/// @dev Real token, NFT, claim book and fund; the basket is a controlled
///      mock. Kept separate from the larger fund/renewal suite so another
///      inherited test does not exceed Solidity's assembly jump-tag limit.
contract JubileeMaturityIntegrationTest is Test {
    uint256 constant YEAR = 365 days + 6 hours;
    uint256 constant Q = 1e36;
    address constant GOV = address(0xA0);
    address constant ALICE = address(0xA11CE);
    address constant BOB = address(0xB0B);
    address constant INSURER = address(0x1551E1);

    IdentityRegistryHarness identity;
    BuckCredit credit;
    BuckNet buck;
    JubileeFund fund;
    JubileeBasketMock basket;
    uint256 t0;

    function setUp() public {
        vm.warp(1_000_000);
        t0 = block.timestamp;
        identity = new IdentityRegistryHarness(GOV);
        credit = new BuckCredit();
        NetK k = new NetK();
        fund = new JubileeFund(GOV);
        buck = new BuckNet(address(credit), address(k), address(identity), address(fund));
        basket = new JubileeBasketMock(address(buck));
        credit.setBuck(address(buck));
        vm.prank(GOV);
        identity.setBuck(address(buck));
        _bind(ALICE, false);
        _bind(BOB, false);
        _bind(INSURER, false);
        _bind(address(fund), true);
        _bind(address(basket), false);
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER, true);
        vm.prank(GOV);
        fund.configure(buck, IJubileeBasket(address(basket)));
    }

    function _bind(address target, bool carrying) private {
        if (target.code.length == 0) vm.etch(target, hex"60006000fd");
        identity.bindContract(target, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, carrying);
    }

    function _credit(uint256 face) private returns (uint256 id) {
        vm.prank(INSURER);
        id = credit.createCredit(ALICE, 0, face, face, BuckCredit.DepreciationType.NONE, 0, 0, 0);
        vm.prank(ALICE);
        buck.issue(id, face, face);
    }

    function test_simultaneousFiftyYearMaturitiesRequirePermissionlessBatchesBeforeRelease() public {
        uint256 first;
        for (uint256 i; i < 16; ++i) {
            uint256 id = _credit(100e6);
            if (i == 0) first = id;
        }
        vm.warp(t0 + YEAR);
        vm.prank(GOV);
        (uint256 position,,) = fund.invest(10e6, type(uint256).max, 10e6, 1);
        (uint128 sharesBefore,) = basket.holdings(position);
        vm.warp(t0 + 50 * YEAR);
        (, uint256 cashBefore) = buck.fundBalancesScaled();
        uint256 ownerBefore = buck.balanceOf(ALICE);

        // A stale denominator must not release the NFT or alter the fund,
        // even when only part of the maturity backlog has been processed.
        vm.expectRevert(JubileeClaims.PendingMaturities.selector);
        vm.prank(ALICE);
        fund.closeMatured(first);
        for (uint256 batch; batch < 4; ++batch) {
            vm.prank(BOB);
            (uint256 processed, bool current) = buck.processMaturities(4);
            assertEq(processed, 4);
            assertEq(current, batch == 3);
            if (!current) {
                vm.expectRevert(JubileeClaims.PendingMaturities.selector);
                vm.prank(ALICE);
                fund.closeMatured(first);
            }
        }
        (uint128 sharesAfterBacklog,) = basket.holdings(position);
        (, uint256 cashAfterBacklog) = buck.fundBalancesScaled();
        assertEq(sharesAfterBacklog, sharesBefore);
        assertEq(cashAfterBacklog, cashBefore);
        assertEq(buck.balanceOf(ALICE), ownerBefore);
        assertEq(credit.ownerOf(first), ALICE);
        assertEq(buck.claimSeconds(), 16 * 100e6 * 50 * YEAR);

        vm.prank(ALICE);
        fund.closeMatured(first);
        (uint128 sharesAfterRelease,) = basket.holdings(position);
        assertEq(sharesAfterRelease, sharesBefore);
        (, uint256 cashAfterRelease) = buck.fundBalancesScaled();
        assertEq(cashAfterRelease, cashBefore);
        assertEq(buck.balanceOf(ALICE), ownerBefore);
        vm.expectRevert();
        credit.ownerOf(first);
        assertEq(buck.totalIssued(), 16 * 100e6);
        assertEq(buck.claimBook().deferredPrincipal(), 100e6);
        assertEq(buck.claimSeconds(), 16 * 100e6 * 50 * YEAR);
    }

    function test_unclosedOldCreditKeepsPayingFeesButItsClaimStopsAtFiftyYears() public {
        uint256 old = _credit(100e6);
        vm.warp(t0 + 50 * YEAR);
        buck.processMaturities(1);
        uint256 newer = _credit(100e6);
        vm.warp(t0 + 100 * YEAR);
        buck.processMaturities(1);
        (uint256 oldSeconds, uint256 total) = buck.selectedClaimSeconds(old, 100e6);
        (uint256 newSeconds,) = buck.selectedClaimSeconds(newer, 100e6);
        assertEq(oldSeconds, 100e6 * 50 * YEAR);
        assertEq(newSeconds, oldSeconds);
        assertEq(total, 2 * oldSeconds);
        BuckLedger.FundFlows memory flows = buck.fundFlows();
        assertEq(flows.grossDemurrage.units, 300e6);
        assertEq(flows.grossDemurrage.fraction, 0);
        assertEq(buck.totalIssued(), 200e6);
        (, uint256 cashBefore) = buck.fundBalancesScaled();
        uint256 ownerBefore = buck.balanceOf(ALICE);
        vm.prank(ALICE);
        fund.closeMatured(old);
        assertEq(buck.totalIssued(), 200e6);
        assertEq(buck.claimBook().deferredPrincipal(), 100e6);
        assertEq(buck.balanceOf(ALICE), ownerBefore);
        (, uint256 cashAfter) = buck.fundBalancesScaled();
        assertEq(cashAfter, cashBefore);
        assertEq(buck.claimSeconds(), total);
    }
}
