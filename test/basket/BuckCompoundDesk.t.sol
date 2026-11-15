// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {JubileeEquityFixture} from "./JubileeEquityIntegration.t.sol";
import {BasketDesk} from "../../src/basket/BasketDesk.sol";
import {MonetaryDesk} from "../../src/basket/MonetaryDesk.sol";
import {BasketVenueFacet} from "../../src/basket/BasketVenueFacet.sol";
import {BuckBasketUniswapV3} from "../../src/basket/BuckBasketUniswapV3.sol";
import {BuckBasketStorage} from "../../src/basket/BuckBasketStorage.sol";
import {JubileeFund, IJubileeValuationWheel} from "../../src/JubileeFund.sol";
import {Buck} from "../../src/Buck.sol";
import {IBuckBasketVenue} from "../../src/basket/IBuckBasketVenue.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {EqMonDir} from "./BasketDesk.t.sol";
import {EqToken, EqController, IPool} from "./BuckBasketEquity.t.sol";

contract PartialCompoundDeskVenue is BuckBasketUniswapV3 {
    function monetaryLeg(uint256 i, bool sellBuck, uint256 amountIn)
        public override onlySelf returns (uint256 spent, uint256 received)
    {
        return super.monetaryLeg(i, sellBuck, sellBuck ? amountIn / 2 : amountIn);
    }
}

contract CutCompoundDeskKVenue is BuckBasketUniswapV3 {
    function monetaryLeg(uint256 i, bool sellBuck, uint256 amountIn)
        public override onlySelf returns (uint256 spent, uint256 received)
    {
        (spent, received) = super.monetaryLeg(i, sellBuck, amountIn);
        EqController(address(controller)).setK(0);
    }
}

contract RejectCompoundDeskExecution {
    function executionAllowed(int32) external pure returns (bool) { return false; }
}

/// @dev Registered on the real fund/basket; publishes their actual mark,
///      never a fabricated value. A zero key models an unavailable publication.
contract ProbeCompoundDeskValuation is IJubileeValuationWheel {
    address public immutable valuationSource;
    BasketDesk public immutable desk;
    uint256 private mark;
    bytes32 private key;
    bool public armed;
    uint256 public rejected;
    bool public completeRead;

    constructor(JubileeFund fund, BasketDesk desk_) {
        valuationSource = address(fund);
        desk = desk_;
    }

    function arm() external { armed = true; }

    function refreshValuation() external returns (uint256, bytes32) {
        if (armed) {
            _mustReject(abi.encodeCall(desk.refresh, ()));
            _mustReject(abi.encodeCall(desk.monetaryOperation, ()));
            _mustReject(abi.encodeCall(desk.retireCredit, (1, 1, 0)));
        }
        (mark, key) = JubileeFund(valuationSource).wheelValuation();
        if (armed) {
            (uint256 cash, uint256 principal,,,) = desk.accountState();
            completeRead = cash != 0 && principal != 0;
        }
        return (mark, key);
    }

    function _mustReject(bytes memory data) private {
        (bool ok, bytes memory reason) = address(desk).call(data);
        require(!ok && reason.length >= 4 && bytes4(reason) == BasketDesk.DeskReentrant.selector,
            "nested desk entry not rejected by financial guard");
        ++rejected;
    }

    function publishedValuation() external view returns (address, uint256, bytes32, uint256, uint256, bool) {
        return (valuationSource, mark, key, block.number, block.timestamp, key != bytes32(0));
    }
}

