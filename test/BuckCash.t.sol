// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckCash} from "../src/lib/BuckCash.sol";
import {BuckTypes} from "../src/BuckTypes.sol";

/// @dev Monetary-component harness only. Production Buck must provide the
///      missing identity, consent, NFT attribution and token-wide lock.
contract BuckCashHarness {
    using BuckCash for BuckCash.Book;
    BuckCash.Book private _cash;
    constructor(address fund) { _cash.initialize(fund); }
    function issue(address owner, uint256 raw, bool carrying) external { _cash.issue(owner, raw, carrying); }
    function move(address from, address to, uint256 raw, bool fc, bool tc) external returns (uint256) {
        return _cash.move(from, to, raw, fc, tc);
    }
    function settle(address owner) external { _cash.settle(owner); }
    function sponsor(address owner, address payer_) external { _cash.establishSponsor(owner, payer_); }
    function clear(address owner) external { _cash.clearSponsor(owner); }
    function sync(address owner, bool carrying) external { _cash.syncMode(owner, carrying); }
    function balance(address owner) external view returns (uint256) { return _cash.balanceOf(owner); }
    function net(address owner) external view returns (uint256) { return _cash.netOf(owner); }
    function parts(address owner) external view returns (uint256, uint256) { return _cash.grossAndNetOf(owner); }
    function fundParts() external view returns (uint256, uint256) { return _cash.fundParts(); }
    function supply() external view returns (uint256) { return _cash.issued; }
    function payer(address owner) external view returns (address) { return _cash.sponsor[owner]; }
    function dependants(address payer_) external view returns (uint256) { return _cash.dependants[payer_]; }
    function availableRelief() external view returns (uint256) { return _cash.availableRelief(); }
    function protectedRetire(address owner, uint256 raw, uint256 relief, bool carrying)
        external returns (uint256, uint256)
    {
        _cash.consumeRelief(relief);
        return _cash.retire(owner, raw, relief, carrying);
    }
    /// @dev Construct exact internal-unit funding boundaries while conserving
    ///      net supply: move the surplus payer backing to fund cash.
    function seedFundingBoundary(address owner, uint256 value) external {
        _cash.settle(owner);
        _cash.settle(_cash.fund);
        uint256 old = _cash.accounts[owner].net;
        require(value <= old && value <= type(uint200).max, "test boundary");
        _cash.accounts[owner].net = uint200(value);
        _cash.fundNet += old - value;
        _cash.fundGross += old - value;
    }
}

