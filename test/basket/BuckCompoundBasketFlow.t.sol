// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {JubileeEquityFixture} from "./JubileeEquityIntegration.t.sol";
import {CompoundBasketHarness} from "./BuckCompoundBasket.t.sol";
import {BuckBasketEquity} from "../../src/basket/BuckBasketEquity.sol";
import {BuckBasketEquityStorage} from "../../src/basket/BuckBasketEquityStorage.sol";
import {BuckBasketUniswapV3} from "../../src/basket/BuckBasketUniswapV3.sol";
import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";
import {BasketVenueFacet} from "../../src/basket/BasketVenueFacet.sol";
import {IBasketUpkeep} from "../../src/basket/IBasketUpkeep.sol";
import {ControlledPairIntent} from "../harness/BasketIntent.sol";
import {EqToken, EqController, IPool} from "./BuckBasketEquity.t.sol";
import {BasketReference} from "../../src/basket/BasketReference.sol";
import {IBuckBasketVenue} from "../../src/basket/IBuckBasketVenue.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

contract RejectCompoundPosition is BuckBasketUniswapV3 {
    error RejectedPosition();
    function positionMint(uint256 i, uint256 tokenAmount, uint256 buckAmount)
        public override onlySelf returns (uint128, uint256, uint256)
    {
        super.positionMint(i, tokenAmount, buckAmount);
        revert RejectedPosition();
    }
}

contract CutKAfterCompoundPosition is BuckBasketUniswapV3 {
    function positionMint(uint256 i, uint256 tokenAmount, uint256 buckAmount)
        public override onlySelf returns (uint128 l, uint256 t, uint256 b)
    {
        (l, t, b) = super.positionMint(i, tokenAmount, buckAmount);
        EqController(address(controller)).setK(0);
    }
}

interface IV3Factory {
    function createPool(address a, address b, uint24 fee) external returns (address);
}

interface IV3Init {
    function initialize(uint160 sqrtPriceX96) external;
    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, uint8, bool);
}

/// @dev The wheel's arbitrage configuration, as the price book reads it (ArbKind).
contract BookMarkets {
    struct Triangle { address token; address own; address usd; uint256 index; }
    address public arbBuck;
    address public arbUsdc;
    address public poolUb;
    Triangle[] public triangles;

    constructor(address buck_, address usdc_, address ub_) {
        (arbBuck, arbUsdc, poolUb) = (buck_, usdc_, ub_);
    }

    function add(address token, address own, address usd, uint256 index) external {
        triangles.push(Triangle(token, own, usd, index));
    }

    function triangleCount() external view returns (uint256) { return triangles.length; }
}

