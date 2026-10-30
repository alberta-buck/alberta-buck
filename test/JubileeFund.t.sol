// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckNet} from "../src/BuckNet.sol";
import {JubileeFund, IJubileeBasket} from "../src/JubileeFund.sol";
import {BuckBasketReceipt} from "../src/basket/BuckBasketReceipt.sol";
import {NetK} from "./BuckNet.t.sol";
import {WideCounter} from "../src/lib/WideCounter.sol";
import {BuckLedger} from "../src/lib/BuckLedger.sol";
import {BuckRenewal, BuckRenewalConsent} from "../src/BuckRenewalConsent.sol";

/// @dev Exercise the real token and coordinator with a controllable basket;
///      this is not a substitute for integration with the real equity basket.
contract JubileeInKindToken is ERC20 {
    bool public chargeTransferFee;
    constructor() ERC20("In-kind TOKEN", "TOK") {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
    function setTransferFee(bool enabled) external { chargeTransferFee = enabled; }
    function _update(address from, address to, uint256 amount) internal override {
        if (chargeTransferFee && from != address(0) && to != address(0)) {
            uint256 fee = amount / 100;
            super._update(from, address(0), fee);
            super._update(from, to, amount - fee);
        } else super._update(from, to, amount);
    }
}

contract JubileeBasketMock {
    uint256 constant Q = 1e36;
    address public immutable buck;
    BuckBasketReceipt public immutable receipt;
    JubileeInKindToken public immutable inKind;
    JubileeInKindToken public immutable secondInKind;
    address public swapPool;
    bool public mockBuckIsToken0;
    address public secondSwapPool;
    bool public secondBuckIsToken0;
    bool public secondEnabled;
    struct Holding { uint128 shares; uint128 basis; }
    mapping(uint256 => Holding) public holdings;
    uint8 public mode; // 0 normal, 1 underwater, 2 partial, 3 zero proceeds, 4 unexpected revert
    error Underwater();
    error Unexpected();

    constructor(address token) {
        buck = token;
        receipt = new BuckBasketReceipt(address(this));
        inKind = new JubileeInKindToken();
        secondInKind = new JubileeInKindToken();
    }
    function setMode(uint8 value) external { mode = value; }
    function setSwapPool(address pool, bool buckIsToken0) external {
        swapPool = pool;
        mockBuckIsToken0 = buckIsToken0;
    }
    function setSecondPool(address pool, bool buckIsToken0) external {
        secondEnabled = true;
        secondSwapPool = pool;
        secondBuckIsToken0 = buckIsToken0;
    }
    function constituentsLength() external view returns (uint256) { return secondEnabled ? 2 : 1; }
    function constituents(uint256 index) external view returns (
        address, uint8, uint256, uint256, uint24, address, int24, int24, bool, uint256, uint128
    ) {
        if (index == 0) return (address(inKind), 18, 0, 0, 0, swapPool, 0, 0, mockBuckIsToken0, 0, 0);
        require(index == 1 && secondEnabled);
        return (address(secondInKind), 18, 0, 0, 0, secondSwapPool, 0, 0, secondBuckIsToken0, 0, 0);
    }
    function deposit(address asset, uint256 amount, uint256 minShares) external returns (uint256 id) {
        require(asset == buck);
        uint256 received = _receive(amount);
        require(received >= minShares && received > 0);
        id = receipt.nextId();
        holdings[id] = Holding(uint128(received), uint128(received));
        require(receipt.mint(msg.sender) == id);
    }
    function increasePosition(uint256 id, address asset, uint256 amount, uint256 minReceived, uint256 minShares)
        external returns (uint256, uint256, uint256)
    {
        require(asset == buck && receipt.ownerOf(id) == msg.sender);
        uint256 received = _receive(amount);
        require(received >= minReceived && received >= minShares && received > 0);
        holdings[id].shares += uint128(received);
        holdings[id].basis += uint128(received);
        return (id, received, received);
    }
    function _receive(uint256 amount) private returns (uint256) {
        (uint256 beforeHeld,) = BuckNet(buck).heldAndUnspentScaled(address(this));
        IERC20(buck).transferFrom(msg.sender, address(this), amount);
        (uint256 afterHeld,) = BuckNet(buck).heldAndUnspentScaled(address(this));
        return (afterHeld - beforeHeld) / Q;
    }
    function redeemShares(uint256 id, uint256 requested, uint256)
        external returns (uint256 newId, uint256 paid, uint256 retired, uint256 remaining)
    {
        require(receipt.ownerOf(id) == msg.sender && requested > 0);
        if (mode == 1) revert Underwater();
        if (mode == 4) revert Unexpected();
        Holding storage h = holdings[id];
        uint256 beforeShares = h.shares;
        require(requested <= beforeShares);
        retired = mode == 2 ? requested / 2 : requested;
        if (mode != 3) {
            paid = BuckNet(buck).netBalanceOf(address(this)) * retired / beforeShares;
            IERC20(buck).transfer(msg.sender, paid);
        } else {
            uint256 tokenAmount = inKind.balanceOf(address(this)) * retired / beforeShares;
            inKind.transfer(msg.sender, tokenAmount);
            if (secondEnabled) {
                uint256 otherAmount = secondInKind.balanceOf(address(this)) * retired / beforeShares;
                secondInKind.transfer(msg.sender, otherAmount);
            }
        }
        h.shares -= uint128(retired);
        h.basis -= uint128(uint256(h.basis) * retired / beforeShares);
        remaining = h.shares;
        if (remaining == 0) { receipt.burn(id); newId = 0; } else newId = id;
    }
}

interface IJubileeSwapCallbackMock {
    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data) external;
}

/// @dev Configurable callback behavior for failure and zero-output cases;
///      real V3 conversion is separately covered by JubileeEquityIntegration.
contract JubileeSwapPoolMock {
    IERC20 public immutable buck;
    IERC20 public asset;
    bool public immutable buckIsToken0;
    uint8 public mode; // 0 full, 1 excessive, 2 partial, 3 forged Underwater
    uint256 public output;
    error Underwater();
    constructor(IERC20 buck_, bool buckIs0) { buck = buck_; buckIsToken0 = buckIs0; }
    function setAsset(IERC20 asset_) external { asset = asset_; }
    function setBehavior(uint8 mode_, uint256 output_) external { mode = mode_; output = output_; }
    function swap(address recipient, bool zeroForOne, int256 amountSpecified, uint160, bytes calldata data)
        external returns (int256 d0, int256 d1)
    {
        require(zeroForOne == !buckIsToken0 && amountSpecified > 0);
        if (mode == 3) revert Underwater();
        uint256 amount = uint256(amountSpecified);
        uint256 owed = mode == 1 ? amount + 1 : mode == 2 ? amount / 2 : amount;
        if (output != 0) buck.transfer(recipient, output);
        d0 = buckIsToken0 ? -int256(output) : int256(owed);
        d1 = buckIsToken0 ? int256(owed) : -int256(output);
        uint256 beforeAsset = asset.balanceOf(address(this));
        IJubileeSwapCallbackMock(msg.sender).uniswapV3SwapCallback(d0, d1, data);
        require(asset.balanceOf(address(this)) >= beforeAsset + owed, "input short");
    }
}

contract JubileeInsurerStrategyMock {
    function send(IERC20 token, address to, uint256 amount) external { token.transfer(to, amount); }
}