contract BuckCashTest is Test {
    uint256 constant Q = 1e36;
    uint256 constant YEAR = 365 days + 6 hours;
    address constant FUND = address(0xF00D);
    address constant ALICE = address(0xA11CE);
    address constant BOB = address(0xB0B);
    address constant PAYER = address(0xCA401);
    address constant POOL = address(0xC001);
    BuckCashHarness cash;
    uint256 t0;
    event DemurrageSponsor(address indexed account, address indexed sponsor);

    function setUp() public { cash = new BuckCashHarness(FUND); t0 = block.timestamp; }
    function _at(uint256 dt) private { vm.warp(t0 + dt); }
    function _fundSponsored(uint256 payerRaw) private {
        cash.issue(ALICE, 100e6, false);
        cash.issue(PAYER, payerRaw, false);
        cash.sponsor(ALICE, PAYER);
    }
    function _netSum(address a, address b) private view returns (uint256) {
        (, uint256 fund) = cash.fundParts();
        return cash.net(a) + cash.net(b) + fund;
    }

    function test_oneYearCompoundCashAndLinearGrossFund() public {
        cash.issue(ALICE, 100e6, false);
        _at(YEAR);
        assertEq(cash.balance(ALICE), 98_019867);
        (uint256 gross, uint256 net) = cash.fundParts();
        assertEq(gross, 2e6 * Q);
        assertEq(net + cash.net(ALICE), 100e6 * Q);
        assertEq(cash.supply(), 100e6);
    }

    function test_fiftyYearsDoesNotEmptyWalletOrCreateFullValueFundCash() public {
        cash.issue(ALICE, 100e6, false);
        _at(50 * YEAR);
        assertEq(cash.balance(ALICE), 36_787944);
        (uint256 gross, uint256 net) = cash.fundParts();
        assertEq(gross, 100e6 * Q);
        assertEq(net / Q, 63_212055);
        assertEq(net + cash.net(ALICE), 100e6 * Q);
    }

    function test_longIdleHasBoundedArithmeticAndPreservesStock() public {
        cash.issue(ALICE, 100e6, false);
        _at(9001 * YEAR);
        assertEq(cash.net(ALICE), 0);
        (, uint256 net) = cash.fundParts();
        assertEq(net, cash.supply() * Q);
        cash.settle(ALICE);
    }

    function test_carryingBalanceStaysNominalAndTransfersOnlyItsNet() public {
        cash.issue(POOL, 100e6, true);
        _at(10 * YEAR);
        assertEq(cash.balance(POOL), 100e6);
        uint256 beforeNet = cash.net(POOL);
        uint256 received = cash.move(POOL, ALICE, 10e6, true, false);
        assertEq(received, beforeNet / 10);
        assertEq(cash.net(ALICE), received);
        assertEq(cash.balance(ALICE), 8_187307);
        assertEq(cash.balance(POOL), 90e6);
        assertEq(cash.supply(), 100e6);
    }

    function test_carryingToCarryingPreservesNominalAndPooledFee() public {
        cash.issue(POOL, 100e6, true);
        _at(YEAR);
        uint256 net = cash.net(POOL);
        cash.move(POOL, BOB, 25e6, true, true);
        assertEq(cash.balance(BOB), 25e6);
        assertEq(cash.balance(POOL), 75e6);
        assertEq(cash.net(BOB), net / 4);
        assertEq(cash.net(BOB) + cash.net(POOL), net);
    }

    function test_selfTransferKeepsCashAndNominalUnchanged() public {
        cash.issue(POOL, 100e6, true);
        _at(YEAR);
        uint256 beforeNet = cash.net(POOL);
        cash.move(POOL, POOL, 50e6, true, true);
        assertEq(cash.net(POOL), beforeNet);
        assertEq(cash.balance(POOL), 100e6);
    }

    function test_sponsorAlsoFundsInheritedCarryingFeesWithoutCreditingFundTwice() public {
        _fundSponsored(100e6);
        cash.issue(POOL, 100e6, true);
        _at(YEAR);
        uint256 poolComponent = cash.net(POOL) / 10;
        (, uint256 fundBefore) = cash.fundParts();
        uint256 actualPoolPayment = cash.move(POOL, ALICE, 10e6, true, false);
        assertEq(actualPoolPayment, poolComponent);
        assertEq(cash.net(ALICE), 110e6 * Q, "sponsor covers inherited fee");
        assertEq(cash.balance(ALICE), 110e6);
        (, uint256 fundAfter) = cash.fundParts();
        assertEq(fundAfter, fundBefore, "inherited fees already belong to fund");
        assertApproxEqAbs(cash.net(ALICE) + cash.net(PAYER) + cash.net(POOL) + fundAfter,
                          cash.supply() * Q, Q);
    }

    function test_unfundedInheritedFeeRevertsTheEntireReceiptAndEarlierFeeSettlement() public {
        _fundSponsored(2_100000);
        cash.issue(POOL, 100e6, true);
        _at(YEAR);
        uint256 payerBefore = cash.net(PAYER);
        uint256 ownerBefore = cash.net(ALICE);
        uint256 ownFee = 100e6 * Q - ownerBefore;
        uint256 inheritedFee = 10e6 * Q - cash.net(POOL) / 10;
        vm.expectRevert(abi.encodeWithSelector(BuckCash.DemurrageSponsorUnderfunded.selector,
            ALICE, PAYER, inheritedFee, payerBefore - ownFee));
        cash.move(POOL, ALICE, 10e6, true, false);
        assertEq(cash.net(PAYER), payerBefore);
        assertEq(cash.net(ALICE), ownerBefore);
        assertEq(cash.balance(POOL), 100e6);
        assertEq(cash.payer(ALICE), PAYER);
    }

    function test_sponsorEstablishmentEmitsCanonicalPair() public {
        cash.issue(ALICE, 100e6, false);
        cash.issue(PAYER, 100e6, false);
        vm.expectEmit(true, true, false, true, address(cash));
        emit DemurrageSponsor(ALICE, PAYER);
        cash.sponsor(ALICE, PAYER);
        assertEq(cash.dependants(PAYER), 1);
    }

    function test_fullSponsorshipPreservesDisplayAndMovesOnlyFundedCash() public {
        _fundSponsored(100e6);
        _at(YEAR);
        uint256 underlying = cash.net(ALICE);
        (, uint256 fundBefore) = cash.fundParts();
        assertEq(cash.balance(ALICE), 100e6);
        assertLt(underlying, 100e6 * Q);
        cash.settle(ALICE);
        assertEq(cash.net(ALICE), 100e6 * Q);
        assertEq(cash.net(PAYER), 2 * underlying - 100e6 * Q);
        (, uint256 fundAfter) = cash.fundParts();
        assertEq(fundAfter, fundBefore, "fees already credited to fund");
        assertEq(_netSum(ALICE, PAYER), cash.supply() * Q);
        assertEq(cash.payer(ALICE), PAYER);
    }

    function test_underfundedSettlementRevertsBalancesAndLinkage() public {
        _fundSponsored(1e6);
        _at(YEAR);
        uint256 beforeAccount = cash.net(ALICE);
        uint256 beforePayer = cash.net(PAYER);
        uint256 fee = 100e6 * Q - beforeAccount;
        vm.expectRevert(abi.encodeWithSelector(BuckCash.DemurrageSponsorUnderfunded.selector,
            ALICE, PAYER, fee, beforePayer));
        cash.settle(ALICE);
        assertEq(cash.net(PAYER), beforePayer, "no partial contribution");
        assertEq(cash.net(ALICE), beforeAccount, "no cash restoration");
        assertEq(cash.balance(ALICE), 100e6, "protected display remains");
        assertEq(cash.payer(ALICE), PAYER);
        assertEq(cash.dependants(PAYER), 1);
        assertApproxEqAbs(_netSum(ALICE, PAYER), cash.supply() * Q, 2);
    }

    function test_manualDetachmentBypassesUnderfundedSponsorAndChargesWholeNormalFee() public {
        _fundSponsored(1e6);
        _at(YEAR);
        uint256 beforePayer = cash.net(PAYER);
        uint256 beforeAccount = cash.net(ALICE);
        vm.expectEmit(true, true, false, true, address(cash));
        emit DemurrageSponsor(ALICE, address(0));
        cash.clear(ALICE);
        assertEq(cash.net(PAYER), beforePayer);
        assertEq(cash.net(ALICE), beforeAccount);
        assertEq(cash.balance(ALICE), 98_019867);
        assertEq(cash.payer(ALICE), address(0));
        assertEq(cash.dependants(PAYER), 0);
        cash.settle(ALICE);
        assertEq(cash.net(ALICE), beforeAccount, "same-block fee not charged twice");
    }

    function test_exactInternalFundingPaysFullFee() public {
        _fundSponsored(100e6);
        _at(YEAR);
        uint256 fee = 100e6 * Q - cash.net(ALICE);
        cash.seedFundingBoundary(PAYER, fee);
        cash.settle(ALICE);
        assertEq(cash.net(PAYER), 0);
        assertEq(cash.net(ALICE), 100e6 * Q);
        assertEq(cash.payer(ALICE), PAYER);
    }

    function test_oneInternalUnitShortRevertsWithoutTakingAnyPayerCash() public {
        _fundSponsored(100e6);
        _at(YEAR);
        uint256 fee = 100e6 * Q - cash.net(ALICE);
        cash.seedFundingBoundary(PAYER, fee - 1);
        vm.expectRevert(abi.encodeWithSelector(BuckCash.DemurrageSponsorUnderfunded.selector,
            ALICE, PAYER, fee, fee - 1));
        cash.settle(ALICE);
        assertEq(cash.net(PAYER), fee - 1);
        assertEq(cash.payer(ALICE), PAYER);
        assertEq(cash.net(ALICE), 100e6 * Q - fee);
    }

    function test_zeroFeeDoesNotDetachEmptySponsor() public {
        _fundSponsored(1);
        cash.move(PAYER, BOB, 1, false, false);
        cash.settle(ALICE);
        assertEq(cash.payer(ALICE), PAYER);
        assertEq(cash.net(PAYER), 0);
    }

    function test_multipleDependantsReceiveOnlyWholePaymentsInSettlementOrder() public {
        _fundSponsored(3e6);
        cash.issue(BOB, 100e6, false);
        cash.sponsor(BOB, PAYER);
        _at(YEAR);
        cash.settle(ALICE);
        uint256 remainder = cash.net(PAYER);
        assertEq(cash.payer(ALICE), PAYER);
        vm.expectRevert(abi.encodeWithSelector(BuckCash.DemurrageSponsorUnderfunded.selector,
            BOB, PAYER, 100e6 * Q - cash.net(BOB), remainder));
        cash.settle(BOB);
        assertEq(cash.net(PAYER), remainder);
        assertEq(cash.payer(BOB), PAYER);
        assertEq(cash.balance(ALICE), 100e6);
        assertEq(cash.balance(BOB), 100e6);
        assertEq(cash.dependants(PAYER), 2);
    }

    function test_underfundedSpendRevertsUntilSponsorFundedOrManuallyDetached() public {
        _fundSponsored(1e6);
        _at(YEAR);
        uint256 fee = 100e6 * Q - cash.net(ALICE);
        vm.expectRevert(abi.encodeWithSelector(BuckCash.DemurrageSponsorUnderfunded.selector,
            ALICE, PAYER, fee, cash.net(PAYER)));
        cash.move(ALICE, BOB, 99e6, false, false);
        assertEq(cash.payer(ALICE), PAYER);
        assertEq(cash.balance(ALICE), 100e6);
        cash.clear(ALICE);
        cash.move(ALICE, BOB, 98e6, false, false);
        assertEq(cash.balance(BOB), 98e6);
        assertEq(cash.payer(ALICE), address(0));
    }

    function test_fundingSponsorRestoresSpendWithoutChangingLink() public {
        _fundSponsored(1e6);
        cash.issue(BOB, 10e6, false);
        _at(YEAR);
        cash.move(BOB, PAYER, 2e6, false, false);
        cash.move(ALICE, BOB, 99e6, false, false);
        assertEq(cash.balance(ALICE), 1e6);
        assertEq(cash.payer(ALICE), PAYER);
    }

    function test_recipientFeeSettlementCannotLeaveSenderSpendCheckStale() public {
        _fundSponsored(3e6);
        _at(YEAR);
        vm.expectRevert(BuckCash.InsufficientSpendableBalance.selector);
        cash.move(PAYER, ALICE, 2e6, false, false);
        assertEq(cash.balance(ALICE), 100e6);
        assertEq(cash.payer(ALICE), PAYER);
    }

    function test_manualDetachmentSettlesOnceThenOrdinaryCompoundingResumes() public {
        _fundSponsored(100e6);
        _at(YEAR);
        cash.clear(ALICE);
        assertEq(cash.payer(ALICE), address(0));
        assertEq(cash.dependants(PAYER), 0);
        assertEq(cash.balance(ALICE), 98_019867);
        _at(2 * YEAR);
        assertEq(cash.balance(ALICE), 96_078943);
    }

    function test_manualDetachmentNeverChargesAPreviouslySponsoredPeriodAgain() public {
        _fundSponsored(100e6);
        _at(YEAR);
        cash.settle(ALICE);
        _at(2 * YEAR);
        uint256 payerBefore = cash.net(PAYER);
        cash.clear(ALICE);
        assertEq(cash.balance(ALICE), 98_019867, "only the unsettled year is self-paid");
        assertEq(cash.net(PAYER), payerBefore, "manual detach does not charge sponsor");
    }

    function test_sponsorshipRejectsSelfChainsAndCarryingChanges() public {
        _fundSponsored(100e6);
        vm.expectRevert(BuckCash.SelfSponsorship.selector);
        cash.sponsor(BOB, BOB);
        vm.expectRevert(BuckCash.SponsorIsSponsored.selector);
        cash.sponsor(BOB, ALICE);
        vm.expectRevert(BuckCash.AccountIsSponsor.selector);
        cash.sponsor(PAYER, BOB);
        vm.expectRevert(BuckCash.SponsoredAccountCarrying.selector);
        cash.sync(ALICE, true);
        vm.expectRevert(BuckCash.SponsorCarrying.selector);
        cash.sync(PAYER, true);
    }

    function test_ownerAndFundJointlyRetireExactPrincipal() public {
        cash.issue(ALICE, 100e6, false);
        _at(YEAR);
        uint256 before = cash.net(ALICE);
        (, uint256 fundBefore) = cash.fundParts();
        cash.protectedRetire(ALICE, 10e6, 100_000 * Q, false);
        assertEq(cash.supply(), 90e6);
        assertEq(cash.net(ALICE), before - 9_900000 * Q);
        (, uint256 fundAfter) = cash.fundParts();
        assertEq(fundAfter, fundBefore - 100_000 * Q);
        assertEq(cash.net(ALICE) + fundAfter, cash.supply() * Q);
    }

    function test_carryingRetirementConsumesNominalForFullValuePrincipal() public {
        cash.issue(POOL, 100e6, true);
        _at(YEAR);
        uint256 before = cash.net(POOL);
        (uint256 nominal,) = cash.protectedRetire(POOL, 10e6, 0, true);
        assertGt(nominal, 10e6 * Q);
        assertEq(cash.net(POOL), before - 10e6 * Q);
        assertEq(cash.supply(), 90e6);
    }

    function test_sharedBlockBudgetDoesNotRefillWithNewCash() public {
        cash.issue(ALICE, 100e6, false);
        _at(YEAR);
        uint256 initial = cash.availableRelief();
        cash.protectedRetire(ALICE, 10e6, 100_000 * Q, false);
        cash.move(ALICE, FUND, 20e6, false, true);
        assertEq(cash.availableRelief(), initial - 100_000 * Q);
        vm.roll(block.number + 1);
        (, uint256 net) = cash.fundParts();
        assertEq(cash.availableRelief(), net / 10);
    }

    function test_accountCapAndTimestampCannotBeSilentlyNarrowed() public {
        vm.expectRevert(BuckCash.AccountOverflow.selector);
        cash.issue(ALICE, BuckTypes.MAX_BALANCE + 1, false);
        cash.issue(ALICE, 1e6, false);
        vm.warp(uint256(type(uint40).max) + 1);
        vm.expectRevert(BuckCash.TimestampOverflow.selector);
        cash.settle(ALICE);
    }

    /// @dev Net backing, rather than nominal Carrying or protected sponsor
    ///      displays, must conserve issued stock through arbitrary actions.
    ///      This checks an observed one-raw-unit rounding bound, not a proof
    ///      of a global error bound for arbitrary account counts/histories.
    function testFuzz_netBackingConservedAcrossTransfersSponsorshipAndTime(uint256 seed) public {
        cash.issue(ALICE, 500e6, false);
        cash.issue(PAYER, 50e6, false);
        cash.issue(BOB, 500e6, false);
        cash.issue(POOL, 500e6, true);
        cash.sponsor(ALICE, PAYER);
        address[4] memory actors = [ALICE, PAYER, BOB, POOL];
        uint256 elapsed;
        for (uint256 i; i < 32; ++i) {
            seed = uint256(keccak256(abi.encode(seed, i)));
            elapsed += seed % 30 days;
            _at(elapsed);
            address from = actors[(seed >> 32) % 4];
            address to = actors[(seed >> 40) % 4];
            uint256 raw = cash.balance(from) / 10;
            // A currently unfunded sponsored target can legitimately fail.
            try cash.move(from, to, raw, from == POOL, to == POOL) {} catch {}
            if ((seed >> 48) % 8 == 0 && cash.payer(ALICE) != address(0)) cash.clear(ALICE);
            if ((seed >> 52) % 8 == 0 && cash.payer(ALICE) == address(0)) cash.sponsor(ALICE, PAYER);
            (, uint256 fund) = cash.fundParts();
            uint256 total = fund;
            for (uint256 j; j < actors.length; ++j) total += cash.net(actors[j]);
            assertApproxEqAbs(total, cash.supply() * Q, Q, "net stock conserved");
        }
    }
}