/// @dev Real canonical Buck, NFT, fund and V3 trades. The unleveraged basket
///      supplies only the programme's size basis, never the desk's capital.
abstract contract BuckCompoundDeskFixture is JubileeEquityFixture {
    BasketDesk desk;
    EqMonDir policy;
    EqToken asset;
    address pool;

    function setUp() public virtual override {
        super.setUp();
        _bind(address(this), false, address(this));
        _credit(ALICE, 2_000_000e6);
        vm.prank(ALICE); buck.transfer(address(this), 1_000_000e6);
        asset = new EqToken("Asset", "AST", 18);
        asset.mint(address(this), 2_000_000e18);
        vm.prank(GOV);
        pool = basket.addBasketToken(address(asset), 18, 1e6, 10_000, 3000);
        _bind(pool, true, address(this));
        (,,,,,, int24 lower, int24 upper,,,) = basket.constituents(0);
        IPool(pool).mint(address(this), lower, upper, 1e17, "");
        vm.warp(t0 + 601);
        k.setK(0.75e18);
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        basket.deposit(address(buck), 10_000e6, 0);
        vm.stopPrank();
        policy = new EqMonDir();
        _newDesk(1e12);
    }

    function _newDesk(uint256 face) internal {
        desk = new BasketDesk(address(buck), address(k), address(basket), GOV);
        _bind(address(desk), false, address(this));
        vm.startPrank(GOV);
        desk.enableNetAccounting();
        desk.setVenue(address(new BasketVenueFacet()));
        desk.mirrorConstituents();
        desk.openCredit(address(credit), face);
        desk.setMonetaryDirector(address(policy));
        desk.setOpsParams(MonetaryDesk.OpsParams(40, 1000, 1000, true));
        desk.setRiskBudget(1_000_000e6, 0, 500);
        (bool ok,) = address(desk).call(abi.encodeWithSignature("setWheel(address)", address(this)));
        require(ok);
        vm.stopPrank();
    }

    function _grant(uint256 amount) internal {
        asset.mint(GOV, amount);
        vm.startPrank(GOV);
        asset.approve(address(desk), amount);
        desk.capitalizeMonetary(0, amount);
        vm.stopPrank();
    }

    function _issue() internal returns (uint256 principal) {
        _grant(1000e18);
        policy.set(50, true);
        assertEq(desk.monetaryOperation(), 4);
        (principal,) = credit.issuanceOf(desk.creditId());
        assertGt(principal, 0);
    }

    function _assertAccount() internal view {
        (uint256 cash, uint256 principal, uint256 relief, uint256 bare, uint256 marked) = desk.accountState();
        assertEq(cash, buck.netBalanceOf(address(desk)));
        (uint256 actual,) = credit.issuanceOf(desk.creditId());
        assertEq(principal, actual);
        assertEq(desk.monetaryBuckHeld(), cash);
        assertEq(desk.monetaryOutstanding(), int256(principal));
        assertEq(desk.netInventory(), int256(cash) - int256(principal));
        assertEq(desk.collateralValue(), bare);
        assertEq(desk.netValue(), marked);
        assertLe(marked, bare + relief);
        assertEq(desk.markNow(), bare);
        assertLe(desk.monetaryTokenHeld(0), asset.balanceOf(address(desk)));
        assertEq(buck.totalIssued(), 2_000_000e6 + principal);
    }
}

