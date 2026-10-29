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

    function test_realBasketReusesTheFundReceiptAndPaysActualProportionalRelief() public {
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
        uint256 requested = uint256(held) / 4;
        uint256 ratio = requested * 1e18 / basket.totalShares();
        uint256 paid = basket.equity() * ratio / 1e18;
        _authorize(ALICE, a, 50e6, 0);
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduce(a, 50e6);
        assertEq(relief, cash / 4 + paid * Q);
        (uint128 remaining,) = basket.holdings(id);
        assertEq(remaining, uint256(held) - requested);
        assertEq(fund.positionId(), id);
        assertEq(buck.totalIssued(), 150e6);
    }

    function test_renewalSellsRealBasketSharesBeforeUpdatingTheFacility() public {
        uint256 a = _credit(ALICE, 100e6);
        _credit(BOB, 100e6);
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
        assertEq(sharesAfter, uint256(sharesBefore) - uint256(sharesBefore) / 2);
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(a);
        assertEq(principal, 150e6 - relief);
        assertEq(seconds_, 0);
        assertEq(credit.ownerOf(a), ALICE);
        assertEq(buck.totalIssued(), 250e6 - relief);
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
        _authorize(ALICE, a, 100e6, 0);
        vm.prank(ALICE);
        fund.reduce(a, 100e6);
        assertEq(fund.positionId(), id);
        (uint128 afterShares,) = basket.holdings(id);
        assertEq(afterShares, beforeShares);
        assertEq(buck.totalIssued(), 0);
    }

    function test_realAmmInKindExitCountsOnlyNetBuckProceeds() public {
        _bind(address(this), false, address(this));
        uint256 a = _credit(ALICE, 2_000_000e6);
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
        uint256 walletBefore = buck.netBalanceOf(address(basket));
        (, uint256 cashBefore) = buck.fundBalancesScaled();
        (uint128 sharesBefore,) = basket.holdings(id);
        (uint256 selected, uint256 total) = buck.selectedClaimSeconds(a, 400_000e6);
        uint256 requestedShares = uint256(sharesBefore) * selected / total;
        // The value requested exceeds the basket's spendable cash, so its
        // existing in-kind exit path must unwind real V3 liquidity.
        uint256 shareValue = basket.equity() * requestedShares / basket.totalShares();
        assertGt(shareValue * 99 / 100, walletBefore);
        _authorize(ALICE, a, 400_000e6, 0);
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduce(a, 400_000e6);
        assertGt(asset.balanceOf(address(fund)), 0);
        (, uint256 cashAfter) = buck.fundBalancesScaled();
        uint256 actualProceeds = cashAfter + relief - cashBefore;
        assertEq(relief, cashBefore * selected / total + actualProceeds);
        (uint128 sharesAfter,) = basket.holdings(id);
        assertLe(sharesAfter, sharesBefore);
        assertEq(buck.totalIssued(), 1_600_000e6);
    }
}
