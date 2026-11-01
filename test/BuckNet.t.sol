// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckNet} from "../src/BuckNet.sol";
import {BuckLedger} from "../src/lib/BuckLedger.sol";
import {JubileeClaims} from "../src/lib/JubileeClaims.sol";
import {JubileeClaimBook} from "../src/JubileeClaimBook.sol";

contract NetK {
    uint256 public k = 1e18;
    uint256 public factor;
    function setK(uint256 value) external { k = value; }
    function setFactor(uint256 value) external { factor = value; }
    function currentBuckK() external view returns (uint256) { return k; }
    function compute() external view returns (uint256) { return k; }
    function fundingFactor() external view returns (uint256) { return factor; }
}

/// @dev Test only. A production fund must check caller authorization, redeem
///      proportional positions and calculate the payout from actual proceeds.
contract MockNetFund {
    function checkpointIssuance() external pure {}
    function settle(BuckNet token, address owner, uint256 id, uint256 principal, uint256 reliefScaled) external {
        token.settleFromJubilee(owner, id, principal, reliefScaled);
    }
    function mature(BuckNet token, address owner, uint256 id) external {
        token.matureFromJubilee(owner, id);
    }
}

/// @dev Test only. Simulates an insurer investing and restoring its own BUCK.
contract MockNetInsurerPool {
    function approveToken(IERC20 token) external { token.approve(address(token), type(uint256).max); }
    function transferToken(IERC20 token, address to, uint256 amount) external { token.transfer(to, amount); }
}

