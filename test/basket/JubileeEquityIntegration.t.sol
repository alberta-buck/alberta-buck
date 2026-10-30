// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {BN254} from "../../src/BN254.sol";
import {IdentityRegistry} from "../../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "../harness/IdentityRegistryHarness.sol";
import {BuckCredit} from "../../src/BuckCredit.sol";
import {BuckNet} from "../../src/BuckNet.sol";
import {BuckRenewal, BuckRenewalConsent} from "../../src/BuckRenewalConsent.sol";
import {JubileeFund, IJubileeBasket} from "../../src/JubileeFund.sol";
import {BuckBasketEquity} from "../../src/basket/BuckBasketEquity.sol";
import {BuckBasketEquityWheel} from "../../src/basket/BuckBasketEquityWheel.sol";
import {BuckBasketUniswapV3} from "../../src/basket/BuckBasketUniswapV3.sol";
import {IBasketUpkeep} from "../../src/basket/IBasketUpkeep.sol";
import {EqController, EqHolder, EqToken, IPool} from "./BuckBasketEquity.t.sol";

/// @dev Real BuckNet, fund and equity basket. Its experimental net mode has
///      no basket-issued credit, so leveraged low-K cases still need their own
///      integration before this is a production basket configuration.
contract JubileeEquityIntegrationTest is Test {
    uint256 constant YEAR = 365 days + 6 hours;
    uint256 constant Q = 1e36;
    uint160 constant MIN_SQRT = 4295128739;
    uint160 constant MAX_SQRT = 1461446703485210103287273052203988822378723970342;
    address constant GOV = address(0xA0);
    address constant ALICE = address(0xA11CE);
    address constant BOB = address(0xB0B);
    address constant INSURER = address(0x1551E1);
    IdentityRegistryHarness identity;
    BuckCredit credit;
    BuckNet buck;
    JubileeFund fund;
    BuckBasketEquity basket;
    EqController k;
    uint256 t0;

    function setUp() public {
        vm.warp(1_000_000);
        t0 = block.timestamp;
        identity = new IdentityRegistryHarness(GOV);
        credit = new BuckCredit();
        k = new EqController();
        k.setK(1e18);
        fund = new JubileeFund(GOV);
        buck = new BuckNet(address(credit), address(k), address(identity), address(fund));
        address factory = deployCode("out/UniswapV3Factory.sol/UniswapV3Factory.json");
        basket = new BuckBasketEquity(address(buck), address(k), factory, GOV, 3000, 600, 64, 500, 1e3);
        credit.setBuck(address(buck));
        vm.prank(GOV);
        identity.setBuck(address(buck));
        _bind(ALICE, false, address(this));
        _bind(BOB, false, address(this));
        _bind(INSURER, false, address(this));
        _bind(address(fund), true, address(this));
        _bind(address(basket), false, address(this));
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER, true);
        vm.prank(BOB);
        credit.setCreditIssuer(INSURER, true);
        vm.startPrank(GOV);
        basket.enableNetAccounting();
        basket.setVenue(address(new BuckBasketUniswapV3()));
        basket.setEquityWheel(address(new BuckBasketEquityWheel()));
        (bool ok,) = address(basket).call(abi.encodeWithSignature("setWheel(address)", address(this)));
        require(ok);
        fund.configure(buck, IJubileeBasket(address(basket)));
        vm.stopPrank();
    }

    function _bind(address target, bool carrying, address binder) internal {
        if (target.code.length == 0) vm.etch(target, address(new EqHolder()).code);
        vm.prank(binder);
        identity.bindContract(target, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, carrying);
    }
    function _credit(address owner, uint256 face) internal returns (uint256 id) {
        vm.prank(INSURER);
        id = credit.createCredit(owner, 0, face, face, BuckCredit.DepreciationType.NONE, 0, 0, 0);
        vm.prank(owner);
        buck.issue(id, face, face);
    }
    function _authorize(address owner, uint256 id, uint256 amount, uint256 minRelief) internal {
        vm.prank(owner);
        buck.authorizeReduction(id, amount, minRelief, uint64(block.timestamp + YEAR));
    }
    function _invest(uint256 amount) internal returns (uint256 id, uint256 nominal, uint256 shares) {
        vm.prank(GOV);
        return fund.invest(amount, type(uint256).max, amount, 1);
    }

    function uniswapV3MintCallback(uint256 amount0, uint256 amount1, bytes calldata) external {
        if (amount0 > 0) IERC20(IPool(msg.sender).token0()).transfer(msg.sender, amount0);
        if (amount1 > 0) IERC20(IPool(msg.sender).token1()).transfer(msg.sender, amount1);
    }

    function uniswapV3SwapCallback(int256 amount0, int256 amount1, bytes calldata) external {
        if (amount0 > 0) IERC20(IPool(msg.sender).token0()).transfer(msg.sender, uint256(amount0));
        if (amount1 > 0) IERC20(IPool(msg.sender).token1()).transfer(msg.sender, uint256(amount1));
    }

    function test_realBasketReusesTheFundReceiptAndPaysMarkedReliefWithoutASale() public {
        uint256 a = _credit(ALICE, 100e6);
        _credit(BOB, 100e6);
        vm.warp(t0 + YEAR);
        (uint256 id, uint256 nominal, uint256 shares) = _invest(1e6);
        assertGt(nominal, 1e6);
        assertEq(basket.receipt().ownerOf(id), address(fund));
        (uint256 secondId,, uint256 moreShares) = _invest(1e6);
        assertEq(secondId, id);
        (uint128 held,) = basket.holdings(id);
        assertEq(held, shares + moreShares);
        assertEq(basket.lien(), 0);
        assertEq(basket.reliefAccrued(), 0);
        assertEq(basket.equity(), buck.netBalanceOf(address(basket)));

        (, uint256 cash) = buck.fundBalancesScaled();
        uint256 marked = basket.valueOf(id);
        uint256 expected = (cash + marked * Q) * 9 / 200;
        _authorize(ALICE, a, 9e6, 0);
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduce(a, 9e6);
        assertEq(relief, expected);
        (uint128 remaining,) = basket.holdings(id);
        assertEq(remaining, held);
        assertEq(fund.positionId(), id);
        assertEq(buck.totalIssued(), 191e6);
    }

    function test_keeperReplenishesReserveFromTheRealSingleReceipt() public {
        _credit(ALICE, 1_000_000e6);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(19_000e6);
        (uint128 sharesBefore,) = basket.holdings(id);
        (, uint256 cashBefore) = buck.fundBalancesScaled();
        (uint256 target,,) = fund.previewReserve();
        assertLt(cashBefore, target);
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](0);
        vm.prank(GOV);
        uint256 proceeds = fund.redeemScheduled(1_000e6, 990e6, routes);
        (, uint256 cashAfter) = buck.fundBalancesScaled();
        (uint128 sharesAfter,) = basket.holdings(id);
        assertEq(cashAfter - cashBefore, proceeds);
        assertGe(proceeds, 990e6 * Q);
        assertEq(sharesAfter, sharesBefore - 1_000e6);
        assertEq(fund.positionId(), id);
        assertEq(basket.receipt().ownerOf(id), address(fund));
    }

    function test_netBasketMarkAccountsForTheCarryingPoolFee() public {
        _bind(address(this), false, address(this));
        _credit(ALICE, 2_000_000e6);
        vm.prank(ALICE);
        buck.transfer(address(this), 1_500_000e6);
        EqToken asset = new EqToken("Asset", "AST", 18);
        asset.mint(address(this), 2_000_000e18);
        asset.mint(ALICE, 100_000e18);
        vm.prank(GOV);
        address pool = basket.addBasketToken(address(asset), 18, 1e6, 10_000, 3000);
        _bind(pool, true, address(this));
        (,,,,,, int24 lower, int24 upper,,,) = basket.constituents(0);
        IPool(pool).mint(address(this), lower, upper, 1e18, "");
        vm.warp(t0 + YEAR);
        vm.startPrank(ALICE);
        asset.approve(address(basket), type(uint256).max);
        basket.deposit(address(asset), 100_000e18, 0);
        vm.stopPrank();
        (uint256 id,,) = _invest(10_000e6);
        assertEq(IBasketUpkeep(address(basket)).wheelStep(2, 0), 1);
        assertGt(basket.liquidityOf(0), 0);
        // Let the observation window move past the wheel's trade, so the
        // TWAP mark is constant during the following idle interval.
        vm.warp(block.timestamp + 601);
        uint256 grossBefore = basket.gross();
        uint256 receiptBefore = basket.valueOf(id);
        (uint256 poolGrossBefore, uint256 poolNetBefore) = buck.accountBalancesScaled(pool);
        assertGt(poolGrossBefore, 0);
        assertGt(poolNetBefore, 0);

        vm.warp(t0 + 11 * YEAR);
        (uint256 poolGrossAfter, uint256 poolNetAfter) = buck.accountBalancesScaled(pool);
        assertEq(poolGrossAfter, poolGrossBefore);
        assertLt(poolNetAfter, poolNetBefore);
        uint256 grossAfter = basket.gross();
        assertLt(grossAfter, grossBefore);
        // No pool trades occurred, so its nominal mark is unchanged and the
        // marked TOKEN/LP value follows the Carrying pool's net fraction.
        assertApproxEqRel(grossAfter, grossBefore * poolNetAfter / poolNetBefore, 2e12);
        assertLt(basket.valueOf(id), receiptBefore);
    }

    function test_markedReliefRequiresPoolTwapHistoryInsteadOfSpotFallback() public {
        _bind(address(this), false, address(this));
        uint256 a = _credit(ALICE, 2_000_000e6);
        vm.prank(ALICE);
        buck.transfer(address(this), 1_500_000e6);
        EqToken asset = new EqToken("Asset", "AST", 18);
        asset.mint(address(this), 2_000_000e18);
        asset.mint(ALICE, 100_000e18);
        vm.warp(t0 + YEAR);
        vm.prank(GOV);
        address pool = basket.addBasketToken(address(asset), 18, 1e6, 10_000, 3000);
        _bind(pool, true, address(this));
        (,,,,,, int24 lower, int24 upper,,,) = basket.constituents(0);
        IPool(pool).mint(address(this), lower, upper, 1e18, "");
        vm.startPrank(ALICE);
        asset.approve(address(basket), type(uint256).max);
        basket.deposit(address(asset), 100_000e18, 0);
        vm.stopPrank();
        _invest(10_000e6);
        assertGt(basket.idleToken(0), 0);

        vm.expectRevert(JubileeFund.MarkUnavailable.selector);
        fund.previewRelief(a, 50_000e6);
        vm.warp(block.timestamp + 601);
        (uint256 relief,, bool exceeds) = fund.previewRelief(a, 50_000e6);
        assertGt(relief, 0);
        assertFalse(exceeds);
    }

    function test_renewalKeepsRealBasketSharesWhileUpdatingTheFacility() public {
        uint256 a = _credit(ALICE, 100e6);
        _credit(BOB, 1900e6);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(2e6);
        (uint128 sharesBefore,) = basket.holdings(id);
        BuckRenewalConsent consent = buck.renewalConsent();
        BuckRenewal.Terms memory terms = BuckRenewal.Terms({
            face: 200e6, floor: 200e6, activatedFace: 200e6, additionalIssuance: 50e6,
            maxPremium: 0, minimumRelief: 0, expectedPrincipal: 100e6,
            expectedCreditHash: consent.currentCreditTermsHash(a),
            depType: BuckCredit.DepreciationType.NONE, depRate: 0, depStartAt: 0,
            premiumRate: 0, deadline: uint64(block.timestamp + YEAR)
        });
        bytes32 digest = consent.renewalDigest(ALICE, a, terms);
        vm.prank(INSURER);
        consent.propose(a, digest);
        vm.prank(ALICE);
        consent.authorize(a, digest);
        vm.prank(ALICE);
        (uint256 relief,,) = fund.renew(a, terms);
        (uint128 sharesAfter,) = basket.holdings(id);
        assertEq(sharesAfter, sharesBefore);
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(a);
        assertEq(principal, 150e6 - relief);
        assertEq(seconds_, 0);
        assertEq(credit.ownerOf(a), ALICE);
        assertEq(buck.totalIssued(), 2050e6 - relief);
    }

    function test_agedCarryingFundInvestmentDoesNotDiluteExistingDepositor() public {
        _credit(ALICE, 1000e6);
        _credit(BOB, 1000e6);
        vm.startPrank(ALICE);
        buck.approve(address(basket), 100e6);
        uint256 aliceId = basket.deposit(address(buck), 100e6, 0);
        vm.stopPrank();
        vm.warp(t0 + YEAR);
        uint256 beforeValue = basket.valueOf(aliceId);
        (uint256 fundId, uint256 nominal, uint256 shares) = _invest(2e6);
        assertGt(nominal, 2e6);
        assertApproxEqAbs(basket.valueOf(aliceId), beforeValue, 1);
        (uint128 fundShares, uint128 basis) = basket.holdings(fundId);
        assertEq(fundShares, shares);
        assertEq(basis, 2e6);
        assertEq(basket.equity(), buck.netBalanceOf(address(basket)));
    }

    function test_dailyWheelWorksWithoutLegacySignedBalanceMethods() public {
        _credit(ALICE, 100e6);
        vm.warp(t0 + YEAR);
        _invest(1e6);
        assertEq(IBasketUpkeep(address(basket)).wheelStep(0, 0), 1);
        assertEq(basket.lien(), 0);
    }

    function test_netModeCannotEnableLegacyBasketCredit() public {
        vm.expectRevert(BuckBasketEquity.NetCreditUnavailable.selector);
        vm.prank(GOV);
        basket.openCredit(address(credit), 1e12);
        vm.expectRevert();
        vm.prank(GOV);
        basket.enableNetAccounting();
    }

    function test_zeroEquityExitRetainsTheFundReceipt() public {
        uint256 a = _credit(ALICE, 100e6);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(1e6);
        (uint128 beforeShares,) = basket.holdings(id);
        vm.warp(t0 + 9001 * YEAR);
        buck.processMaturities(1);
        assertEq(basket.equity(), 0);
        vm.prank(ALICE);
        fund.closeMatured(a);
        assertEq(fund.positionId(), id);
        (uint128 afterShares,) = basket.holdings(id);
        assertEq(afterShares, beforeShares);
        assertEq(buck.totalIssued(), 100e6);
        assertEq(buck.claimBook().deferredPrincipal(), 100e6);
    }

    function test_realAmmInKindExitCountsOnlyNetBuckProceeds() public {
        _bind(address(this), false, address(this));
        _credit(ALICE, 2_000_000e6);
        vm.prank(ALICE);
        buck.transfer(address(this), 1_500_000e6);
        EqToken asset = new EqToken("Asset", "AST", 18);
        asset.mint(address(this), 2_000_000e18);
        asset.mint(ALICE, 100_000e18);
        vm.prank(GOV);
        address pool = basket.addBasketToken(address(asset), 18, 1e6, 10_000, 3000);
        _bind(pool, true, address(this));
        (,,,,,, int24 lower, int24 upper,,,) = basket.constituents(0);
        IPool(pool).mint(address(this), lower, upper, 1e18, "");
        vm.warp(t0 + YEAR);

        vm.startPrank(ALICE);
        asset.approve(address(basket), type(uint256).max);
        basket.deposit(address(asset), 100_000e18, 0);
        vm.stopPrank();
        (uint256 id,,) = _invest(39_000e6);
        assertEq(IBasketUpkeep(address(basket)).wheelStep(2, 0), 1);
        assertGt(basket.liquidityOf(0), 0);
        uint256 walletBefore = buck.netBalanceOf(address(basket));
        (, uint256 cashBefore) = buck.fundBalancesScaled();
        (uint128 sharesBefore,) = basket.holdings(id);
        uint256 requestedShares = uint256(sharesBefore) / 5;
        // The value requested exceeds the basket's spendable cash, so its
        // existing in-kind exit path must unwind real V3 liquidity.
        uint256 shareValue = basket.equity() * requestedShares / basket.totalShares();
        assertGt(shareValue * 99 / 100, walletBefore);
        // Without a route, the separate keeper sale rejects the unconverted TOKEN.
        JubileeFund.SaleRoute[] memory noRoutes = new JubileeFund.SaleRoute[](0);
        vm.expectRevert(JubileeFund.MissingRoute.selector);
        vm.prank(GOV);
        fund.redeemScheduled(requestedShares, 0, noRoutes);
        (uint128 deferredShares,) = basket.holdings(id);
        assertEq(deferredShares, sharesBefore);
        assertEq(asset.balanceOf(address(fund)), 0);

        (,,,,,,,, bool buckIs0,,) = basket.constituents(0);
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](1);
        routes[0] = JubileeFund.SaleRoute({
            sqrtPriceLimitX96: buckIs0 ? MAX_SQRT - 1 : MIN_SQRT + 1,
            minimumNetBuck: type(uint256).max
        });
        vm.expectRevert(JubileeFund.SwapMinOut.selector);
        vm.prank(GOV);
        fund.redeemScheduled(requestedShares, 0, routes);
        (uint128 afterFailedSale,) = basket.holdings(id);
        assertEq(afterFailedSale, sharesBefore);
        assertEq(asset.balanceOf(address(fund)), 0);
        routes[0].minimumNetBuck = 1;
        vm.prank(GOV);
        uint256 proceeds = fund.redeemScheduled(requestedShares, 0, routes);
        assertEq(asset.balanceOf(address(fund)), 0);
        (, uint256 cashAfter) = buck.fundBalancesScaled();
        assertGt(proceeds, 0);
        assertEq(proceeds, cashAfter - cashBefore);
        (uint128 sharesAfter,) = basket.holdings(id);
        assertLe(sharesAfter, sharesBefore);
        assertEq(buck.totalIssued(), 2_000_000e6);
    }

    function test_agedPoolSaleAgainstMarkWithBuckAsToken0() public {
        _compareAgedPoolSaleWithMark(true, false);
    }

    function test_agedPoolSaleAgainstMarkWithBuckAsToken1() public {
        _compareAgedPoolSaleWithMark(false, false);
    }

    function test_poolShockMakesTwapMarkExceedExecutableValueWithBuckAsToken0() public {
        _compareAgedPoolSaleWithMark(true, true);
    }

    function test_poolShockMakesTwapMarkExceedExecutableValueWithBuckAsToken1() public {
        _compareAgedPoolSaleWithMark(false, true);
    }

    function _compareAgedPoolSaleWithMark(bool buckIs0, bool shock) private {
        _bind(address(this), false, address(this));
        uint256 facility = _credit(ALICE, 2_000_000e6);
        vm.prank(ALICE);
        buck.transfer(address(this), 1_500_000e6);

        // Give the same test TOKEN deterministic addresses on either side of
        // BuckNet so both Uniswap token orders exercise the real contracts.
        EqToken template = new EqToken("Asset", "AST", 18);
        address assetAddress = buckIs0 ? address(type(uint160).max - 1) : address(0x100000);
        vm.etch(assetAddress, address(template).code);
        EqToken asset = EqToken(assetAddress);
        asset.mint(address(this), 2_000_000e18);
        asset.mint(ALICE, 100_000e18);
        vm.prank(GOV);
        address pool = basket.addBasketToken(assetAddress, 18, 1e6, 10_000, 3000);
        _bind(pool, true, address(this));
        (,,,,,, int24 lower, int24 upper, bool actualOrder,,) = basket.constituents(0);
        assertEq(actualOrder, buckIs0);
        IPool(pool).mint(address(this), lower, upper, 1e18, "");
        vm.warp(t0 + YEAR);
        vm.startPrank(ALICE);
        asset.approve(address(basket), type(uint256).max);
        basket.deposit(assetAddress, 100_000e18, 0);
        vm.stopPrank();
        (uint256 id,,) = _invest(39_000e6);
        assertEq(IBasketUpkeep(address(basket)).wheelStep(2, 0), 1);
        assertGt(basket.liquidityOf(0), 0);

        // Ten idle years make the pool's carried fee substantial. The fund
        // marks its receipt at TWAP, then converts the in-kind TOKEN through
        // that very pool; a mark is not a guaranteed executable sale price.
        vm.warp(t0 + 11 * YEAR);
        (uint256 poolGross, uint256 poolNet) = buck.accountBalancesScaled(pool);
        assertLt(poolNet * 100, poolGross * 85);
        if (shock) {
            (uint256 reliefBefore,,) = fund.previewRelief(facility, 10_000e6);
            // The pool has the required TWAP history, but an external TOKEN
            // sale can move spot before that historical average catches up.
            IPool(pool).swap(address(this), !buckIs0, int256(500_000e18),
                buckIs0 ? MAX_SQRT - 1 : MIN_SQRT + 1, "");
            (uint256 reliefAfter,, bool overLimit) = fund.previewRelief(facility, 10_000e6);
            assertFalse(overLimit);
            assertApproxEqRel(reliefAfter, reliefBefore, 1e9);
            // Show that a real reduction can pay that stale value, without
            // changing the state used for the following sale comparison.
            uint256 snapshot = vm.snapshotState();
            _authorize(ALICE, facility, 10_000e6, 0);
            vm.prank(ALICE);
            (uint256 paid,) = fund.reduce(facility, 10_000e6);
            assertEq(paid, reliefAfter);
            assertTrue(vm.revertToState(snapshot));
        }
        vm.prank(GOV);
        fund.setReserveParameters(10_000, 100);
        (uint256 target,,) = fund.previewReserve();
        (, uint256 cashBefore) = buck.fundBalancesScaled();
        assertLt(cashBefore, target);
        (uint128 shares, uint128 basis) = basket.holdings(id);
        uint256 requested = uint256(shares) / 5;
        uint256 mark = basket.valueOf(id);
        if (mark > basis) mark -= (mark - basis) * basket.LAMBDA_BP() / 10_000;
        uint256 requestedMark = mark * requested / shares;
        uint256 shareValue = basket.equity() * requested / basket.totalShares();
        assertGt(shareValue * 99 / 100, buck.netBalanceOf(address(basket)));

        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](1);
        routes[0] = JubileeFund.SaleRoute({
            sqrtPriceLimitX96: buckIs0 ? MAX_SQRT - 1 : MIN_SQRT + 1,
            minimumNetBuck: 1
        });
        vm.prank(GOV);
        uint256 proceeds = fund.redeemScheduled(requested, 0, routes);
        emit log_named_uint("requested marked BUCK", requestedMark);
        emit log_named_uint("actual net BUCK", proceeds / Q);
        assertGt(proceeds, 0);
        assertLt(proceeds, requestedMark * Q);
        if (shock) assertLt(proceeds, requestedMark * Q * 8 / 10);
        else assertGt(proceeds, requestedMark * Q * 98 / 100);
        assertEq(asset.balanceOf(address(fund)), 0);
        (, uint256 cashAfter) = buck.fundBalancesScaled();
        assertEq(cashAfter - cashBefore, proceeds);
        (uint128 remaining,) = basket.holdings(id);
        assertEq(remaining, shares - requested);
        assertEq(fund.positionId(), id);
    }
}