contract JubileeInsurerPoolMock {
    IERC20 public immutable token;
    JubileeInsurerStrategyMock public immutable strategy;
    bool public fail;
    uint256 public calls;
    JubileeFund public reenter;
    bytes4 public reentryError;
    constructor(IERC20 token_) { token = token_; strategy = new JubileeInsurerStrategyMock(); }
    function approveToken() external { token.approve(address(token), type(uint256).max); }
    function invest(uint256 amount) external { token.transfer(address(strategy), amount); }
    function setFail(bool value) external { fail = value; }
    function setReenter(JubileeFund fund) external { reenter = fund; }
    function prepareRefund(uint256 tokenId, uint256 nominalRequired) external {
        calls++;
        if (address(reenter) != address(0)) {
            try reenter.reduce(tokenId, 1) {} catch (bytes memory reason) {
                reentryError = bytes4(reason);
            }
        }
        if (!fail) strategy.send(token, address(this), nominalRequired - token.balanceOf(address(this)));
    }
}

contract JubileeFundTest is Test {
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
    NetK k;
    uint256 t0;

    function setUp() public {
        vm.warp(1_000_000);
        t0 = block.timestamp;
        identity = new IdentityRegistryHarness(GOV);
        credit = new BuckCredit();
        k = new NetK();
        fund = new JubileeFund(GOV);
        buck = new BuckNet(address(credit), address(k), address(identity), address(fund));
        basket = new JubileeBasketMock(address(buck));
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
        vm.prank(GOV);
        fund.configure(buck, IJubileeBasket(address(basket)));
    }
    function _bind(address target, bool carrying, address binder) internal {
        if (target.code.length == 0) vm.etch(target, hex"60006000fd");
        vm.prank(binder);
        identity.bindContract(target, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, carrying);
    }
    function _credit(address owner, uint256 face, uint32 rate) internal returns (uint256 id) {
        vm.prank(INSURER);
        id = credit.createCredit(owner, 0, face, face, BuckCredit.DepreciationType.NONE, 0, 0, rate);
        vm.prank(owner);
        buck.issue(id, face, face - face * rate * 10 / 10_000);
    }
    function _authorize(address owner, uint256 id, uint256 amount, uint256 minRelief) internal {
        vm.prank(owner);
        buck.authorizeReduction(id, amount, minRelief, uint64(block.timestamp + YEAR));
    }
    function _scaled(WideCounter.Count memory c) internal pure returns (uint256) {
        return c.units * Q + c.fraction;
    }
    function _invest(uint256 net) internal returns (uint256 id, uint256 nominal, uint256 shares) {
        vm.prank(GOV);
        return fund.invest(net, type(uint256).max, net, 1);
    }
    function _pool() internal returns (JubileeInsurerPoolMock pool) {
        pool = new JubileeInsurerPoolMock(IERC20(address(buck)));
        _bind(address(pool), true, INSURER);
        _bind(address(pool.strategy()), true, INSURER);
        vm.prank(INSURER);
        buck.setInsurerPool(address(pool));
        pool.approveToken();
    }
    function _inKindSaleFixture() internal returns (uint256 a, uint256 id, JubileeSwapPoolMock pool) {
        a = _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        pool = new JubileeSwapPoolMock(IERC20(address(buck)), false);
        pool.setAsset(IERC20(address(basket.inKind())));
        _bind(address(pool), true, address(this));
        vm.prank(BOB);
        buck.transfer(address(pool), 2e6);
        vm.warp(t0 + YEAR);
        (id,,) = _invest(2e6);
        basket.inKind().mint(address(basket), 4e6);
        basket.setMode(3);
        basket.setSwapPool(address(pool), false);
        _authorize(ALICE, a, 50e6, 0);
    }

    function test_noAgeRequiresNoSaleOrRelief() public {
        uint256 id = _credit(ALICE, 100e6, 0);
        _authorize(ALICE, id, 100e6, 0);
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduce(id, 100e6);
        assertEq(relief, 0);
        assertEq(buck.totalIssued(), 0);
    }

    function test_fundFitsTheEvmRuntimeSizeLimit() public view {
        assertLe(address(fund).code.length, 24_576);
    }

    function test_equalFacilitiesReceiveTheirShareOfAvailableCash() public {
        uint256 a = _credit(ALICE, 100e6, 0);
        uint256 b = _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        (, uint256 beforeNet) = buck.fundBalancesScaled();
        _authorize(ALICE, a, 100e6, 0);
        vm.prank(ALICE);
        (uint256 first,) = fund.reduce(a, 100e6);
        assertEq(first, beforeNet / 2);
        _authorize(BOB, b, 100e6, 0);
        vm.prank(BOB);
        (uint256 second,) = fund.reduce(b, 100e6);
        assertEq(second, beforeNet - first);
        assertEq(buck.totalIssued(), 0);
    }

    function test_threeRawUnitsCloseAfterFiftyYearsUsingFractionalCash() public {
        uint256 a = _credit(ALICE, 3, 0);
        vm.warp(t0 + 50 * YEAR);
        buck.processMaturities(1);
        (, uint256 cash) = buck.fundBalancesScaled();
        _authorize(ALICE, a, 3, cash);
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduce(a, 3);
        assertEq(relief, cash);
        assertEq(buck.totalIssued(), 0);
        assertEq(buck.balanceOf(ALICE), 0);
    }

    function test_investGrossesUpAndReusesTheSingleReceipt() public {
        _credit(ALICE, 500e6, 0);
        vm.warp(t0 + YEAR);
        (uint256 id, uint256 nominal, uint256 shares) = _invest(2e6);
        assertGt(nominal, 2e6);
        assertEq(basket.receipt().ownerOf(id), address(fund));
        (uint256 secondId,, uint256 added) = _invest(2e6);
        assertEq(id, secondId);
        (uint128 totalShares,) = basket.holdings(id);
        assertEq(totalShares, shares + added);
        assertEq(fund.positionId(), id);
    }

    function test_saleUsesSelectedCashShareAndActualNetProceeds() public {
        uint256 a = _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(2e6);
        (, uint256 cash) = buck.fundBalancesScaled();
        (uint128 oldShares,) = basket.holdings(id);
        uint256 basketCash = buck.netBalanceOf(address(basket));
        uint256 expectedProceeds = (basketCash * (uint256(oldShares) / 2) / oldShares) * Q;
        // The basket keeps sub-raw-unit receipt rounding dust. Acquire one raw
        // unit so Alice can close the entire facility despite that real loss.
        vm.prank(BOB);
        buck.transfer(ALICE, 1);
        _authorize(ALICE, a, 100e6, 0);
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduce(a, 100e6);
        assertEq(relief, cash / 2 + expectedProceeds);
        (uint128 left,) = basket.holdings(id);
        assertEq(left, uint256(oldShares) - uint256(oldShares) / 2);
        assertEq(fund.positionId(), id);
    }

    function test_lifetimeFundAndInvestmentFlowsTrackActualNetValue() public {
        uint256 a = _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        (, uint256 beforeInvestment) = buck.fundBalancesScaled();
        _invest(2e6);
        (, uint256 afterInvestment) = buck.fundBalancesScaled();
        (WideCounter.Count memory invested, WideCounter.Count memory sold) = fund.investmentFlows();
        assertEq(_scaled(invested), beforeInvestment - afterInvestment);
        assertEq(_scaled(sold), 0);
        _authorize(ALICE, a, 50e6, 0);
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduce(a, 50e6);
        (, uint256 cash) = buck.fundBalancesScaled();
        (invested, sold) = fund.investmentFlows();
        assertEq(_scaled(sold), cash + relief - afterInvestment);
        BuckLedger.FundFlows memory flows = buck.fundFlows();
        assertEq(_scaled(flows.transfersOut), _scaled(invested));
        assertEq(_scaled(flows.transfersIn), _scaled(sold));
        assertEq(_scaled(flows.relief), relief);
        assertEq(_scaled(flows.netDemurrage) + _scaled(flows.transfersIn),
                 cash + _scaled(flows.transfersOut) + _scaled(flows.relief));
    }

    function test_fullSaleBurnsTheSingleReceipt() public {
        uint256 a = _credit(ALICE, 100e6, 0);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(1e6);
        _credit(BOB, 100e6, 0); // New principal has no claim on older fund value.
        vm.prank(BOB);
        buck.transfer(ALICE, 1);
        _authorize(ALICE, a, 100e6, 0);
        vm.prank(ALICE);
        fund.reduce(a, 100e6);
        assertEq(fund.positionId(), 0);
        BuckBasketReceipt receipt = basket.receipt();
        vm.expectRevert();
        receipt.ownerOf(id);
    }

    function test_newFacilityDoesNotSellExistingPositionOrReceiveOldRelief() public {
        _credit(ALICE, 100e6, 0);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(1e6);
        uint256 b = _credit(BOB, 100e6, 0);
        (uint128 beforeShares,) = basket.holdings(id);
        _authorize(BOB, b, 100e6, 0);
        vm.prank(BOB);
        (uint256 relief,) = fund.reduce(b, 100e6);
        (uint128 afterShares,) = basket.holdings(id);
        assertEq(relief, 0);
        assertEq(afterShares, beforeShares);
    }

    function test_investmentGainIsPaidOnlyWhenActuallySold() public {
        uint256 a = _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(1e6);
        vm.prank(BOB);
        buck.transfer(address(basket), 2e6);
        (, uint256 cash) = buck.fundBalancesScaled();
        (uint128 shares,) = basket.holdings(id);
        uint256 retired = uint256(shares) / 4;
        uint256 proceeds = buck.netBalanceOf(address(basket)) * retired / shares * Q;
        _authorize(ALICE, a, 50e6, 0);
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduce(a, 50e6);
        assertEq(relief, cash / 4 + proceeds);
    }

    function test_partialSaleUsesOnlyActualProceedsAndKeepsUnsoldShares() public {
        uint256 a = _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(2e6);
        basket.setMode(2);
        (, uint256 cash) = buck.fundBalancesScaled();
        (uint128 oldShares,) = basket.holdings(id);
        _authorize(ALICE, a, 50e6, 0);
        // Half a facility out of two equal ones requests one quarter of shares.
        uint256 retired = (uint256(oldShares) / 4) / 2;
        uint256 expected = (buck.netBalanceOf(address(basket)) * retired / oldShares) * Q;
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduce(a, 50e6);
        assertEq(relief, cash / 4 + expected);
        (uint128 left,) = basket.holdings(id);
        assertEq(left, uint256(oldShares) - retired);
    }

    function test_underwaterSaleKeepsPositionAndPaysOnlyCashShare() public {
        uint256 a = _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(2e6);
        basket.setMode(1);
        (, uint256 cash) = buck.fundBalancesScaled();
        (uint128 oldShares,) = basket.holdings(id);
        _authorize(ALICE, a, 50e6, 0);
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduce(a, 50e6);
        assertEq(relief, cash / 4);
        (uint128 left,) = basket.holdings(id);
        assertEq(left, oldShares);
        assertEq(fund.positionId(), id);
    }

    function test_inKindSaleWithoutRouteLeavesSharesAndTokenInTheBasket() public {
        uint256 a = _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(2e6);
        basket.inKind().mint(address(basket), 4e6);
        basket.setMode(3);
        (, uint256 cash) = buck.fundBalancesScaled();
        (uint128 oldShares,) = basket.holdings(id);
        _authorize(ALICE, a, 50e6, 0);
        vm.expectRevert(JubileeFund.MissingRoute.selector);
        vm.prank(ALICE);
        fund.reduce(a, 50e6);
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduceCashOnly(a, 50e6);
        assertEq(relief, cash / 4);
        assertEq(basket.inKind().balanceOf(address(fund)), 0);
        assertEq(basket.inKind().balanceOf(address(basket)), 4e6);
        (uint128 left,) = basket.holdings(id);
        assertEq(left, oldShares);
    }

    function test_cashOnlyDeferralStillHonorsTheOwnersMinimumRelief() public {
        (uint256 a, uint256 id,) = _inKindSaleFixture();
        (, uint256 cash) = buck.fundBalancesScaled();
        _authorize(ALICE, a, 50e6, cash / 4 + 1);
        (uint128 beforeShares,) = basket.holdings(id);
        vm.expectRevert(BuckNet.BadAuthorization.selector);
        vm.prank(ALICE);
        fund.reduceCashOnly(a, 50e6);
        (uint128 afterShares,) = basket.holdings(id);
        assertEq(afterShares, beforeShares);
        assertEq(basket.inKind().balanceOf(address(fund)), 0);
    }

    function test_zeroBuckConversionSellsOnlyNewTokenAndPreservesOldInventory() public {
        (uint256 a, uint256 id, JubileeSwapPoolMock pool) = _inKindSaleFixture();
        basket.inKind().mint(address(fund), 777);
        (, uint256 cash) = buck.fundBalancesScaled();
        (uint128 shares,) = basket.holdings(id);
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](1);
        routes[0] = JubileeFund.SaleRoute({sqrtPriceLimitX96: 1, minimumNetBuck: 0});
        pool.setBehavior(0, 0);
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduceWithRoutes(a, 50e6, routes);
        assertEq(relief, cash / 4);
        assertEq(basket.inKind().balanceOf(address(fund)), 777);
        assertEq(basket.inKind().balanceOf(address(pool)), 1e6);
        (uint128 remaining,) = basket.holdings(id);
        assertEq(remaining, uint256(shares) - uint256(shares) / 4);
    }

    function test_feeOnTransferAssetCanBeDeferredWithoutLosingShares() public {
        (uint256 a, uint256 id, JubileeSwapPoolMock pool) = _inKindSaleFixture();
        basket.inKind().setTransferFee(true);
        pool.setBehavior(0, 1e6);
        (uint128 shares,) = basket.holdings(id);
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](1);
        routes[0] = JubileeFund.SaleRoute({sqrtPriceLimitX96: 1, minimumNetBuck: 0});
        vm.expectRevert(bytes("input short"));
        vm.prank(ALICE);
        fund.reduceWithRoutes(a, 50e6, routes);
        (uint128 afterFailure,) = basket.holdings(id);
        assertEq(afterFailure, shares);
        assertEq(basket.inKind().balanceOf(address(fund)), 0);
        vm.prank(ALICE);
        fund.reduceCashOnly(a, 50e6);
        (uint128 afterDeferral,) = basket.holdings(id);
        assertEq(afterDeferral, shares);
        assertEq(basket.inKind().balanceOf(address(basket)), 4e6);
    }

    function test_swapCallbackCannotTakeMoreThanTheSelectedSaleReceived() public {
        (uint256 a, uint256 id, JubileeSwapPoolMock pool) = _inKindSaleFixture();
        basket.inKind().mint(address(fund), 777);
        (uint128 shares,) = basket.holdings(id);
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](1);
        routes[0] = JubileeFund.SaleRoute({sqrtPriceLimitX96: 1, minimumNetBuck: 0});
        pool.setBehavior(1, 0);
        vm.expectRevert(JubileeFund.BadSwapCallback.selector);
        vm.prank(ALICE);
        fund.reduceWithRoutes(a, 50e6, routes);
        (uint128 remaining,) = basket.holdings(id);
        assertEq(remaining, shares);
        assertEq(basket.inKind().balanceOf(address(fund)), 777);
        assertEq(basket.inKind().balanceOf(address(pool)), 0);
    }

    function test_partialConversionAndInvalidRouteRollBackTheBasketSale() public {
        (uint256 a, uint256 id, JubileeSwapPoolMock pool) = _inKindSaleFixture();
        (uint128 shares,) = basket.holdings(id);
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](1);
        routes[0] = JubileeFund.SaleRoute({sqrtPriceLimitX96: 1, minimumNetBuck: 0});
        pool.setBehavior(2, 0);
        vm.expectRevert(JubileeFund.IncompleteSwap.selector);
        vm.prank(ALICE);
        fund.reduceWithRoutes(a, 50e6, routes);
        (uint128 remaining,) = basket.holdings(id);
        assertEq(remaining, shares);
        JubileeFund.SaleRoute[] memory wrong = new JubileeFund.SaleRoute[](2);
        vm.expectRevert(JubileeFund.BadRoute.selector);
        vm.prank(ALICE);
        fund.reduceWithRoutes(a, 50e6, wrong);
        (remaining,) = basket.holdings(id);
        assertEq(remaining, shares);
    }

    function test_poolCannotForgeBasketUnderwaterToBypassConversionBounds() public {
        (uint256 a, uint256 id, JubileeSwapPoolMock pool) = _inKindSaleFixture();
        (uint128 shares,) = basket.holdings(id);
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](1);
        routes[0] = JubileeFund.SaleRoute({sqrtPriceLimitX96: 1, minimumNetBuck: 1});
        pool.setBehavior(3, 0);
        vm.expectRevert(JubileeSwapPoolMock.Underwater.selector);
        vm.prank(ALICE);
        fund.reduceWithRoutes(a, 50e6, routes);
        (uint128 remaining,) = basket.holdings(id);
        assertEq(remaining, shares);
    }

    function test_conversionMinimumUsesNetBuckAfterPoolDemurrage() public {
        (uint256 a,, JubileeSwapPoolMock pool) = _inKindSaleFixture();
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](1);
        routes[0] = JubileeFund.SaleRoute({sqrtPriceLimitX96: 1, minimumNetBuck: 1e6});
        pool.setBehavior(0, 1e6);
        vm.expectRevert(JubileeFund.SwapMinOut.selector);
        vm.prank(ALICE);
        fund.reduceWithRoutes(a, 50e6, routes);
        routes[0].minimumNetBuck = 980_000;
        (, uint256 cash) = buck.fundBalancesScaled();
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduceWithRoutes(a, 50e6, routes);
        assertGt(relief - cash / 4, 980_000 * Q);
        assertLt(relief - cash / 4, 1e6 * Q);
    }

    function test_multiAssetConversionIsAtomicAndPreservesUnrelatedInventory() public {
        (uint256 a, uint256 id, JubileeSwapPoolMock first) = _inKindSaleFixture();
        JubileeSwapPoolMock second = new JubileeSwapPoolMock(IERC20(address(buck)), true);
        second.setAsset(IERC20(address(basket.secondInKind())));
        _bind(address(second), true, address(this));
        vm.prank(BOB);
        buck.transfer(address(second), 2e6);
        basket.setSecondPool(address(second), true);
        basket.secondInKind().mint(address(basket), 8e6);
        basket.secondInKind().mint(address(fund), 777);
        first.setBehavior(0, 1e6);
        second.setBehavior(0, 1e6);
        (uint128 shares,) = basket.holdings(id);
        (, uint256 cash) = buck.fundBalancesScaled();
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](2);
        routes[0] = JubileeFund.SaleRoute({sqrtPriceLimitX96: 1, minimumNetBuck: 980_000});
        vm.expectRevert(JubileeFund.MissingRoute.selector);
        vm.prank(ALICE);
        fund.reduceWithRoutes(a, 50e6, routes);
        (uint128 afterFailure,) = basket.holdings(id);
        assertEq(afterFailure, shares);
        assertEq(basket.inKind().balanceOf(address(first)), 0);
        assertEq(basket.secondInKind().balanceOf(address(fund)), 777);
        (, uint256 cashAfterFailure) = buck.fundBalancesScaled();
        assertEq(cashAfterFailure, cash);

        routes[1] = JubileeFund.SaleRoute({sqrtPriceLimitX96: 1, minimumNetBuck: 1e6});
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduceWithRoutes(a, 50e6, routes);
        assertGt(relief - cash / 4, 1_980_000 * Q);
        assertEq(basket.inKind().balanceOf(address(first)), 1e6);
        assertEq(basket.secondInKind().balanceOf(address(second)), 2e6);
        assertEq(basket.secondInKind().balanceOf(address(fund)), 777);
    }

    function test_equalClaimsReceiveEqualConvertedProceedsInEitherOrder() public {
        (uint256 a,, JubileeSwapPoolMock pool) = _inKindSaleFixture();
        uint256 b = credit.tokenOfOwnerByIndex(BOB, 0);
        _authorize(BOB, b, 50e6, 0);
        pool.setBehavior(0, 1e6);
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](1);
        routes[0] = JubileeFund.SaleRoute({sqrtPriceLimitX96: 1, minimumNetBuck: 980_000});
        uint256 snapshot = vm.snapshotState();
        vm.prank(ALICE);
        (uint256 aliceFirst,) = fund.reduceWithRoutes(a, 50e6, routes);
        vm.prank(BOB);
        (uint256 bobSecond,) = fund.reduceWithRoutes(b, 50e6, routes);
        assertApproxEqAbs(aliceFirst, bobSecond, 2 * Q);
        assertTrue(vm.revertToState(snapshot));
        vm.prank(BOB);
        (uint256 bobFirst,) = fund.reduceWithRoutes(b, 50e6, routes);
        vm.prank(ALICE);
        (uint256 aliceSecond,) = fund.reduceWithRoutes(a, 50e6, routes);
        assertApproxEqAbs(bobFirst, aliceSecond, 2 * Q);
        assertApproxEqAbs(aliceFirst, aliceSecond, 2 * Q);
        assertApproxEqAbs(bobFirst, bobSecond, 2 * Q);
    }

    function test_unexpectedBasketFailurePropagatesAndKeepsAuthorization() public {
        uint256 a = _credit(ALICE, 100e6, 0);
        vm.warp(t0 + YEAR);
        _invest(1e6);
        basket.setMode(4);
        _authorize(ALICE, a, 50e6, 0);
        vm.expectRevert(JubileeBasketMock.Unexpected.selector);
        vm.prank(ALICE);
        fund.reduce(a, 50e6);
        (uint256 principal,,) = buck.reductionAuthorization(a);
        assertEq(principal, 50e6);
    }

    function test_minimumReliefFailureRollsBackTheSale() public {
        uint256 a = _credit(ALICE, 100e6, 0);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(1e6);
        (uint128 oldShares,) = basket.holdings(id);
        (, uint256 oldCash) = buck.fundBalancesScaled();
        _authorize(ALICE, a, 50e6, 50e6 * Q);
        vm.expectRevert(BuckNet.BadAuthorization.selector);
        vm.prank(ALICE);
        fund.reduce(a, 50e6);
        (uint128 remaining,) = basket.holdings(id);
        (, uint256 cash) = buck.fundBalancesScaled();
        assertEq(remaining, oldShares);
        assertEq(cash, oldCash);
    }

    function test_poolRestoresItsOwnNominalDepositBeforeRefund() public {
        JubileeInsurerPoolMock pool = _pool();
        pool.setReenter(fund);
        uint256 a = _credit(ALICE, 1000e6, 100);
        pool.invest(100e6);
        vm.warp(t0 + YEAR);
        _authorize(ALICE, a, 100e6, 0);
        vm.prank(ALICE);
        (, uint256 refund) = fund.reduce(a, 100e6);
        assertEq(refund, 10e6);
        assertEq(pool.calls(), 1);
        assertEq(pool.reentryError(), JubileeFund.Reentrant.selector);
        assertEq(buck.balanceOf(address(pool)), 0);
        assertEq(buck.balanceOf(address(pool.strategy())), 90e6);
    }

    function test_poolFailureRollsBackBasketSale() public {
        JubileeInsurerPoolMock pool = _pool();
        uint256 a = _credit(ALICE, 1000e6, 100);
        pool.invest(100e6);
        pool.setFail(true);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(1e6);
        (uint128 beforeShares,) = basket.holdings(id);
        _authorize(ALICE, a, 100e6, 0);
        vm.expectRevert(JubileeFund.InsufficientPoolLiquidity.selector);
        vm.prank(ALICE);
        fund.reduce(a, 100e6);
        (uint128 afterShares,) = basket.holdings(id);
        assertEq(afterShares, beforeShares);
        assertEq(pool.calls(), 0);
        assertEq(buck.premiumPrincipal(a), 100e6);
    }

    function test_onlyOwnerCanExecuteAuthorizedReduction() public {
        uint256 a = _credit(ALICE, 100e6, 0);
        _authorize(ALICE, a, 50e6, 0);
        vm.expectRevert(JubileeFund.NotOwner.selector);
        vm.prank(BOB);
        fund.reduce(a, 50e6);
    }

    function test_investLimitsAndUnexpectedReceipts() public {
        _credit(ALICE, 500e6, 0);
        vm.warp(t0 + YEAR);
        vm.expectRevert(JubileeFund.NominalLimit.selector);
        vm.prank(GOV);
        fund.invest(1e6, 1e6, 0, 0);
        vm.expectRevert(JubileeFund.NotGovernance.selector);
        vm.prank(ALICE);
        fund.invest(1e6, type(uint256).max, 0, 0);
        vm.expectRevert(JubileeFund.UnexpectedReceipt.selector);
        fund.onERC721Received(address(basket), address(0), 1, "");
        vm.expectRevert(JubileeFund.BadSwapCallback.selector);
        fund.uniswapV3SwapCallback(1, 0, "");
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](0);
        vm.expectRevert(JubileeFund.NotSelf.selector);
        fund.attemptSale(1, 1, 1, routes);
    }

    function test_issuanceCheckpointDoesNotBackdateFreshlyObservedExcess() public {
        _credit(ALICE, 1000e6, 0);
        assertEq(fund.reserveCheckpoint(), block.timestamp);
        vm.warp(t0 + YEAR);
        _credit(BOB, 1000e6, 0); // BuckNet observes the first year's excess now.
        (uint256 target, uint256 excess, uint256 budget) = fund.previewReserve();
        assertEq(target, 2e6 * Q); // 10% of 20 BUCK in capped target relief.
        assertGt(excess, 0);
        assertEq(budget, 0);
        assertEq(fund.reserveCheckpoint(), block.timestamp);

        vm.warp(block.timestamp + 1 days);
        (,, budget) = fund.previewReserve();
        assertApproxEqAbs(budget, excess / 2, 1e14);
    }

    function test_failedIssuanceCheckpointRollsBackMintAndNftActivation() public {
        vm.prank(INSURER);
        uint256 id = credit.createCredit(ALICE, 0, 100e6, 100e6,
                                         BuckCredit.DepreciationType.NONE, 0, 0, 0);
        vm.mockCallRevert(address(fund), abi.encodeWithSelector(fund.checkpointIssuance.selector),
                          abi.encodeWithSignature("Error(string)", "checkpoint failed"));
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "checkpoint failed"));
        vm.prank(ALICE);
        buck.issue(id, 100e6, 100e6);
        assertEq(buck.totalIssued(), 0);
        (, uint256 active,) = credit.creditInfo(id);
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(active, 0);
        assertEq(principal, 0);
    }

    function test_unconfiguredRealFundCannotBeSkippedOnIssuance() public {
        IdentityRegistryHarness freshIdentity = new IdentityRegistryHarness(GOV);
        BuckCredit freshCredit = new BuckCredit();
        NetK freshK = new NetK();
        JubileeFund freshFund = new JubileeFund(GOV);
        BuckNet freshBuck = new BuckNet(address(freshCredit), address(freshK),
                                        address(freshIdentity), address(freshFund));
        freshCredit.setBuck(address(freshBuck));
        vm.prank(GOV);
        freshIdentity.setBuck(address(freshBuck));
        address[3] memory accounts = [ALICE, INSURER, address(freshFund)];
        for (uint256 i = 0; i < accounts.length; i++) {
            freshIdentity.bindContract(accounts[i], BN254.g1(),
                IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, i == 2);
        }
        vm.prank(ALICE);
        freshCredit.setCreditIssuer(INSURER, true);
        vm.prank(INSURER);
        uint256 id = freshCredit.createCredit(ALICE, 0, 100e6, 100e6,
                                              BuckCredit.DepreciationType.NONE, 0, 0, 0);
        vm.expectRevert(JubileeFund.NotConfigured.selector);
        vm.prank(ALICE);
        freshBuck.issue(id, 100e6, 100e6);
        assertEq(freshBuck.totalIssued(), 0);
        (, uint256 active,) = freshCredit.creditInfo(id);
        assertEq(active, 0);
        assertFalse(freshIdentity.carryingModeHookEnabled());
    }

    function test_scheduleWaitsForThresholdAndInvestsHalfTheGap() public {
        _credit(ALICE, 1000e6, 0);
        vm.warp(t0 + YEAR);
        (uint256 target, uint256 excess,) = fund.checkpointReserve();
        assertEq(target, 2e6 * Q);
        assertGt(excess, target);
        vm.prank(GOV);
        (uint256 noId, uint256 noNominal, uint256 noShares) = fund.investScheduled(type(uint256).max, 0, 1);
        assertEq(noId, 0);
        assertEq(noNominal, 0);
        assertEq(noShares, 0);

        vm.warp(t0 + YEAR + 1 days);
        (uint256 newTarget, uint256 newExcess, uint256 budget) = fund.previewReserve();
        assertApproxEqAbs(budget, excess / 2, 1e14);
        (, uint256 beforeCash) = buck.fundBalancesScaled();
        vm.prank(GOV);
        (uint256 id,, uint256 shares) = fund.investScheduled(type(uint256).max, 0, 1);
        (, uint256 afterCash) = buck.fundBalancesScaled();
        uint256 spent = beforeCash - afterCash;
        assertGt(shares, 0);
        assertLe(spent, budget);
        assertLe(spent, newExcess);
        assertGe(spent, newTarget / 100);
        assertEq(fund.reserveBudgetScaled(), budget - spent);
        assertEq(fund.positionId(), id);

        uint256 remainingExcess = fund.observedExcessScaled();
        uint256 remainingBudget = fund.reserveBudgetScaled();
        vm.warp(block.timestamp + 1 days);
        (,, uint256 nextBudget) = fund.previewReserve();
        assertApproxEqAbs(nextBudget, (remainingExcess + remainingBudget) / 2, 1e14);
        vm.prank(GOV);
        (uint256 nextId,, uint256 moreShares) = fund.investScheduled(type(uint256).max, 0, 1);
        assertEq(nextId, id);
        assertGt(moreShares, 0);
    }

    function test_newDonationOnlyMaturesAfterItIsObserved() public {
        _credit(ALICE, 1000e6, 0);
        vm.warp(t0 + YEAR);
        _credit(BOB, 1000e6, 0);
        (, uint256 oldExcess,) = fund.previewReserve();
        vm.prank(BOB);
        buck.transfer(address(fund), 10e6);
        vm.warp(block.timestamp + 1 days);
        (, uint256 newExcess, uint256 budget) = fund.previewReserve();
        assertGt(newExcess, oldExcess + 9e6 * Q);
        assertApproxEqAbs(budget, oldExcess / 2, 1e14);
        fund.checkpointReserve();
        assertEq(fund.observedExcessScaled(), newExcess);
    }

    function test_belowThresholdAttemptSavesCheckpointAndMaturingBudget() public {
        _credit(ALICE, 1000e6, 0);
        vm.warp(t0 + YEAR);
        // The first attempt observes the newly accumulated surplus but has
        // no matured budget. It must not roll the observation back.
        vm.prank(GOV);
        (uint256 id,,) = fund.investScheduled(type(uint256).max, 0, 1);
        assertEq(id, 0);
        assertGt(fund.observedExcessScaled(), 0);
        vm.warp(block.timestamp + 1 seconds);
        vm.prank(GOV);
        (id,,) = fund.investScheduled(type(uint256).max, 0, 1);
        assertEq(id, 0);
        assertGt(fund.reserveBudgetScaled(), 0);
        assertEq(fund.reserveCheckpoint(), block.timestamp);
    }

    function test_failedScheduledInvestmentRollsBackItsBudgetCheckpoint() public {
        _credit(ALICE, 1000e6, 0);
        vm.warp(t0 + YEAR);
        fund.checkpointReserve();
        uint256 oldCheckpoint = fund.reserveCheckpoint();
        vm.warp(block.timestamp + 1 days);
        (,, uint256 budget) = fund.previewReserve();
        (, uint256 cash) = buck.fundBalancesScaled();
        vm.expectRevert();
        vm.prank(GOV);
        fund.investScheduled(type(uint256).max, 0, type(uint128).max);
        assertEq(fund.reserveCheckpoint(), oldCheckpoint);
        (,, uint256 afterBudget) = fund.previewReserve();
        (, uint256 afterCash) = buck.fundBalancesScaled();
        assertEq(afterBudget, budget);
        assertEq(afterCash, cash);
        assertEq(fund.positionId(), 0);
    }

    function test_parameterChangesCapBudgetAndDoNotBackdateNewExcess() public {
        _credit(ALICE, 1000e6, 0);
        vm.warp(t0 + YEAR);
        fund.checkpointReserve();
        vm.warp(block.timestamp + 1 days);
        (,, uint256 matured) = fund.previewReserve();
        assertGt(matured, 0);
        vm.prank(GOV);
        fund.setReserveParameters(10_000, 100);
        (, uint256 excess, uint256 budget) = fund.previewReserve();
        assertEq(excess, 0);
        assertEq(budget, 0);
        vm.prank(GOV);
        fund.setReserveParameters(1000, 100);
        (, excess, budget) = fund.previewReserve();
        assertGt(excess, 0);
        assertEq(budget, 0);
    }

    function test_reductionCheckpointsTheNewCashAndClaimBenchmark() public {
        uint256 a = _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        fund.checkpointReserve();
        vm.warp(block.timestamp + 1 days);
        _authorize(ALICE, a, 50e6, 0);
        vm.prank(ALICE);
        fund.reduce(a, 50e6);
        (, uint256 excess, uint256 budget) = fund.previewReserve();
        assertEq(fund.reserveCheckpoint(), block.timestamp);
        assertEq(fund.observedExcessScaled(), excess);
        assertLe(budget, excess);
    }

    function test_reservePermissionsAndScheduleBoundsAreEnforced() public {
        _credit(ALICE, 1000e6, 0);
        vm.warp(t0 + YEAR);
        fund.checkpointReserve();
        vm.warp(block.timestamp + 1 days);
        vm.expectRevert(JubileeFund.NotInvestmentKeeper.selector);
        vm.prank(ALICE);
        fund.investScheduled(type(uint256).max, 0, 1);
        vm.expectRevert(JubileeFund.NotToken.selector);
        fund.checkpointIssuance();
        vm.expectRevert(JubileeFund.BadAmount.selector);
        vm.prank(GOV);
        fund.setReserveParameters(10_001, 100);
    }

    function test_governanceCanAuthorizeAndRevokeAnInvestmentKeeper() public {
        _credit(ALICE, 1000e6, 0);
        vm.warp(t0 + YEAR);
        fund.checkpointReserve();
        vm.warp(block.timestamp + 1 days);
        vm.prank(GOV);
        fund.setInvestmentKeeper(BOB);
        vm.prank(BOB);
        (uint256 id,,) = fund.investScheduled(type(uint256).max, 0, 1);
        assertGt(id, 0);
        vm.expectRevert(JubileeFund.NotGovernance.selector);
        vm.prank(BOB);
        fund.invest(1, type(uint256).max, 0, 1);
        vm.prank(GOV);
        fund.setInvestmentKeeper(address(0));
        vm.expectRevert(JubileeFund.NotInvestmentKeeper.selector);
        vm.prank(BOB);
        fund.investScheduled(type(uint256).max, 0, 1);
    }

    function testFuzz_scheduledInvestmentNeverSpendsTheTargetReserve(uint64 principal_, uint8 days_) public {
        uint256 principal = bound(principal_, 100e6, 1e13);
        uint256 elapsed = bound(days_, 1, 30) * 1 days;
        _credit(ALICE, principal, 0);
        vm.warp(t0 + YEAR);
        fund.checkpointReserve();
        vm.warp(block.timestamp + elapsed);
        (uint256 target, uint256 excess, uint256 budget) = fund.previewReserve();
        (, uint256 beforeCash) = buck.fundBalancesScaled();
        vm.prank(GOV);
        fund.investScheduled(type(uint256).max, 0, 1);
        (, uint256 afterCash) = buck.fundBalancesScaled();
        assertLe(beforeCash - afterCash, budget);
        assertLe(beforeCash - afterCash, excess);
        assertGe(afterCash, target);
    }
}