/// @dev Actual Buck, claim book, fund, credit NFT and V3 pools. Harness probes
///      isolate funding/rollback boundaries; BasketPublicLifecycleTest covers
///      the unmodified shell's public opening and complete V3 lifecycle.
contract BuckCompoundBasketFlowTest is JubileeEquityFixture {
    EqToken[3] assets;
    address[3] pools;
    CompoundBasketHarness b;

    function _makeBasket(address factory) internal override returns (BuckBasketEquity) {
        return new CompoundBasketHarness(address(buck), address(k), factory, GOV);
    }

    function _open(uint256 face) private {
        b = CompoundBasketHarness(address(basket));
        vm.startPrank(GOV);
        basket.setNetMark(true);
        b.openAccountingCredit(address(credit), face);
        vm.stopPrank();
    }

    function _pools() private {
        _bind(address(this), false, address(this));
        _credit(ALICE, 2_000_000e6);
        vm.prank(ALICE);
        buck.transfer(address(this), 1_000_000e6);
        for (uint256 i; i < 3; ++i) {
            assets[i] = new EqToken("Asset", "AST", 18);
            assets[i].mint(address(this), 1_000_000e18);
            assets[i].mint(ALICE, 10_000e18);
            vm.prank(GOV);
            pools[i] = basket.addBasketToken(address(assets[i]), 18, 1e6, 10_000 / (i + 1), 3000);
            _bind(pools[i], true, address(this));
            (,,,,,, int24 lo, int24 hi,,,) = basket.constituents(i);
            IPool(pools[i]).mint(address(this), lo, hi, 1e17, "");
        }
        vm.warp(t0 + 601); // Actual V3 history, not the bootstrap spot fallback.
        k.setK(0.75e18);
        vm.startPrank(ALICE);
        for (uint256 i; i < 3; ++i) {
            assets[i].approve(address(basket), type(uint256).max);
            basket.deposit(address(assets[i]), 1000e18, 0);
        }
        vm.stopPrank();
    }

    function _machine() private {
        ControlledPairIntent intent = new ControlledPairIntent(address(basket));
        intent.setRate(0);
        vm.startPrank(GOV);
        basket.setVenue(address(new BasketVenueFacet(address(0))));
        basket.setEquityWheel(address(new BasketUpkeepFacet(address(intent), address(0), 0, 0)));
        vm.stopPrank();
    }

    // K's reference prices the pools in net BUCK (owner, 2026-11-15): a Carrying
    // pool's aged BUCK delivers only its net share when it leaves (BuckCash.move),
    // so each TOKEN's TWAP price counts at its own pool's net / gross. The
    // nominal reference (governance, for comparison only) reads quoted prices.
    // The parity reference (BasketReference) follows the same switch.
    function test_referencesReadCarryingPoolsInNetBuck() public {
        _pools();
        _machine();
        BasketReference parity = new BasketReference(address(basket), 1800, 3600, 500);
        BasketVenueFacet venue = BasketVenueFacet(address(basket));   // through the shell's fallback
        vm.warp(vm.getBlockTimestamp() + YEAR);            // the pools' BUCK ages a year, idle
        uint256 lo = type(uint256).max;
        uint256 hi;
        for (uint256 i; i < 3; ++i) {
            (uint256 gross, uint256 net) = buck.accountBalancesScaled(pools[i]);
            assertLt(net, gross, "an aged Carrying pool carries a fee");
            uint256 r = net * 1e18 / gross;
            if (r < lo) lo = r;
            if (r > hi) hi = r;
        }
        int256 netValue = venue.basketValueInBuck();
        assertEq(parity.basketValueInBuck(), netValue, "the parity reference reads net BUCK too");
        // The basket's marks are net at the venue (the one place they are netted), and agree
        // with the reference: every exit, deposit and plan reads these (BasketMarketOps.exitAssets too).
        uint256 sum;
        for (uint256 i; i < 3; ++i) {
            (,, uint256 amount,,,,,,,,) = basket.constituents(i);
            sum += Math.mulDiv(amount, IBuckBasketVenue(address(basket)).marks(i, basket.liquidityOf(i)).pTwap, 1e18);
        }
        assertEq(int256(sum), netValue, "marks and BVIB: one net price");
        vm.prank(GOV);
        BasketVenueFacet(address(basket)).setNominalReference(true);
        int256 nominalValue = venue.basketValueInBuck();
        assertEq(parity.basketValueInBuck(), nominalValue, "and nominal under the switch");
        assertLt(netValue, nominalValue, "net BVIB is below the quoted one once the pools carry a fee");
        // A weighted mean of the pools' ratios: between the least and the most hollow
        // (to a part per million: each price and product is floored).
        assertGe(uint256(netValue) * 1e18 / uint256(nominalValue) + 1e12, lo);
        assertLe(uint256(netValue) * 1e18 / uint256(nominalValue), hi + 1e12);
        vm.prank(GOV);
        BasketVenueFacet(address(basket)).setNominalReference(false);
        assertEq(venue.basketValueInBuck(), netValue);
        vm.expectRevert();
        BasketVenueFacet(address(basket)).setNominalReference(true);   // governance only
    }

    /// @dev A full-range V3 pool of `base` at `quoteRaw` per `baseRaw`, initialized, empty.
    function _newPool(address base, address quote, uint256 baseRaw, uint256 quoteRaw) private returns (address pool) {
        pool = IV3Factory(address(basket.v3Factory())).createPool(base, quote, 3000);
        (uint256 num, uint256 den) = base < quote ? (quoteRaw, baseRaw) : (baseRaw, quoteRaw);
        IV3Init(pool).initialize(uint160(Math.sqrt(Math.mulDiv(num, 1 << 192, den))));
    }

    /// @dev The dollar pool at a USDC per BUCK, fresh; TOKENs 0 and 1 at a USDC each (as in
    ///      BUCK), TOKEN 2 at 0.95; and the wheel's triangles naming them.
    function _bookMarkets() private returns (BookMarkets m, address[3] memory usd) {
        EqToken usdc = new EqToken("USD Coin", "USDC", 6);
        usdc.mint(address(this), 1_000_000e6);
        address dollar = _newPool(address(buck), address(usdc), 1e6, 1e6);
        _bind(dollar, true, address(this));
        IPool(dollar).mint(address(this), -887220, 887220, 1e9, "");
        m = new BookMarkets(address(buck), address(usdc), dollar);
        for (uint256 i; i < 3; ++i) {
            usd[i] = _newPool(address(assets[i]), address(usdc), 1e18, i == 2 ? 0.95e6 : 1e6);
            IPool(usd[i]).mint(address(this), -887220, 887220, 1e14, "");
            m.add(address(assets[i]), pools[i], usd[i], i);
        }
    }

    // The price book (owner, 2026-11-15): each TOKEN at what its best route fetches in net
    // BUCK -- its own pool at that pool's net / gross, or USDC and the USDC into the dollar
    // pool at the dollar pool's -- and in USDC, directly or through BUCK. K reads the book's
    // sample while it is fresh, the live direct net reference after.
    function test_priceBookPricesEachTokenByItsBestNetRoute() public {
        _pools();
        _machine();
        BasketReference book = new BasketReference(address(basket), 1800, 3600, 500);
        vm.warp(vm.getBlockTimestamp() + YEAR);            // the TOKEN/BUCK pools age a year, idle
        (BookMarkets m, address[3] memory usd) = _bookMarkets();
        vm.expectRevert(BasketReference.NotGovernance.selector);
        book.setMarkets(address(m));
        vm.prank(GOV);
        book.setMarkets(address(m));
        (,,,, uint16 next,,) = IV3Init(usd[0]).slot0();
        assertEq(next, basket.observationCardinality(), "the book widens its routes' oracles");

        // Without history over the window the routes through USDC are skipped, not refused.
        BasketVenueFacet venue = BasketVenueFacet(address(basket));
        BasketReference.Book memory b = book.prices();
        assertEq(b.twap, venue.basketValueInBuck(), "unreadable routes: the direct net reference");
        assertEq(b.usdc, 0, "no USDC value unless every TOKEN has a USDC route");

        vm.warp(vm.getBlockTimestamp() + 601);
        int256 direct = venue.basketValueInBuck();
        b = book.prices();
        uint256 sumBuck;
        uint256 sumUsdc;
        for (uint256 i; i < 3; ++i) {
            (,, uint256 amount,,,,,,,,) = basket.constituents(i);
            uint256 own = IBuckBasketVenue(address(basket)).marks(i, basket.liquidityOf(i)).pTwap;
            BasketReference.Bid memory bid = b.bids[i];
            if (i < 2) {
                // A USDC buys about a full BUCK from the fresh dollar pool; the aged pool
                // delivers only its net share of one.
                assertTrue(bid.buckViaUsdc, "through USDC");
                assertGt(bid.buck, own, "beats the hollow pool");
                assertApproxEqRel(bid.buck, 1e6, 2e14);
            } else {
                assertFalse(bid.buckViaUsdc, "0.95 USDC fetches less than the hollow pool's net");
                assertEq(bid.buck, own, "the direct route, at its pool's ratio");
                assertTrue(bid.usdcViaBuck, "and more USDC through BUCK than directly");
                assertApproxEqRel(bid.usdc, 1e6, 2e14);
            }
            sumBuck += Math.mulDiv(amount, bid.buck, 1e18);
            sumUsdc += Math.mulDiv(amount, bid.usdc, 1e18);
        }
        assertEq(b.twap, int256(sumBuck), "the basket at its bids");
        assertGt(b.twap, direct, "best routes fetch more than the direct net reference");
        assertEq(b.usdc, sumUsdc, "and in USDC");
        assertApproxEqRel(b.buckBid, 1e6, 2e14, "a USDC per BUCK");
        assertGe(b.buckAsk, b.buckBid, "a bought BUCK arrives at the dollar pool's ratio");

        book.observe();
        assertTrue(book.fresh());
        assertEq(book.rawValue(), b.twap);
        assertEq(book.valueUsdc(), b.usdc);
        assertEq(book.buckBid(), b.buckBid);
        (uint112 bidBuck, uint112 bidUsdc, bool viaUsdc, bool viaBuck) = book.bids(2);
        assertEq(bidBuck, b.bids[2].buck);
        assertEq(bidUsdc, b.bids[2].usdc);
        assertTrue(!viaUsdc && viaBuck, "the routes are recorded");

        // K reads the sample through the venue while it is fresh; stale, the live direct reference.
        address withBook = address(new BasketVenueFacet(address(book)));
        vm.prank(GOV);
        basket.setVenue(withBook);
        assertEq(venue.basketValueInBuck(), b.twap, "K reads the book");
        vm.warp(vm.getBlockTimestamp() + 3601);
        assertFalse(book.fresh());
        assertLt(venue.basketValueInBuck(), b.twap, "stale: the live direct net reference");

        // Under the comparison switch the BUCK bids are the direct quoted prices, as before.
        vm.prank(GOV);
        venue.setNominalReference(true);
        b = book.prices();
        for (uint256 i; i < 3; ++i) assertFalse(b.bids[i].buckViaUsdc, "quoted: direct");
        assertEq(b.twap, venue.basketValueInBuck(), "the quoted reference");
    }

    function test_budgetUsesExistingCashThenIssuesOnlyItsShortfall() public {
        _credit(ALICE, 1000e6);
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        basket.deposit(address(buck), 100e6, 0);
        vm.stopPrank();
        _open(1e12);
        (uint256 cash, uint256 capacity) = b.availableForAccounting();
        assertEq(cash, 100e6);
        assertEq(capacity, 100e6);
        assertEq(buck.balanceOf(address(basket)), cash);
        vm.prank(GOV);
        assertEq(b.fundForAccounting(140e6), 140e6);
        assertEq(basket.lien(), 40e6);
        assertEq(buck.balanceOf(address(basket)), 140e6);
        assertEq(basket.equity(), 100e6);
        vm.prank(GOV);
        assertEq(b.fundForAccounting(120e6), 120e6);
        assertEq(basket.lien(), 40e6); // Existing cash does not cause a second draw.
    }

    function test_cashFundedPurchaseCannotLeaveActualActivatedBackingShort() public {
        _pools();
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        basket.deposit(address(buck), 100e6, 0);
        vm.stopPrank();
        _open(1e12);
        vm.startPrank(GOV);
        b.fundForAccounting(200e6);
        b.buyFromCashForAccounting(0, 1e6);
        vm.stopPrank();
        assertEq(basket.lien(), 100e6);
        uint256 actualLimit = credit.currentValue(basket.creditId()) * k.currentBuckK() / 1e18;
        assertLe(basket.lien(), actualLimit);
    }

    function test_repaymentKeepsTheFreeBackingActiveAndCashBuysWithoutIssuing() public {
        _pools();
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        basket.deposit(address(buck), 100e6, 0);
        vm.stopPrank();
        _open(1e12);
        vm.prank(GOV);
        b.fundForAccounting(200e6);
        basket.retireCredit(50e6, 50e6, 0);
        (uint256 face0, uint256 active0,) = credit.creditInfo(basket.creditId());
        assertEq(active0, face0);       // repayment re-activates the self-insured backing at once
        k.setK(0.02e18);
        b.refreshMark();
        assertGt(basket.headroom(), 0);
        uint256 supply = buck.totalIssued();
        vm.prank(GOV);
        b.buyFromCashForAccounting(0, 1e6);
        assertEq(basket.lien(), 50e6);
        assertEq(buck.totalIssued(), supply);
        (uint256 face, uint256 active,) = credit.creditInfo(basket.creditId());
        assertEq(active, face);
        assertGt(basket.headroom(), 0);
    }

    function test_machinePairsDepositsUsingActualIssuanceAndDailyHasNoSignedCalls() public {
        _pools();
        _open(1e12);
        _machine();
        assertEq(IBasketUpkeep(address(basket)).wheelStep(2, 0), 1);
        for (uint256 i; i < 3; ++i) assertGt(basket.liquidityOf(i), 0);
        assertGt(basket.lien(), 2900e6);
        assertLt(buck.balanceOf(address(basket)), 1e6);
        uint256 principal = basket.lien();
        assertEq(IBasketUpkeep(address(basket)).wheelStep(0, 0), 1);
        assertEq(basket.lien(), principal); // Marked claims are not automatic paydown.
    }

    function test_poolPaymentsPreserveAgedWalletAndDeliverFullNet() public {
        _pools();
        identity.setIsCarrying(address(basket), true);
        vm.startPrank(GOV);
        buck.setApprovedCarryingIssuer(address(basket), true);
        vm.stopPrank();
        vm.prank(ALICE);
        buck.transfer(address(basket), 50e6);
        vm.warp(vm.getBlockTimestamp() + 200 days);
        _open(1e12);
        _machine();
        (uint256 gross, uint256 net) = buck.accountBalancesScaled(address(basket));
        assertGt(gross, net);
        (uint256 poolGross, uint256 poolNet) = buck.accountBalancesScaled(pools[0]);
        IBasketUpkeep(address(basket)).wheelStep(2, 0);
        (uint256 afterGross, uint256 afterNet) = buck.accountBalancesScaled(pools[0]);
        assertGt(afterGross, poolGross);
        assertEq(afterGross - poolGross, afterNet - poolNet);
        (afterGross, afterNet) = buck.accountBalancesScaled(address(basket));
        assertEq(afterGross, gross);
        assertEq(afterNet, net);
    }

    function test_idleResponseRefreshesPoolWithoutMovingSpot() public {
        _pools();
        _open(1e12);
        _machine();
        IBasketUpkeep upkeep = IBasketUpkeep(address(basket));
        upkeep.wheelStep(2, 0);
        vm.warp(vm.getBlockTimestamp() + 220 days);
        IBuckBasketVenue venue = IBuckBasketVenue(address(basket));
        (uint256 index, uint128 slice,) = venue.refreshQuote(9900, 9950, type(uint256).max);
        assertGt(slice, 0);
        address pool = pools[index];
        uint128 liquidity = basket.liquidityOf(index);
        (uint160 spot,,,,,,) = IV3Init(pool).slot0();
        (uint256 gross, uint256 net) = buck.accountBalancesScaled(pool);
        uint256 lien = basket.lien();
        upkeep.wheelStep(2, 0);
        (uint160 afterSpot,,,,,,) = IV3Init(pool).slot0();
        (uint256 afterGross, uint256 afterNet) = buck.accountBalancesScaled(pool);
        assertEq(afterSpot, spot);
        assertApproxEqRel(basket.liquidityOf(index), liquidity, 1e12);
        assertGt(basket.lien(), lien);
        assertGt(buck.balanceOf(address(basket)), 0);
        assertGt(Math.mulDiv(afterNet, 1e18, afterGross), Math.mulDiv(net, 1e18, gross));
    }

    function test_lowMarksSupportTokenExposureAndHeldCashAtTheirRespectiveRates() public {
        _pools();
        _open(1e12);
        vm.prank(GOV);
        basket.setPortfolioLimits(true);
        _machine();
        IBasketUpkeep(address(basket)).wheelStep(2, 0);
        vm.prank(ALICE);
        buck.transfer(address(basket), 25e6);
        for (uint256 i; i < 4; ++i) {
            uint256[4] memory rails = [uint256(0.1e18), 0.5e18, 0.75e18, 0.9e18];
            k.setK(rails[i]);
            b.refreshMark();
            uint256 held;
            for (uint256 j; j < 3; ++j) {
                held += IBuckBasketVenue(address(basket)).marks(j, basket.liquidityOf(j)).posLow / 2;
            }
            uint256 tokens = basket.grossAt(2) - held;
            uint256 cash = buck.netBalanceOf(address(basket));
            assertEq(basket.markNow(), tokens + (held + cash) * 1e18 / rails[i]);
            assertApproxEqAbs(buck.flashRoom(basket.creditId()),
                              tokens * rails[i] / 1e18 + held + cash - basket.lien(), 1);
        }
    }

    function test_faceCapStopsFurtherDrawsWithoutActivatingOrMintingExtraRoom() public {
        _pools();
        _open(600e6);
        _machine();
        assertEq(IBasketUpkeep(address(basket)).wheelStep(2, 0), 1);
        assertEq(basket.lien(), 450e6);
        (, uint256 active,) = credit.creditInfo(basket.creditId());
        assertEq(active, 600e6);
        (, uint256 room) = b.availableForAccounting();
        assertEq(room, 0);
        assertGt(basket.idleToken(1), 900e18);
    }

    function test_failedLpMintRollsBackIssuanceCoveragePoolTransfersAndClaims() public {
        _failedPlacement(address(new RejectCompoundPosition()), RejectCompoundPosition.RejectedPosition.selector);
    }

    function test_postTradeCreditFailureRollsBackTheDrawAndRealLpMint() public {
        _failedPlacement(address(new CutKAfterCompoundPosition()),
            BuckBasketEquityStorage.CreditShortfallIncreased.selector);
        assertEq(k.currentBuckK(), 0.75e18);
    }

    function _failedPlacement(address rejectedVenue, bytes4 reason) private {
        _pools();
        _open(1e12);
        vm.prank(GOV);
        basket.setVenue(rejectedVenue);
        uint256 supply = buck.totalIssued();
        uint256 cash = buck.balanceOf(pools[0]);
        uint256 seconds_ = buck.claimSeconds();
        vm.expectRevert(reason);
        IBasketUpkeep(address(basket)).wheelStep(2, 0);
        assertEq(basket.lien(), 0);
        (, uint256 active,) = credit.creditInfo(basket.creditId());
        assertEq(active, 0);
        assertEq(buck.totalIssued(), supply);
        assertEq(buck.claimSeconds(), seconds_);
        assertEq(buck.balanceOf(pools[0]), cash);
        assertEq(buck.balanceOf(address(basket)), 0);
        assertEq(basket.idleToken(0), 1000e18);
        assertEq(basket.liquidityOf(0), 0);
        assertFalse(basket.creditLive());
    }

    function test_zeroKCannotTurnCreditAvailabilityIntoWalletCash() public {
        _pools();
        _open(1e12);
        k.setK(0);
        vm.prank(GOV);
        assertEq(b.fundForAccounting(1000e6), 0);
        assertEq(basket.lien(), 0);
        assertEq(buck.balanceOf(address(basket)), 0);
        assertFalse(basket.creditLive());
    }

    function test_fullyAgedFacilityHasNoNewCapacityDespitePositiveCashAndCollateral() public {
        _credit(ALICE, 10_000e6);
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        basket.deposit(address(buck), 100e6, 0);
        vm.stopPrank();
        _open(1e12);
        vm.prank(GOV);
        b.fundForAccounting(140e6);
        vm.warp(t0 + 50 * YEAR);
        buck.processMaturities(10);
        vm.prank(ALICE);
        buck.transfer(address(basket), 200e6);
        b.refreshMark();
        assertGt(basket.headroom(), 0);
        (uint256 cash, uint256 capacity) = b.availableForAccounting();
        assertGt(cash, 200e6);
        assertEq(capacity, 0);
        vm.prank(GOV);
        assertEq(b.fundForAccounting(cash + 100e6), cash);
        assertEq(basket.lien(), 40e6);
    }

    function testFuzz_capacityAfterKChangeMatchesExecutableRootQuotes(
        uint64 capital_, uint64 face_, uint64 k_
    ) public {
        uint256 capital = bound(capital_, 1000, 10_000e6);
        uint256 face = bound(face_, 1, 2 * capital);
        _credit(ALICE, 2 * capital);
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        basket.deposit(address(buck), capital, 0);
        vm.stopPrank();
        _open(face);
        vm.prank(GOV);
        b.fundForAccounting(capital + (face < capital ? face : capital) / 2);
        uint256 prior = basket.lien();
        k.setK(bound(k_, 0, 1e18));
        b.refreshMark();
        (uint256 cash, uint256 room) = b.availableForAccounting();
        uint256[] memory ids = new uint256[](1);
        ids[0] = basket.creditId();
        if (room != 0) {
            vm.prank(address(basket));
            buck.quoteMint(room, ids);
        }
        vm.expectRevert();
        vm.prank(address(basket));
        buck.quoteMint(room + 1, ids);
        vm.prank(GOV);
        assertEq(b.fundForAccounting(cash + room), cash + room);
        assertEq(basket.lien(), prior + room);
        assertEq(buck.balanceOf(address(basket)), cash + room);
        assertEq(basket.equity(), capital);
    }
}