contract BuckCompoundDeskTest is BuckCompoundDeskFixture {
    function test_modeAndPublicOrdinaryIdentityAreRequiredBeforeOpening() public {
        assertTrue(desk.netAccounting());
        vm.expectRevert(BuckBasketStorage.AlreadyPresent.selector);
        vm.prank(GOV); desk.enableNetAccounting();
        BasketDesk unbound = new BasketDesk(address(buck), address(k), address(basket), GOV);
        vm.expectRevert(BasketDesk.NetAccountingRequired.selector);
        vm.prank(GOV); unbound.openCredit(address(credit), 100e6);
        vm.expectRevert(BasketDesk.InvalidNetAccounting.selector);
        vm.prank(GOV); unbound.enableNetAccounting();
        _bind(address(unbound), true, address(this));
        vm.expectRevert(BasketDesk.InvalidNetAccounting.selector);
        vm.prank(GOV); unbound.enableNetAccounting();
    }

    function test_refreshAndGrantDoNotIssueOrActivateOrAgeCredit() public {
        uint256 supply = buck.totalIssued();
        _grant(1000e18);
        desk.refresh();
        (uint256 principal, uint256 age) = credit.issuanceOf(desk.creditId());
        (, uint256 active,) = credit.creditInfo(desk.creditId());
        assertEq(principal, 0);
        assertEq(age, 0);
        assertEq(active, 0);
        assertEq(buck.totalIssued(), supply);
        assertEq(buck.balanceOf(address(desk)), 0);
        assertGt(desk.markNow(), 999e6);
        _assertAccount();
    }

    function test_issuesOnlyTheActualSaleOnItsOwnNftAndDoesNotTouchDepositorBooks() public {
        uint256 cash = buck.netBalanceScaled(address(basket));
        uint256 shares = basket.totalShares();
        uint256 mark = basket.markNow();
        uint256 principal = _issue();
        assertEq(principal, 50e6);
        assertEq(buck.netBalanceOf(address(desk)), 0);
        assertGt(desk.monetaryTokenHeld(0), 1000e18);
        assertLe(principal, credit.currentValue(desk.creditId()) * k.currentBuckK() / 1e18);
        assertEq(buck.netBalanceScaled(address(basket)), cash);
        assertEq(basket.totalShares(), shares);
        assertEq(basket.markNow(), mark);
        assertEq(basket.lien(), 0);
        _assertAccount();
    }

    function test_existingCashAndDebtCoexistAndCashIsSoldBeforeAnotherDraw() public {
        uint256 principal = _issue();
        vm.prank(ALICE); buck.transfer(address(desk), 10e6);
        assertEq(desk.netInventory(), int256(10e6) - int256(principal));
        desk.refresh();
        assertEq(desk.monetaryBuckHeld(), 10e6);
        assertEq(desk.monetaryOutstanding(), int256(principal));
        uint256 supply = buck.totalIssued();
        policy.set(50, true);
        assertEq(desk.monetaryOperation(), 3);
        assertEq(buck.netBalanceOf(address(desk)), 0);
        assertEq(buck.totalIssued(), supply);
        _assertAccount();
    }

    function test_buysNetBuckWithoutInventingDebtWhenThereIsNoLiability() public {
        _grant(1000e18);
        uint256 supply = buck.totalIssued();
        policy.set(-50, true);
        assertEq(desk.monetaryOperation(), 1);
        assertGt(buck.netBalanceScaled(address(desk)), 0);
        assertEq(desk.monetaryOutstanding(), 0);
        assertEq(buck.totalIssued(), supply);
        _assertAccount();
    }

    function test_absorbAgainstDebtActuallyReducesNftPrincipalAndSupply() public {
        uint256 principal = _issue();
        uint256 supply = buck.totalIssued();
        policy.set(-10, false);
        assertEq(desk.monetaryOperation(), 2);
        assertLt(uint256(desk.monetaryOutstanding()), principal);
        assertEq(buck.totalIssued(), supply - (principal - uint256(desk.monetaryOutstanding())));
        assertLt(buck.netBalanceOf(address(desk)), 1); // At zero claim age only fractional dust remains.
        _assertAccount();
    }

    function test_agedClaimIsMarkedButRefreshNeverRealizesItOrLendsAgainstIt() public {
        uint256 principal = _issue();
        vm.warp(block.timestamp + YEAR);
        uint256 supply = buck.totalIssued();
        uint256 seconds_ = buck.claimSeconds();
        desk.refresh();
        (uint256 cash, uint256 p, uint256 relief, uint256 bare, uint256 marked) = desk.accountState();
        assertEq(p, principal);
        assertEq(cash, 0);
        assertGt(relief, 0);
        assertEq(marked, bare + relief);
        assertEq(desk.markNow(), bare);
        assertEq(buck.totalIssued(), supply);
        assertEq(buck.claimSeconds(), seconds_);
        _assertAccount();
    }

    function test_capacityAndInventoryObserveCashBetweenRefreshesAndAfterAging() public {
        _grant(1000e18);
        desk.refresh();
        vm.prank(ALICE); buck.transfer(address(desk), 10e6);
        vm.warp(block.timestamp + YEAR);
        uint256 cash = buck.netBalanceOf(address(desk));
        uint256 cap = basket.equity() / 10;
        assertEq(desk.monetaryBuckHeld(), 0); // Stored diagnostic has not refreshed.
        assertEq(desk.netInventory(), int256(cash));
        assertEq(desk.capacity(), Math.mulDiv(cap - cash, 1e18, cap));
        assertEq(desk.monetaryDeviationOffset(), int256(cash) - int256(desk.monetaryTokenValue()));
    }

    function test_agedPurchasePaysActualReliefAndLeavesItAsRecordedCash() public {
        uint256 principal = _issue();
        vm.warp(block.timestamp + YEAR);
        policy.set(-10, true);
        assertEq(desk.monetaryOperation(), 2);
        assertLt(uint256(desk.monetaryOutstanding()), principal);
        assertGt(desk.monetaryBuckHeld(), 0);
        _assertAccount();
    }

    function test_manualRetirementUsesExactCashAndScaledReliefBounds() public {
        uint256 principal = _issue();
        vm.warp(block.timestamp + YEAR);
        vm.prank(ALICE); buck.transfer(address(desk), 10e6);
        (uint256 quote,, bool limited) = fund.previewRelief(desk.creditId(), 5e6);
        assertFalse(limited);
        uint256 supply = buck.totalIssued();
        vm.expectRevert(BasketDesk.CreditCashLimit.selector);
        desk.retireCredit(5e6, 0, quote);
        assertEq(buck.totalIssued(), supply);
        vm.expectRevert(Buck.BadAuthorization.selector);
        desk.retireCredit(5e6, 5e6, quote + 1);
        assertEq(buck.totalIssued(), supply);
        (uint256 relief, uint256 spent) = desk.retireCredit(5e6, 5e6, quote);
        assertEq(relief, quote);
        assertEq(spent, 5e6 * Q - relief);
        assertEq(desk.monetaryOutstanding(), int256(principal - 5e6));
        _assertAccount();
    }

    function test_retirementAuthorizationCannotBeBypassedOrUseZeroAsACloseSentinel() public {
        _issue();
        vm.expectRevert(BuckBasketStorage.NotWheel.selector);
        vm.prank(BOB); desk.retireCredit(1e6, 1e6, 0);
        vm.expectRevert(BuckBasketStorage.Amount0.selector);
        desk.retireCredit(0, 0, 0);
    }

    function test_faceAndDebtCapsClipIssuanceWithoutMintingUnusedHeadroom() public {
        _newDesk(60e6);
        _grant(1000e18);
        policy.set(50, true);
        assertEq(desk.monetaryOperation(), 4);
        assertEq(desk.monetaryOutstanding(), int256(45e6));
        assertEq(buck.balanceOf(address(desk)), 0);
        _assertAccount();
    }

    function test_debtProgrammeLimitClipsARealDraw() public {
        _grant(1000e18);
        vm.prank(GOV);
        desk.setOpsParams(MonetaryDesk.OpsParams(40, 1000, 1, true));
        policy.set(50, true);
        assertEq(desk.monetaryOperation(), 4);
        assertEq(desk.monetaryOutstanding(), int256(1e6));
        _assertAccount();
    }

    function testFuzz_drawMatchesFaceKAndActualLegBudget(uint64 faceRaw, uint64 kRaw, uint16 effortRaw) public {
        uint256 face = bound(faceRaw, 1e6, 900e6);
        uint256 nextK = bound(kRaw, 0.01e18, 1e18);
        uint256 effort = bound(effortRaw, 1, 1000);
        _newDesk(face);
        _grant(1000e18);
        k.setK(nextK);
        uint256 depth = IBuckBasketVenue(address(desk)).marks(0, 0).depth;   // net: the venue nets it
        uint256 expected = Math.min(Math.mulDiv(face, nextK, 1e18),
            Math.min(Math.mulDiv(depth, 40, 10_000), Math.mulDiv(basket.equity(), effort, 10_000)));
        policy.set(int32(uint32(effort)), true);
        assertEq(desk.monetaryOperation(), 4);
        assertEq(desk.monetaryOutstanding(), int256(expected));
        assertEq(buck.netBalanceOf(address(desk)), 0);
        _assertAccount();
    }

    function test_zeroKAndNoDeskCapitalCannotUseDepositorRoom() public {
        policy.set(50, true);
        vm.expectRevert(BuckBasketStorage.MonetaryBound.selector);
        desk.monetaryOperation();
        _grant(1000e18);
        k.setK(0);
        vm.expectRevert(BuckBasketStorage.MonetaryBound.selector);
        desk.monetaryOperation();
        assertEq(desk.monetaryOutstanding(), 0);
        assertEq(buck.totalIssued(), 2_000_000e6);
    }

    function test_inventoryCapUsesActualNetCash() public {
        _grant(1000e18);
        vm.prank(GOV);
        desk.setOpsParams(MonetaryDesk.OpsParams(40, 1, 1000, true));
        policy.set(-50, false);
        desk.monetaryOperation();
        assertGt(desk.monetaryBuckHeld(), 0);
        assertLe(desk.monetaryBuckHeld(), basket.equity() / 10_000);
        _assertAccount();
    }

    function test_capitalFloorAndExecutionGuardRollbackCurrencyAndRealTrades() public {
        _grant(1000e18);
        uint256 floor = desk.collateralValue();
        vm.prank(GOV); desk.setRiskBudget(1_000_000e6, floor, 500);
        policy.set(50, true);
        vm.expectRevert(BuckBasketStorage.MonetaryBound.selector);
        desk.monetaryOperation();
        assertEq(buck.totalIssued(), 2_000_000e6);
        assertEq(desk.monetaryTokenHeld(0), 1000e18);
        assertEq(desk.lastMonetaryEpoch(), 0);
        vm.startPrank(GOV);
        desk.setRiskBudget(1_000_000e6, 0, 500);
        desk.setExecutionGuard(address(new RejectCompoundDeskExecution()));
        vm.stopPrank();
        vm.expectRevert(BuckBasketStorage.MonetaryBound.selector);
        desk.monetaryOperation();
        assertEq(buck.totalIssued(), 2_000_000e6);
        assertEq(desk.monetaryTokenHeld(0), 1000e18);
    }

    function test_partialExecutionAndKShockRollbackTheDrawAndCoverage() public {
        _grant(1000e18);
        policy.set(50, true);
        address partialVenue = address(new PartialCompoundDeskVenue());
        vm.prank(GOV); desk.setVenue(partialVenue);
        vm.expectRevert(BasketDesk.InconsistentExecution.selector);
        desk.monetaryOperation();
        assertEq(buck.totalIssued(), 2_000_000e6);
        (, uint256 active,) = credit.creditInfo(desk.creditId());
        assertEq(active, 0);
        address cutK = address(new CutCompoundDeskKVenue());
        vm.prank(GOV); desk.setVenue(cutK);
        vm.expectRevert(BasketDesk.CreditShortfallIncreased.selector);
        desk.monetaryOperation();
        assertEq(k.currentBuckK(), 0.75e18);
        assertEq(buck.totalIssued(), 2_000_000e6);
        assertEq(asset.balanceOf(address(desk)), 1000e18);
    }

    function test_turnoverAndEpochLimitsCannotBeResetByOppositeSideTrades() public {
        _grant(1000e18);
        vm.prank(GOV); desk.setRiskBudget(100e6, 0, 500);
        policy.set(-50, false);
        desk.monetaryOperation();
        uint256 remaining = desk.turnoverRemaining();
        assertLt(remaining, 100e6);
        vm.expectRevert(BuckBasketStorage.StepAlreadyDone.selector);
        desk.monetaryOperation();
        policy.set(50, false);
        assertEq(desk.monetaryOperation(), 3);
        assertLt(desk.turnoverRemaining(), remaining);
        _assertAccount();
    }

    function test_agedPoolQuotesAndBoughtCashAreNetOfCarriedFees() public {
        _grant(1000e18);
        vm.warp(block.timestamp + YEAR);
        policy.set(-50, false);
        uint256 poolGross = buck.balanceOf(pool);
        uint256 beforeNet = buck.netBalanceScaled(address(desk));
        desk.monetaryOperation();
        uint256 nominalReceived = poolGross - buck.balanceOf(pool);
        uint256 netReceived = buck.netBalanceScaled(address(desk)) - beforeNet;
        assertGt(nominalReceived * Q, netReceived);
        assertEq(desk.monetaryBuckHeld(), netReceived / Q);
        assertApproxEqRel(netReceived, nominalReceived * Q * 980_198 / 1_000_000, 1e-5 ether);
        _assertAccount();
    }

    function test_fractionalOnlyPurchaseCannotCountAsSuccessfulMonetaryWork() public {
        _grant(1000e18);
        vm.warp(block.timestamp + YEAR);
        vm.prank(GOV); desk.setRiskBudget(1, 0, 500);
        policy.set(-50, false);
        vm.expectRevert(BuckBasketStorage.MonetaryIdle.selector);
        desk.monetaryOperation();
        assertEq(desk.turnoverRemaining(), 1);
        assertEq(desk.lastMonetaryEpoch(), 0);
        assertEq(desk.monetaryTokenHeld(0), 1000e18);
    }

    function test_fullyAgedNftCannotIssueAgain() public {
        _issue();
        vm.warp(block.timestamp + 50 * YEAR);
        buck.processMaturities(10);
        uint256 supply = buck.totalIssued();
        policy.set(50, true);
        vm.expectRevert(BuckBasketStorage.MonetaryBound.selector);
        desk.monetaryOperation();
        assertEq(buck.totalIssued(), supply);
    }

    function _investAllButOneBuck() private returns (uint256 id) {
        (, uint256 cash) = buck.fundBalancesScaled();
        assertGt(cash, 10e6 * Q);
        (id,,) = _invest(cash / Q - 1e6);
        (, cash) = buck.fundBalancesScaled();
        assertLe(cash, 1e6 * Q);
    }

    function test_liquidityRefusalRollsBackThePurchaseAndNeverReportsDebtRetirement() public {
        uint256 principal = _issue();
        vm.warp(block.timestamp + YEAR);
        uint256 receipt = _investAllButOneBuck();
        (uint128 shares,) = basket.holdings(receipt);
        uint256 held = desk.monetaryTokenHeld(0);
        uint256 budget = desk.turnoverRemaining();
        uint256 supply = buck.totalIssued();
        uint256 epoch = desk.lastMonetaryEpoch();
        uint256 poolBuck = buck.balanceOf(pool);
        policy.set(-10, true);
        vm.expectRevert(JubileeFund.ReliefLiquidityLimit.selector);
        desk.monetaryOperation();
        assertEq(desk.monetaryTokenHeld(0), held);
        assertEq(desk.monetaryOutstanding(), int256(principal));
        assertEq(desk.turnoverRemaining(), budget);
        assertEq(desk.lastMonetaryEpoch(), epoch);
        assertEq(buck.totalIssued(), supply);
        assertEq(buck.balanceOf(pool), poolBuck);
        (uint128 afterShares,) = basket.holdings(receipt);
        assertEq(afterShares, shares);
        (uint256 authorized,,) = buck.reductionAuthorization(desk.creditId());
        assertEq(authorized, 0);
    }

    function test_reductionsShareTheBlockBudgetWithoutSellingTheFundReceipt() public {
        _issue();
        vm.warp(block.timestamp + YEAR);
        uint256 receipt = _investAllButOneBuck();
        (uint128 shares,) = basket.holdings(receipt);
        vm.prank(ALICE); buck.transfer(address(desk), 10e6);
        uint256 budget = desk.turnoverRemaining();
        desk.retireCredit(5e6, 5e6, 0);
        (uint256 next, uint256 cash, bool limited) = fund.previewRelief(desk.creditId(), 1e6);
        assertLe(next, cash / 10); // Current-cash cap alone would permit it.
        assertTrue(limited);
        vm.expectRevert(Buck.ReliefLiquidityLimit.selector);
        desk.retireCredit(1e6, 1e6, 0);
        vm.roll(block.number + 1);
        desk.retireCredit(1e6, 1e6, 0);
        assertEq(desk.turnoverRemaining(), budget);
        assertEq(desk.monetaryOutstanding(), int256(44e6));
        (uint128 afterShares,) = basket.holdings(receipt);
        assertEq(afterShares, shares);
        _assertAccount();
    }

    function test_allDepositorsCanLeaveWithoutSpendingOrRetiringTheDesksBook() public {
        _issue();
        policy.set(-10, false);
        desk.monetaryOperation();
        uint256 held = desk.monetaryTokenHeld(0);
        uint256 cash = buck.netBalanceScaled(address(desk));
        int256 principal = desk.monetaryOutstanding();
        uint256 mark = desk.markNow();
        vm.prank(ALICE); basket.redeem(1, 10_000);
        assertEq(desk.monetaryTokenHeld(0), held);
        assertEq(buck.netBalanceScaled(address(desk)), cash);
        assertEq(desk.monetaryOutstanding(), principal);
        assertEq(desk.markNow(), mark);
        _assertAccount();
    }

    function test_badSpotDoesNotSubstituteForTwapOrSpendCredit() public {
        _grant(1000e18);
        (,,,,,,,, bool buckIs0,,) = basket.constituents(0);
        IPool(pool).swap(address(this), buckIs0, int256(100_000e6),
            buckIs0 ? MIN_SQRT + 1 : MAX_SQRT - 1, "");
        uint256 poolBuck = buck.balanceOf(pool);
        policy.set(50, true);
        vm.expectRevert(BuckBasketStorage.Slippage.selector);
        desk.monetaryOperation();
        assertEq(desk.monetaryTokenHeld(0), 1000e18);
        assertEq(buck.balanceOf(pool), poolBuck);
        assertEq(buck.totalIssued(), 2_000_000e6);
    }

    function _valuation(bool prime) private returns (ProbeCompoundDeskValuation probe) {
        probe = new ProbeCompoundDeskValuation(fund, desk);
        vm.startPrank(GOV);
        (bool ok,) = address(basket).call(abi.encodeWithSignature("setWheel(address)", address(probe)));
        require(ok);
        fund.setValuationWheel(probe);
        vm.stopPrank();
        if (prime) probe.refreshValuation();
    }

    function test_unavailableReliefMarkCannotInflateOrBlockBareCreditCollateral() public {
        uint256 principal = _issue();
        vm.warp(block.timestamp + YEAR);
        _invest(100e6);
        _valuation(false);
        vm.expectRevert(JubileeFund.MarkUnavailable.selector);
        desk.netValue();
        desk.refresh();
        assertEq(desk.markNow(), desk.collateralValue());
        policy.set(50, true);
        assertEq(desk.monetaryOperation(), 4);
        assertGt(uint256(desk.monetaryOutstanding()), principal);
        assertEq(buck.totalIssued(), 2_000_000e6 + uint256(desk.monetaryOutstanding()));
    }

    function test_realValuationCallbackCanReadButCannotReenterDeskFinances() public {
        _issue();
        vm.warp(block.timestamp + YEAR);
        _invest(100e6);
        vm.prank(ALICE); buck.transfer(address(desk), 10e6);
        ProbeCompoundDeskValuation probe = _valuation(true);
        probe.arm();
        desk.retireCredit(5e6, 5e6, 0);
        assertEq(probe.rejected(), 3);
        assertTrue(probe.completeRead());
        policy.set(-10, false);
        assertEq(desk.monetaryOperation(), 2);
        assertEq(probe.rejected(), 6);
        assertTrue(probe.completeRead());
        _assertAccount();
    }

    function test_deskFitsEvmRuntimeLimit() public pure {
        assertLe(type(BasketDesk).runtimeCode.length, 24_576);
    }
}