/// @dev The existing fixture exercises the coordinator, token and actual NFT;
///      the strategy/basket and insurer liquidity contracts remain controllable.
contract JubileeRenewalTest is JubileeFundTest {
    function _terms(uint256 id, uint256 face, uint256 activated, uint32 rate, uint256 additional)
        internal view returns (BuckRenewal.Terms memory terms)
    {
        (uint256 principal,) = credit.issuanceOf(id);
        terms = BuckRenewal.Terms({
            face: face,
            floor: face,
            activatedFace: activated,
            additionalIssuance: additional,
            maxPremium: type(uint256).max,
            minimumRelief: 0,
            expectedPrincipal: principal,
            expectedCreditHash: buck.renewalConsent().currentCreditTermsHash(id),
            depType: BuckCredit.DepreciationType.NONE,
            depRate: 0,
            depStartAt: 0,
            premiumRate: rate,
            deadline: uint64(block.timestamp + YEAR)
        });
    }
    function _approveRenewal(uint256 id, BuckRenewal.Terms memory terms) internal returns (bytes32 digest) {
        BuckRenewalConsent consent = buck.renewalConsent();
        digest = consent.renewalDigest(ALICE, id, terms);
        vm.prank(INSURER);
        consent.propose(id, digest);
        vm.prank(ALICE);
        consent.authorize(id, digest);
    }

    function test_renewalCrystallizesActualReliefAndIssuesAgainstIncreasedCoverage() public {
        uint256 id = _credit(ALICE, 1000e6, 0);
        vm.warp(t0 + YEAR);
        (, uint256 cashBefore) = buck.fundBalancesScaled();
        uint256 beforeBalance = buck.balanceOf(ALICE);
        BuckRenewal.Terms memory terms = _terms(id, 2000e6, 2000e6, 0, 200e6);
        _approveRenewal(id, terms);
        vm.prank(ALICE);
        (uint256 relief, uint256 refund, uint256 premium) = fund.renew(id, terms);
        assertEq(relief, cashBefore / Q);
        assertApproxEqAbs(relief, 19_801_326, 1); // independent exp(-0.02) vector
        assertEq(refund, 0);
        assertEq(premium, 0);
        assertEq(buck.balanceOf(ALICE), beforeBalance + 200e6);
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(id);
        assertEq(principal, 1200e6 - relief);
        assertEq(seconds_, 0);
        assertEq(buck.totalIssued(), principal);
        (uint256 face, uint256 active,) = credit.creditInfo(id);
        assertEq(face, 2000e6);
        assertEq(active, 2000e6);
        assertEq(credit.ownerOf(id), ALICE);
        assertEq(buck.renewalConsent().authorization(id), bytes32(0));
        assertEq(buck.renewalConsent().proposal(id), bytes32(0));
        (, uint256 cashAfter) = buck.fundBalancesScaled();
        assertEq(cashAfter, cashBefore - relief * Q);
    }

    function test_renewalLeavesOtherNftPrincipalAndAgeUnchanged() public {
        uint256 id = _credit(ALICE, 1000e6, 0);
        vm.warp(t0 + YEAR);
        uint256 other = _credit(BOB, 500e6, 0);
        vm.warp(t0 + 2 * YEAR);
        (uint256 otherPrincipal, uint256 otherSeconds) = credit.issuanceOf(other);
        BuckRenewal.Terms memory terms = _terms(id, 1200e6, 1200e6, 0, 0);
        _approveRenewal(id, terms);
        vm.prank(ALICE);
        fund.renew(id, terms);
        (uint256 afterPrincipal, uint256 afterSeconds) = credit.issuanceOf(other);
        assertEq(afterPrincipal, otherPrincipal);
        assertEq(afterSeconds, otherSeconds);
        assertEq(buck.claimSeconds(), otherSeconds);
    }

    function test_renewalConvertsItsInKindProceedsAndClearsOldReductionConsent() public {
        (uint256 id, uint256 position, JubileeSwapPoolMock pool) = _inKindSaleFixture();
        pool.setBehavior(0, 1e6);
        (uint128 sharesBefore,) = basket.holdings(position);
        BuckRenewal.Terms memory terms = _terms(id, 120e6, 120e6, 0, 0);
        _approveRenewal(id, terms);
        vm.expectRevert(JubileeFund.MissingRoute.selector);
        vm.prank(ALICE);
        fund.renew(id, terms);
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](1);
        routes[0] = JubileeFund.SaleRoute({sqrtPriceLimitX96: 1, minimumNetBuck: 980_000});
        vm.prank(ALICE);
        (uint256 relief,,) = fund.renewWithRoutes(id, terms, routes);
        assertGt(relief, 0);
        (uint128 sharesAfter,) = basket.holdings(position);
        assertEq(sharesAfter, uint256(sharesBefore) - uint256(sharesBefore) / 2);
        assertEq(basket.inKind().balanceOf(address(fund)), 0);
        assertEq(basket.inKind().balanceOf(address(pool)), 2e6);
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(id);
        assertEq(principal, 100e6 - relief);
        assertEq(seconds_, 0);
        (uint256 oldAuthorization,,) = buck.reductionAuthorization(id);
        assertEq(oldAuthorization, 0);
    }

    function test_renewalRefundsOriginalPoolAndFundsNewPoolInFull() public {
        JubileeInsurerPoolMock original = _pool();
        uint256 id = _credit(ALICE, 1000e6, 100);
        original.invest(100e6);
        original.setReenter(fund);
        JubileeInsurerPoolMock replacement = _pool();
        vm.warp(t0 + YEAR);
        uint256 netRefund = buck.netBalanceOf(address(original.strategy()));
        uint256 beforeBalance = buck.balanceOf(ALICE);
        BuckRenewal.Terms memory terms = _terms(id, 2000e6, 2000e6, 50, 200e6);
        terms.maxPremium = 100e6;
        _approveRenewal(id, terms);
        vm.prank(ALICE);
        (, uint256 refund, uint256 premium) = fund.renew(id, terms);
        assertEq(refund, 100e6); // nominal, carrying the original pool's fee
        assertEq(premium, 100e6); // full-value replacement deposit
        assertLt(netRefund, refund);
        assertApproxEqAbs(buck.balanceOf(ALICE), beforeBalance + netRefund + 200e6 - premium, 1);
        assertEq(original.calls(), 1);
        assertEq(original.reentryError(), JubileeFund.Reentrant.selector);
        assertEq(buck.balanceOf(address(original)), 0);
        assertEq(buck.balanceOf(address(original.strategy())), 0);
        assertEq(buck.netBalanceOf(address(replacement)), 100e6);
        assertEq(buck.facilityPool(id), address(replacement));
        assertEq(buck.premiumPrincipal(id), 100e6);
    }

    function test_renewalMayImproveAnExistingKShortfall() public {
        uint256 id = _credit(ALICE, 1000e6, 0);
        vm.warp(t0 + YEAR);
        k.setK(0.5e18);
        BuckRenewal.Terms memory terms = _terms(id, 2000e6, 2000e6, 0, 100e6);
        _approveRenewal(id, terms);
        vm.prank(ALICE);
        (uint256 relief,,) = fund.renew(id, terms);
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(principal, 1100e6 - relief);
        assertLt(principal - 1000e6, 500e6);
    }

    function test_reliefIsRecordedBeforeTermsThatWouldOtherwiseOverissue() public {
        uint256 id = _credit(ALICE, 1000e6, 0);
        vm.warp(t0 + YEAR);
        BuckRenewal.Terms memory terms = _terms(id, 1000e6, 1000e6, 0, 0);
        terms.floor = 0;
        terms.depType = BuckCredit.DepreciationType.LINEAR;
        terms.depRate = 1000;
        terms.depStartAt = uint48(block.timestamp - YEAR / 10);
        _approveRenewal(id, terms);
        vm.recordLogs();
        vm.prank(ALICE);
        fund.renew(id, terms);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bytes32 renewed = keccak256("IssuanceRenewed(uint256,address,uint256,uint256,uint256,uint256)");
        bytes32 updated = keccak256("CreditUpdated(uint256,address,uint256,uint32,uint32)");
        uint256 renewedIndex = type(uint256).max;
        uint256 updatedIndex = type(uint256).max;
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter != address(credit) || logs[i].topics.length == 0) continue;
            if (logs[i].topics[0] == renewed) renewedIndex = i;
            if (logs[i].topics[0] == updated) updatedIndex = i;
        }
        assertLt(renewedIndex, updatedIndex);
        (uint256 principal,) = credit.issuanceOf(id);
        assertLt(principal, credit.currentValue(id));
        assertGt(1000e6, credit.currentValue(id));
    }

    function test_fiftyYearRenewalUsesOnlyAvailableFullValueFundCash() public {
        uint256 id = _credit(ALICE, 1000e6, 0);
        vm.warp(t0 + 50 * YEAR);
        buck.processMaturities(1);
        (, uint256 cash) = buck.fundBalancesScaled();
        BuckRenewal.Terms memory terms = _terms(id, 1000e6, 1000e6, 0, 0);
        _approveRenewal(id, terms);
        vm.prank(ALICE);
        (uint256 relief,,) = fund.renew(id, terms);
        assertEq(relief, cash / Q);
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(id);
        assertEq(principal, 1000e6 - relief);
        assertEq(seconds_, 0);
        assertEq(buck.totalIssued(), principal);
    }

    function test_fundingFactorIsCheckedBeforeRenewalIssuance() public {
        _pool();
        uint256 id = _credit(ALICE, 1000e6, 0);
        vm.prank(ALICE);
        buck.transfer(BOB, 999e6);
        k.setFactor(1e18);
        BuckRenewal.Terms memory terms = _terms(id, 2000e6, 2000e6, 100, 500e6);
        _approveRenewal(id, terms);
        vm.expectRevert(BuckNet.FundingInsufficient.selector);
        vm.prank(ALICE);
        fund.renew(id, terms);
        (uint256 face, uint256 active,) = credit.creditInfo(id);
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(face, 1000e6);
        assertEq(active, 1000e6);
        assertEq(principal, 1000e6);
        assertEq(buck.balanceOf(ALICE), 1e6);
    }

    function testFuzz_renewalNeverWorsensTheExistingKShortfall(
        uint64 principal_, uint64 additional_, uint16 kBp_, uint8 years_
    ) public {
        uint256 original = bound(principal_, 100e6, 1e12);
        uint256 additional = bound(additional_, 0, original);
        uint256 factor = bound(kBp_, 1, 10_000) * 1e14;
        uint256 elapsed = bound(years_, 1, 49) * YEAR;
        uint256 id = _credit(ALICE, original, 0);
        vm.warp(t0 + elapsed);
        k.setK(factor);
        (, uint256 cash) = buck.fundBalancesScaled();
        uint256 relief = cash / Q;
        uint256 newPrincipal = original - relief + additional;
        uint256 oldLimit = original * factor / 1e18;
        uint256 newLimit = 2 * original * factor / 1e18;
        uint256 oldShortfall = original > oldLimit ? original - oldLimit : 0;
        uint256 newShortfall = newPrincipal > newLimit ? newPrincipal - newLimit : 0;
        BuckRenewal.Terms memory terms = _terms(id, 2 * original, 2 * original, 0, additional);
        _approveRenewal(id, terms);
        if (newShortfall > oldShortfall) vm.expectRevert(BuckNet.CreditExceeded.selector);
        vm.prank(ALICE);
        fund.renew(id, terms);
        (uint256 recorded, uint256 seconds_) = credit.issuanceOf(id);
        if (newShortfall > oldShortfall) {
            assertEq(recorded, original);
            assertEq(buck.totalIssued(), original);
        } else {
            assertEq(recorded, newPrincipal);
            assertEq(seconds_, 0);
            assertEq(buck.totalIssued(), newPrincipal);
        }
    }

    function test_worsenedKShortfallRollsBackTheSaleAndApprovals() public {
        uint256 id = _credit(ALICE, 1000e6, 0);
        vm.warp(t0 + YEAR);
        (uint256 position,,) = _invest(1e6);
        (uint128 beforeShares,) = basket.holdings(position);
        (, uint256 beforeCash) = buck.fundBalancesScaled();
        k.setK(0.5e18);
        BuckRenewal.Terms memory terms = _terms(id, 1000e6, 1000e6, 0, 100e6);
        bytes32 digest = _approveRenewal(id, terms);
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        vm.prank(ALICE);
        fund.renew(id, terms);
        (uint128 afterShares,) = basket.holdings(position);
        (, uint256 afterCash) = buck.fundBalancesScaled();
        assertEq(afterShares, beforeShares);
        assertEq(afterCash, beforeCash);
        assertEq(buck.renewalConsent().proposal(id), digest);
        assertEq(buck.renewalConsent().authorization(id), digest);
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(principal, 1000e6);
    }

    function test_insurerLiquidityFailureRollsBackTheRenewalSale() public {
        JubileeInsurerPoolMock original = _pool();
        uint256 id = _credit(ALICE, 1000e6, 100);
        original.invest(100e6);
        original.setFail(true);
        vm.warp(t0 + YEAR);
        (uint256 position,,) = _invest(1e6);
        (uint128 beforeShares,) = basket.holdings(position);
        BuckRenewal.Terms memory terms = _terms(id, 1200e6, 1200e6, 50, 0);
        bytes32 digest = _approveRenewal(id, terms);
        vm.expectRevert(JubileeFund.InsufficientPoolLiquidity.selector);
        vm.prank(ALICE);
        fund.renew(id, terms);
        (uint128 afterShares,) = basket.holdings(position);
        assertEq(afterShares, beforeShares);
        assertEq(buck.renewalConsent().proposal(id), digest);
        assertEq(buck.premiumPrincipal(id), 100e6);
        assertEq(original.calls(), 0);
    }

    function test_bothApprovalsAreRequiredAndEitherCanCancel() public {
        uint256 id = _credit(ALICE, 1000e6, 0);
        BuckRenewal.Terms memory terms = _terms(id, 1200e6, 1200e6, 0, 0);
        BuckRenewalConsent consent = buck.renewalConsent();
        bytes32 digest = consent.renewalDigest(ALICE, id, terms);
        vm.expectRevert(BuckRenewalConsent.NotBuck.selector);
        consent.consume(id, digest);
        vm.prank(INSURER);
        consent.propose(id, digest);
        vm.expectRevert(BuckRenewalConsent.BadAuthorization.selector);
        vm.prank(ALICE);
        fund.renew(id, terms);
        vm.prank(ALICE);
        consent.authorize(id, digest);
        vm.prank(INSURER);
        consent.propose(id, bytes32(0));
        vm.expectRevert(BuckRenewalConsent.BadAuthorization.selector);
        vm.prank(ALICE);
        fund.renew(id, terms);
        vm.prank(INSURER);
        consent.propose(id, digest);
        vm.prank(ALICE);
        consent.authorize(id, bytes32(0));
        vm.expectRevert(BuckRenewalConsent.BadAuthorization.selector);
        vm.prank(ALICE);
        fund.renew(id, terms);
    }

    function test_expiredAndWrongOwnerRenewalsCannotExecute() public {
        uint256 id = _credit(ALICE, 1000e6, 0);
        BuckRenewal.Terms memory terms = _terms(id, 1200e6, 1200e6, 0, 0);
        _approveRenewal(id, terms);
        vm.expectRevert(BuckRenewalConsent.NotOwner.selector);
        vm.prank(BOB);
        fund.renew(id, terms);
        vm.warp(uint256(terms.deadline) + 1);
        vm.expectRevert(BuckRenewalConsent.BadAuthorization.selector);
        vm.prank(ALICE);
        fund.renew(id, terms);
    }

    function test_additionalIssuanceMakesAnOldProposalStale() public {
        vm.prank(INSURER);
        uint256 id = credit.createCredit(ALICE, 0, 1000e6, 1000e6,
                                         BuckCredit.DepreciationType.NONE, 0, 0, 0);
        vm.prank(ALICE);
        buck.issue(id, 1000e6, 500e6);
        BuckRenewal.Terms memory terms = _terms(id, 1200e6, 1200e6, 0, 0);
        _approveRenewal(id, terms);
        vm.prank(ALICE);
        buck.issue(id, 0, 1);
        vm.expectRevert(BuckRenewalConsent.BadCreditSnapshot.selector);
        vm.prank(ALICE);
        fund.renew(id, terms);
    }

    function test_renewalRejectsForgedConsentAndWrongCreditSnapshot() public {
        uint256 id = _credit(ALICE, 1000e6, 0);
        BuckRenewal.Terms memory terms = _terms(id, 1200e6, 1200e6, 0, 0);
        BuckRenewalConsent consent = buck.renewalConsent();
        bytes32 digest = consent.renewalDigest(ALICE, id, terms);
        vm.expectRevert(BuckRenewalConsent.NotInsurer.selector);
        vm.prank(BOB);
        consent.propose(id, digest);
        vm.expectRevert(BuckRenewalConsent.NotOwner.selector);
        vm.prank(BOB);
        consent.authorize(id, digest);
        vm.expectRevert(BuckNet.NotJubilee.selector);
        buck.renewFromJubilee(ALICE, id, terms, 0);
        terms.expectedCreditHash = bytes32(uint256(1));
        _approveRenewal(id, terms);
        vm.expectRevert(BuckRenewalConsent.BadCreditSnapshot.selector);
        vm.prank(ALICE);
        fund.renew(id, terms);
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(principal, 1000e6);
    }

    function test_reliefAndPremiumLimitsRevertAtomically() public {
        _pool();
        uint256 id = _credit(ALICE, 1000e6, 0);
        vm.warp(t0 + YEAR);
        BuckRenewal.Terms memory terms = _terms(id, 2000e6, 2000e6, 100, 0);
        terms.minimumRelief = 1000e6;
        _approveRenewal(id, terms);
        vm.expectRevert(JubileeFund.BadAuthorization.selector);
        vm.prank(ALICE);
        fund.renew(id, terms);
        terms.minimumRelief = 0;
        terms.maxPremium = 1;
        _approveRenewal(id, terms);
        vm.expectRevert(BuckNet.BadAuthorization.selector);
        vm.prank(ALICE);
        fund.renew(id, terms);
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(principal, 1000e6);
    }
}