contract BuckNetTest is Test {
    uint256 constant YEAR = 365 days + 6 hours;
    uint256 constant Q = 1e36;
    address constant GOV = address(0xA0);
    address constant ALICE = address(0xA11CE);
    address constant BOB = address(0xB0B);
    address constant INSURER_A = address(0x1551E1);
    address constant INSURER_B = address(0x1551E2);

    IdentityRegistryHarness internal identity;
    BuckCredit internal credit;
    BuckNet internal buck;
    MockNetFund internal fund;
    NetK internal k;
    uint256 internal t0;

    function setUp() public {
        vm.warp(1_000_000);
        t0 = block.timestamp;
        identity = new IdentityRegistryHarness(GOV);
        credit = new BuckCredit();
        k = new NetK();
        fund = new MockNetFund();
        buck = new BuckNet(address(credit), address(k), address(identity), address(fund));
        credit.setBuck(address(buck));
        vm.prank(GOV);
        identity.setBuck(address(buck));
        _bind(ALICE, false, address(this));
        _bind(BOB, false, address(this));
        _bind(INSURER_A, false, address(this));
        _bind(INSURER_B, false, address(this));
        _bind(address(fund), true, address(this));
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER_A, true);
        vm.prank(BOB);
        credit.setCreditIssuer(INSURER_B, true);
    }

    function _bind(address target, bool carrying, address binder) internal {
        if (target.code.length == 0) vm.etch(target, hex"60006000fd");
        vm.prank(binder);
        identity.bindContract(target, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, carrying);
    }

    function _credit(address owner, address insurer, uint256 face, uint32 rate) internal returns (uint256 id) {
        vm.prank(insurer);
        id = credit.createCredit(owner, 0, face, face, BuckCredit.DepreciationType.NONE, 0, 0, rate);
    }

    function _pool(address insurer) internal returns (MockNetInsurerPool pool) {
        pool = new MockNetInsurerPool();
        _bind(address(pool), true, insurer);
        vm.prank(insurer);
        buck.setInsurerPool(address(pool));
    }

    function _authorize(address owner, uint256 id, uint256 amount, uint256 minRelief) internal {
        vm.prank(owner);
        buck.authorizeReduction(id, amount, minRelief, uint64(block.timestamp + YEAR));
    }

    function test_issuedUnspentIsTheFullErc20BalanceAndCanCloseAfterFiftyYears() public {
        uint256 id = _credit(ALICE, INSURER_A, 1_000_000e6, 0);
        vm.prank(ALICE);
        (uint256 principal, uint256 premium) = buck.issue(id, 1_000_000e6, 1_000_000e6, type(uint256).max, address(0));
        assertEq(principal, 1_000_000e6);
        assertEq(premium, 0);
        assertEq(buck.balanceOf(ALICE), principal);
        assertEq(buck.totalSupply(), principal);
        (uint256 held, uint256 unspent) = buck.heldAndUnspentScaled(ALICE);
        assertEq(held, 0);
        assertEq(unspent, principal * Q);
        vm.warp(t0 + YEAR);
        assertApproxEqAbs(buck.balanceOf(ALICE), 980198673306, 1);
        assertApproxEqAbs(buck.netBalanceOf(address(fund)), 19801326693, 1);
        assertEq(credit.issuanceRelief(id, principal), 20_000e6);
        assertEq(buck.claimSeconds(), principal * YEAR);

        vm.warp(t0 + 50 * YEAR);
        assertTrue(buck.maturitiesPending());
        buck.processMaturities(1);
        uint256 holderBefore = buck.balanceOf(ALICE);
        fund.mature(buck, ALICE, id);
        assertEq(buck.totalSupply(), principal);
        assertEq(buck.balanceOf(ALICE), holderBefore);
        assertEq(buck.claimSeconds(), principal * 50 * YEAR);
        assertEq(buck.claimBook().deferredPrincipal(), principal);
        vm.expectRevert();
        credit.ownerOf(id);
    }

    function test_registryModeChangesKeepNetBalancesAndFundValueConserved() public {
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(id, 100e6, 100e6, type(uint256).max, address(0));
        assertTrue(identity.carryingModeHookEnabled());
        vm.prank(ALICE);
        buck.transfer(BOB, 40e6);
        vm.warp(t0 + YEAR);
        uint256 netBefore = buck.netBalanceOf(BOB);
        (, uint256 fundBefore) = buck.fundBalancesScaled();
        identity.setIsCarrying(BOB, true);
        assertEq(buck.balanceOf(BOB), netBefore);
        assertEq(buck.netBalanceOf(BOB), netBefore);
        (, uint256 fundAfter) = buck.fundBalancesScaled();
        assertEq(fundAfter, fundBefore);
        vm.warp(t0 + 2 * YEAR);
        assertGt(buck.balanceOf(BOB), buck.netBalanceOf(BOB));
        uint256 aliceBefore = buck.netBalanceOf(ALICE);
        vm.prank(BOB);
        buck.transfer(ALICE, 10e6);
        assertLt(buck.netBalanceOf(ALICE) - aliceBefore, 10e6);
        netBefore = buck.netBalanceOf(BOB);
        (, fundBefore) = buck.fundBalancesScaled();
        identity.setIsCarrying(BOB, false);
        assertEq(buck.balanceOf(BOB), netBefore);
        (, fundAfter) = buck.fundBalancesScaled();
        assertEq(fundAfter, fundBefore);
    }

    function test_carryingServicesKeepNominalBalancesAndPassTheirCarriedFee() public {
        MockNetInsurerPool first = new MockNetInsurerPool();
        MockNetInsurerPool second = new MockNetInsurerPool();
        _bind(address(first), true, address(this));
        _bind(address(second), true, address(this));
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(id, 100e6, 100e6, type(uint256).max, address(0));
        vm.prank(ALICE);
        buck.transfer(address(first), 40e6);

        vm.warp(t0 + 10 * YEAR);
        assertEq(buck.balanceOf(address(first)), 40e6);
        uint256 firstNet = buck.netBalanceOf(address(first));
        assertApproxEqAbs(firstNet, 32_749_230, 1);

        (uint256 grossScaled, uint256 netScaled) = buck.accountBalancesScaled(address(first));
        uint256 expected = Math.mulDiv(netScaled, 10e6 * Q, grossScaled);
        vm.expectEmit(true, true, false, true, address(buck));
        emit BuckNet.NetValueTransfer(address(first), address(second), 10e6, expected);
        first.transferToken(buck, address(second), 10e6);
        assertEq(buck.balanceOf(address(first)), 30e6);
        assertEq(buck.balanceOf(address(second)), 10e6);
        uint256 secondNet = buck.netBalanceOf(address(second));
        assertApproxEqAbs(secondNet, firstNet / 4, 1);

        (grossScaled, netScaled) = buck.accountBalancesScaled(address(second));
        expected = Math.mulDiv(netScaled, 10e6 * Q, grossScaled);
        vm.expectEmit(true, true, false, true, address(buck));
        emit BuckNet.NetValueTransfer(address(second), BOB, 10e6, expected);
        second.transferToken(buck, BOB, 10e6);
        assertEq(buck.balanceOf(address(second)), 0);
        assertEq(buck.balanceOf(BOB), secondNet);
        assertLt(buck.balanceOf(BOB), 10e6);
        assertEq(buck.totalSupply(), 100e6);
    }

    function test_activeFacilityCannotSwitchToCarryingAndCanSwitchAfterRetirement() public {
        // An earlier inactive NFT must not conceal a later issued facility.
        _credit(ALICE, INSURER_A, 10e6, 0);
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(id, 100e6, 100e6, type(uint256).max, address(0));
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        identity.setIsCarrying(ALICE, true);
        assertFalse(identity.isCarrying(ALICE));
        assertTrue(credit.hasIssuedPrincipal(ALICE));
        assertEq(buck.balanceOf(ALICE), 100e6);
        _authorize(ALICE, id, 100e6, 0);
        fund.settle(buck, ALICE, id, 100e6, 0);
        assertFalse(credit.hasIssuedPrincipal(ALICE));
        identity.setIsCarrying(ALICE, true);
        assertTrue(identity.isCarrying(ALICE));
    }

    function test_jubileeAndRegisteredInsurerPoolStayCarrying() public {
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(id, 100e6, 100e6, type(uint256).max, address(0));
        MockNetInsurerPool pool = _pool(INSURER_A);
        vm.expectRevert(BuckNet.NotCarryingPool.selector);
        identity.setIsCarrying(address(fund), false);
        vm.expectRevert(BuckNet.NotCarryingPool.selector);
        vm.prank(INSURER_A);
        identity.setIsCarrying(address(pool), false);
        assertTrue(identity.isCarrying(address(fund)));
        assertTrue(identity.isCarrying(address(pool)));
        vm.expectRevert(BuckNet.NotRegistry.selector);
        buck.onCarryingModeChange(BOB, true);
    }

    function test_unspentSurvivesACompletedFacilityAndModeRoundTrip() public {
        uint256 aliceId = _credit(ALICE, INSURER_A, 100e6, 0);
        uint256 bobId = _credit(BOB, INSURER_B, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(aliceId, 100e6, 100e6, type(uint256).max, address(0));
        vm.prank(BOB);
        buck.issue(bobId, 100e6, 100e6, type(uint256).max, address(0));
        vm.prank(BOB);
        buck.transfer(ALICE, 100e6);
        _authorize(ALICE, aliceId, 100e6, 0);
        fund.settle(buck, ALICE, aliceId, 100e6, 0);
        (uint256 held, uint256 unspent) = buck.heldAndUnspentScaled(ALICE);
        assertEq(held, 0);
        assertEq(unspent, 100e6 * Q);
        identity.setIsCarrying(ALICE, true);
        vm.warp(t0 + YEAR);
        uint256 beforeBob = buck.netBalanceOf(BOB);
        vm.prank(ALICE);
        buck.transfer(BOB, 10e6);
        assertApproxEqAbs(buck.netBalanceOf(BOB) - beforeBob, 9_801_986, 1);
        uint256 netBefore = buck.netBalanceOf(ALICE);
        identity.setIsCarrying(ALICE, false);
        assertEq(buck.balanceOf(ALICE), netBefore);
        (held, unspent) = buck.heldAndUnspentScaled(ALICE);
        assertEq(held, 0);
        assertGt(unspent, 0);
    }

    function test_coreContractsFitTheEvmRuntimeSizeLimit() public view {
        assertLe(address(buck).code.length, 24_576);
        assertLe(address(credit).code.length, 24_576);
        assertLe(address(identity).code.length, 24_576);
        assertLe(address(buck.renewalConsent()).code.length, 24_576);
        assertLe(address(buck.claimBook()).code.length, 24_576);
    }

    function test_claimBookOnlyAcceptsTokenSyncAndAllowsBoundedPublicMaturityProcessing() public {
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(id, 100e6, 100e6, type(uint256).max, address(0));
        JubileeClaimBook claims = buck.claimBook();
        assertEq(claims.buck(), address(buck));
        assertEq(address(claims.credit()), address(credit));
        vm.expectRevert(JubileeClaimBook.NotBuck.selector);
        vm.prank(BOB);
        claims.sync(id);
        vm.warp(t0 + YEAR);
        assertEq(claims.positionSeconds(id), 100e6 * YEAR);
        assertEq(claims.totalSeconds(), buck.claimSeconds());
        vm.warp(t0 + 50 * YEAR);
        assertTrue(buck.maturitiesPending());
        vm.prank(BOB);
        (uint256 processed, bool current) = claims.process(1);
        assertEq(processed, 1);
        assertTrue(current);
        assertFalse(buck.maturitiesPending());
        assertEq(buck.claimSeconds(), 100e6 * 50 * YEAR);
    }

    function test_transferAndTransferFromUseHeldAndUnspentWithoutRetiringNft() public {
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(id, 100e6, 100e6, type(uint256).max, address(0));
        vm.prank(ALICE);
        buck.transfer(BOB, 40e6);
        vm.prank(BOB);
        buck.transfer(ALICE, 5e6);
        assertEq(buck.balanceOf(ALICE), 65e6);
        vm.prank(ALICE);
        buck.approve(BOB, 50e6);
        vm.prank(BOB);
        buck.transferFrom(ALICE, BOB, 50e6);
        assertEq(buck.balanceOf(ALICE), 15e6);
        assertEq(buck.allowance(ALICE, BOB), 0);
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(principal, 100e6);
        assertEq(buck.totalIssued(), principal);
    }

    function test_twoNftsKeepDistinctAgesWhenOneFacilityIsReduced() public {
        uint256 old = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(old, 100e6, 100e6, type(uint256).max, address(0));
        vm.warp(t0 + YEAR);
        uint256 newer = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(newer, 100e6, 100e6, type(uint256).max, address(0));
        vm.warp(t0 + 2 * YEAR);
        (uint256 selected, uint256 total) = buck.selectedClaimSeconds(newer, 100e6);
        assertEq(selected, 100e6 * YEAR);
        assertEq(total, 300e6 * YEAR);
        (, uint256 fundNet) = buck.fundBalancesScaled();
        _authorize(ALICE, newer, 100e6, 0);
        fund.settle(buck, ALICE, newer, 100e6, fundNet / 10);
        (uint256 oldPrincipal, uint256 oldSeconds) = credit.issuanceOf(old);
        (uint256 newPrincipal,) = credit.issuanceOf(newer);
        assertEq(oldPrincipal, 100e6);
        assertEq(oldSeconds, 200e6 * YEAR);
        assertEq(newPrincipal, 0);
        assertEq(buck.claimSeconds(), oldSeconds);
    }

    function test_legacyCreditViewsTrackCoveragePremiumAndLiveK() public {
        MockNetInsurerPool pool = _pool(INSURER_A);
        pool.approveToken(IERC20(address(buck)));
        uint256 insured = _credit(ALICE, INSURER_A, 1000e6, 100);
        uint256 free = _credit(ALICE, INSURER_A, 200e6, 0);

        // Unactivated coverage is neither an issued balance nor a credit limit.
        assertEq(buck.creditLimit(ALICE), 0);
        assertEq(buck.mintsBacked(insured), 0);
        assertEq(buck.mintsPrincipal(insured), 0);
        assertEq(buck.mintsBacked(type(uint256).max), 0);
        assertEq(buck.mintsPrincipal(type(uint256).max), 0);

        vm.startPrank(ALICE);
        buck.issue(insured, 1000e6, 900e6, type(uint256).max, address(pool));
        buck.issue(free, 200e6, 200e6, type(uint256).max, address(0));
        vm.stopPrank();
        assertEq(buck.creditLimit(ALICE), 1200e6);
        assertEq(buck.creditLimit(BOB), 0);
        assertEq(buck.mintsBacked(insured), 1000e6);
        assertEq(buck.mintsBacked(free), 200e6);
        assertEq(buck.mintsPrincipal(insured), 100e6);
        assertEq(buck.mintsPrincipal(free), 0);
        assertEq(buck.balanceOf(ALICE), 1100e6);

        // K changes the limit, not the BUCK already issued to the wallet.
        k.setK(0.75e18);
        assertEq(buck.creditLimit(ALICE), 900e6);
        assertEq(buck.balanceOf(ALICE), 1100e6);

        // A partial settlement releases coverage and the original pool's
        // nominal premium pro rata, while the other facility is untouched.
        _authorize(ALICE, insured, 500e6, 0);
        fund.settle(buck, ALICE, insured, 500e6, 0);
        assertEq(buck.creditLimit(ALICE), 525e6);
        assertEq(buck.mintsBacked(insured), 500e6);
        assertEq(buck.mintsBacked(free), 200e6);
        assertEq(buck.mintsPrincipal(insured), 50e6);
        assertEq(buck.mintsPrincipal(free), 0);
    }

    function test_kCutDoesNotRecallIssuedUnspentAndReductionImprovesShortfall() public {
        uint256 id = _credit(ALICE, INSURER_A, 1000e6, 0);
        vm.prank(ALICE);
        buck.issue(id, 1000e6, 1000e6, type(uint256).max, address(0));
        k.setK(0.5e18);
        assertEq(buck.balanceOf(ALICE), 1000e6);
        vm.prank(ALICE);
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        buck.issue(id, 0, 1, type(uint256).max, address(0));
        _authorize(ALICE, id, 100e6, 0);
        fund.settle(buck, ALICE, id, 100e6, 0);
        (uint256 remaining,) = credit.issuanceOf(id);
        assertEq(remaining, 900e6);
        assertEq(buck.balanceOf(ALICE), 900e6);
    }

    function test_distinctInsurersReceiveAndRefundTheirOwnNominalDeposits() public {
        MockNetInsurerPool a = _pool(INSURER_A);
        MockNetInsurerPool b = _pool(INSURER_B);
        a.approveToken(IERC20(address(buck)));
        b.approveToken(IERC20(address(buck)));
        uint256 aliceId = _credit(ALICE, INSURER_A, 1000e6, 100);
        uint256 bobId = _credit(BOB, INSURER_B, 1000e6, 100);
        vm.prank(ALICE);
        buck.issue(aliceId, 1000e6, 900e6, type(uint256).max, address(a));
        vm.prank(BOB);
        buck.issue(bobId, 1000e6, 900e6, type(uint256).max, address(b));
        assertEq(buck.balanceOf(address(a)), 100e6);
        assertEq(buck.balanceOf(address(b)), 100e6);
        assertEq(buck.facilityPool(aliceId), address(a));
        assertEq(buck.facilityPool(bobId), address(b));
        vm.warp(t0 + YEAR);
        (, uint256 fundNet) = buck.fundBalancesScaled();
        uint256 bobPoolNet = buck.netBalanceOf(address(b));
        _authorize(ALICE, aliceId, 1000e6, 0);
        vm.prank(BOB);
        buck.transfer(ALICE, 20e6);
        fund.settle(buck, ALICE, aliceId, 1000e6, fundNet / 10);
        assertEq(buck.balanceOf(address(a)), 0);
        assertEq(buck.balanceOf(address(b)), 100e6);
        assertEq(buck.netBalanceOf(address(b)), bobPoolNet);
        assertEq(buck.premiumPrincipal(bobId), 100e6);
        assertEq(buck.totalIssued(), 1000e6);
    }

    function test_aRegisteredPoolCannotBeReassignedToAnotherInsurer() public {
        MockNetInsurerPool pool = _pool(INSURER_A);
        assertEq(buck.poolInsurer(address(pool)), INSURER_A);
        vm.prank(address(pool));
        vm.expectRevert(BuckNet.PoolNotControlled.selector);
        buck.setInsurerPool(address(pool));
        assertEq(buck.insurerPool(address(pool)), address(0));
    }

    function test_partialRefundCarriesThePoolAverageFeeIntoTheOwner() public {
        MockNetInsurerPool pool = _pool(INSURER_A);
        pool.approveToken(IERC20(address(buck)));
        uint256 id = _credit(ALICE, INSURER_A, 1000e6, 100);
        vm.prank(ALICE);
        buck.issue(id, 1000e6, 900e6, type(uint256).max, address(pool));
        vm.warp(t0 + YEAR);
        (uint256 ownerHeld, uint256 ownerUnspent) = buck.heldAndUnspentScaled(ALICE);
        (uint256 poolNet,) = buck.heldAndUnspentScaled(address(pool));
        (, uint256 fundNet) = buck.fundBalancesScaled();
        uint256 relief = fundNet / 10;
        uint256 expectedOwner = ownerHeld + ownerUnspent + poolNet / 2 + relief - 500e6 * Q;
        _authorize(ALICE, id, 500e6, 0);
        fund.settle(buck, ALICE, id, 500e6, relief);
        (ownerHeld, ownerUnspent) = buck.heldAndUnspentScaled(ALICE);
        assertApproxEqAbs(ownerHeld + ownerUnspent, expectedOwner, 1);
        assertEq(buck.balanceOf(address(pool)), 50e6);
        assertEq(buck.premiumPrincipal(id), 50e6);
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(principal, 500e6);
    }

    function test_insurerPoolStaysPinnedAndCanRedeemInvestmentsBeforeRefund() public {
        MockNetInsurerPool original = _pool(INSURER_A);
        original.approveToken(IERC20(address(buck)));
        uint256 id = _credit(ALICE, INSURER_A, 2000e6, 100);
        vm.prank(ALICE);
        buck.issue(id, 1000e6, 900e6, type(uint256).max, address(original));
        MockNetInsurerPool replacement = _pool(INSURER_A);
        vm.expectRevert(BuckNet.InsurerPoolChanged.selector);
        vm.prank(ALICE);
        buck.issue(id, 500e6, 450e6, type(uint256).max, address(replacement));
        assertEq(buck.totalIssued(), 1000e6);
        vm.prank(ALICE);
        buck.issue(id, 500e6, 450e6, type(uint256).max, address(original));
        uint256 bobId = _credit(BOB, INSURER_B, 1000e6, 0);
        vm.prank(BOB);
        buck.issue(bobId, 1000e6, 1000e6, type(uint256).max, address(0));
        assertEq(buck.facilityPool(id), address(original));
        assertEq(buck.balanceOf(address(original)), 150e6);
        assertEq(buck.balanceOf(address(replacement)), 0);

        MockNetInsurerPool strategy = new MockNetInsurerPool();
        _bind(address(strategy), true, INSURER_A);
        original.transferToken(IERC20(address(buck)), address(strategy), 150e6);
        vm.warp(t0 + YEAR);
        vm.prank(BOB);
        buck.transfer(ALICE, 30e6);
        (, uint256 fundNet) = buck.fundBalancesScaled();
        _authorize(ALICE, id, 1500e6, 0);
        vm.expectRevert(BuckLedger.InsufficientBalance.selector);
        fund.settle(buck, ALICE, id, 1500e6, fundNet / 10);
        assertEq(buck.totalIssued(), 2500e6);
        strategy.transferToken(IERC20(address(buck)), address(original), 150e6);
        fund.settle(buck, ALICE, id, 1500e6, fundNet / 10);
        assertEq(buck.balanceOf(address(original)), 0);
        assertEq(buck.balanceOf(address(strategy)), 0);
        assertEq(buck.totalIssued(), 1000e6);
    }

    function test_refundWithoutPoolAllowanceRevertsAtomically() public {
        MockNetInsurerPool pool = _pool(INSURER_A);
        uint256 id = _credit(ALICE, INSURER_A, 1000e6, 100);
        vm.prank(ALICE);
        buck.issue(id, 1000e6, 900e6, type(uint256).max, address(pool));
        _authorize(ALICE, id, 1000e6, 0);
        vm.expectRevert(BuckNet.InsufficientAllowance.selector);
        fund.settle(buck, ALICE, id, 1000e6, 0);
        assertEq(buck.balanceOf(address(pool)), 100e6);
        assertEq(buck.balanceOf(ALICE), 900e6);
        assertEq(buck.totalIssued(), 1000e6);
        pool.approveToken(IERC20(address(buck)));
        fund.settle(buck, ALICE, id, 1000e6, 0);
        assertEq(buck.totalIssued(), 0);
    }

    function test_reductionRequiresOwnerAuthorizationAndReliefFloor() public {
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(id, 100e6, 100e6, type(uint256).max, address(0));
        vm.expectRevert(BuckNet.BadAuthorization.selector);
        fund.settle(buck, ALICE, id, 10e6, 0);
        vm.prank(BOB);
        vm.expectRevert(BuckNet.NotOwner.selector);
        buck.authorizeReduction(id, 10e6, 0, uint64(t0 + YEAR));
        _authorize(ALICE, id, 10e6, Q);
        vm.expectRevert(BuckNet.BadAuthorization.selector);
        fund.settle(buck, ALICE, id, 10e6, 0);
        assertEq(buck.totalIssued(), 100e6);
    }

    function test_tokenRejectsACompromisedFundAttemptAboveTenPercentOfNetCash() public {
        uint256 id = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(id, 100e6, 100e6, type(uint256).max, address(0));
        vm.warp(t0 + YEAR);
        (, uint256 cash) = buck.fundBalancesScaled();
        _authorize(ALICE, id, 1e6, 0);
        vm.expectRevert(BuckNet.ReliefLiquidityLimit.selector);
        fund.settle(buck, ALICE, id, 1e6, cash / 10 + 1);
        assertEq(buck.totalIssued(), 100e6);
        (uint256 authorized,,) = buck.reductionAuthorization(id);
        assertEq(authorized, 1e6);
        fund.settle(buck, ALICE, id, 1e6, cash / 10);
        assertEq(buck.totalIssued(), 99e6);
    }

    function test_reductionsShareBlockBudgetAndRetryInNextBlock() public {
        uint256 aliceId = _credit(ALICE, INSURER_A, 100e6, 0);
        uint256 bobId = _credit(BOB, INSURER_B, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(aliceId, 100e6, 100e6, type(uint256).max, address(0));
        vm.prank(BOB);
        buck.issue(bobId, 100e6, 100e6, type(uint256).max, address(0));
        vm.warp(t0 + YEAR);
        (, uint256 cash) = buck.fundBalancesScaled();
        uint256 first = cash / 15;
        uint256 second = cash / 25;
        assertEq(buck.availableReliefScaled(), cash / 10);
        _authorize(ALICE, aliceId, 10e6, 0);
        _authorize(BOB, bobId, 10e6, 0);
        fund.settle(buck, ALICE, aliceId, 10e6, first);
        uint256 remaining = cash / 10 - first;
        assertEq(buck.availableReliefScaled(), remaining);
        assertGt(second, remaining);
        assertLe(second, (cash - first) / 10); // only the shared limit blocks it
        uint256 bobBalance = buck.balanceOf(BOB);
        uint256 supply = buck.totalSupply();
        vm.expectRevert(BuckNet.ReliefLiquidityLimit.selector);
        fund.settle(buck, BOB, bobId, 10e6, second);
        assertEq(buck.balanceOf(BOB), bobBalance);
        assertEq(buck.totalSupply(), supply);
        (uint256 authorization,,) = buck.reductionAuthorization(bobId);
        assertEq(authorization, 10e6);
        assertEq(buck.availableReliefScaled(), remaining);
        vm.roll(block.number + 1);
        assertEq(buck.availableReliefScaled(), (cash - first) / 10);
        fund.settle(buck, BOB, bobId, 10e6, second);
        assertEq(buck.totalSupply(), supply - 10e6);
    }

    function test_zeroReliefDoesNotStartBudgetAndLaterInflowsDoNotReplenishIt() public {
        vm.roll(0); // block zero must not be confused with an uninitialized budget
        uint256 aliceId = _credit(ALICE, INSURER_A, 100e6, 0);
        uint256 bobId = _credit(BOB, INSURER_B, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(aliceId, 100e6, 100e6, type(uint256).max, address(0));
        vm.prank(BOB);
        buck.issue(bobId, 100e6, 100e6, type(uint256).max, address(0));
        _authorize(ALICE, aliceId, 1e6, 0);
        fund.settle(buck, ALICE, aliceId, 1e6, 0);
        vm.prank(BOB);
        buck.transfer(address(fund), 10e6);
        assertEq(buck.availableReliefScaled(), 1e6 * Q);
        _authorize(ALICE, aliceId, 1e6, 0);
        fund.settle(buck, ALICE, aliceId, 1e6, 600_000 * Q);
        assertEq(buck.availableReliefScaled(), 400_000 * Q);
        vm.prank(BOB);
        buck.transfer(address(fund), 10e6);
        assertEq(buck.availableReliefScaled(), 400_000 * Q);
        _authorize(BOB, bobId, 1e6, 0);
        vm.expectRevert(BuckNet.ReliefLiquidityLimit.selector);
        fund.settle(buck, BOB, bobId, 1e6, 400_000 * Q + 1);
        fund.settle(buck, BOB, bobId, 1e6, 400_000 * Q);
        assertEq(buck.availableReliefScaled(), 0);
        vm.roll(1);
        (, uint256 cash) = buck.fundBalancesScaled();
        assertEq(buck.availableReliefScaled(), cash / 10);
    }

    function test_failedSettlementDoesNotConsumeOrStartBlockBudget() public {
        uint256 aliceId = _credit(ALICE, INSURER_A, 100e6, 0);
        _credit(BOB, INSURER_B, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(aliceId, 100e6, 100e6, type(uint256).max, address(0));
        vm.prank(ALICE);
        buck.transfer(BOB, 100e6);
        vm.warp(t0 + YEAR);
        (, uint256 cash) = buck.fundBalancesScaled();
        uint256 available = cash / 10;
        _authorize(ALICE, aliceId, 10e6, 0);
        vm.expectRevert(BuckLedger.InsufficientBalance.selector);
        fund.settle(buck, ALICE, aliceId, 10e6, available);
        assertEq(buck.availableReliefScaled(), available);
        (uint256 authorization,,) = buck.reductionAuthorization(aliceId);
        assertEq(authorization, 10e6);
        // If the failed attempt had snapshotted the budget, this new cash
        // would not increase it within the current block.
        vm.prank(BOB);
        buck.transfer(address(fund), 10e6);
        (, uint256 afterDonation) = buck.fundBalancesScaled();
        assertGt(afterDonation, cash);
        assertEq(buck.availableReliefScaled(), afterDonation / 10);
    }

    function test_expiredMaturityRequiresPermissionlessProcessingBeforeIssuance() public {
        uint256 old = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(old, 100e6, 100e6, type(uint256).max, address(0));
        vm.warp(t0 + 50 * YEAR);
        uint256 next = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        vm.expectRevert(JubileeClaims.PendingMaturities.selector);
        buck.issue(next, 100e6, 100e6, type(uint256).max, address(0));
        (uint256 principal,) = credit.issuanceOf(next);
        assertEq(principal, 0);
        (uint256 processed, bool current) = buck.processMaturities(1);
        assertEq(processed, 1);
        assertTrue(current);
        vm.prank(ALICE);
        buck.issue(next, 100e6, 100e6, type(uint256).max, address(0));
        assertEq(buck.totalIssued(), 200e6);
    }

    function test_insuranceFundingFactorUsesExistingSpendableBalance() public {
        MockNetInsurerPool pool = _pool(INSURER_A);
        uint256 id = _credit(ALICE, INSURER_A, 1000e6, 100);
        k.setFactor(1e18);
        vm.prank(ALICE);
        vm.expectRevert(BuckNet.FundingInsufficient.selector);
        buck.issue(id, 1000e6, 900e6, type(uint256).max, address(0));
        assertEq(buck.totalIssued(), 0);
        assertEq(buck.balanceOf(address(pool)), 0);
    }

    function test_insuranceFundingFactorAcceptsAnExistingReserve() public {
        MockNetInsurerPool pool = _pool(INSURER_A);
        uint256 starter = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(starter, 100e6, 100e6, type(uint256).max, address(0));
        uint256 insured = _credit(ALICE, INSURER_A, 1000e6, 100);
        k.setFactor(1e18);
        vm.prank(ALICE);
        buck.issue(insured, 1000e6, 900e6, type(uint256).max, address(pool));
        assertEq(buck.balanceOf(address(pool)), 100e6);
        assertEq(buck.balanceOf(ALICE), 1000e6);
        assertEq(buck.totalIssued(), 1100e6);
    }

    function testFuzz_idleFacilityCanPartiallyRetireWithinTheLiquidLimit(uint32 amount_, uint8 years_) public {
        uint256 amount = bound(amount_, 20, 1_000_000_000);
        uint256 elapsedYears = bound(years_, 0, 49);
        uint256 id = _credit(ALICE, INSURER_A, amount, 0);
        vm.prank(ALICE);
        buck.issue(id, amount, amount, type(uint256).max, address(0));
        vm.warp(t0 + elapsedYears * YEAR);
        if (buck.maturitiesPending()) buck.processMaturities(1);
        (, uint256 fundNet) = buck.fundBalancesScaled();
        uint256 reduction = amount / 20;
        uint256 relief = fundNet * reduction / amount;
        _authorize(ALICE, id, reduction, 0);
        fund.settle(buck, ALICE, id, reduction, relief);
        assertEq(buck.totalIssued(), amount - reduction);
        (, uint256 afterNet) = buck.fundBalancesScaled();
        assertEq(afterNet, fundNet - relief);
    }
}
