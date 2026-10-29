// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
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

/// @dev Exercise the real token and coordinator with a controllable basket;
///      this is not a substitute for integration with the real equity basket.
contract JubileeInKindToken is ERC20 {
    constructor() ERC20("In-kind TOKEN", "TOK") {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

contract JubileeBasketMock {
    uint256 constant Q = 1e36;
    address public immutable buck;
    BuckBasketReceipt public immutable receipt;
    JubileeInKindToken public immutable inKind;
    struct Holding { uint128 shares; uint128 basis; }
    mapping(uint256 => Holding) public holdings;
    uint8 public mode; // 0 normal, 1 underwater, 2 partial, 3 zero proceeds, 4 unexpected revert
    error Underwater();
    error Unexpected();

    constructor(address token) {
        buck = token;
        receipt = new BuckBasketReceipt(address(this));
        inKind = new JubileeInKindToken();
    }
    function setMode(uint8 value) external { mode = value; }
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
        }
        h.shares -= uint128(retired);
        h.basis -= uint128(uint256(h.basis) * retired / beforeShares);
        remaining = h.shares;
        if (remaining == 0) { receipt.burn(id); newId = 0; } else newId = id;
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

    function test_noAgeRequiresNoSaleOrRelief() public {
        uint256 id = _credit(ALICE, 100e6, 0);
        _authorize(ALICE, id, 100e6, 0);
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduce(id, 100e6);
        assertEq(relief, 0);
        assertEq(buck.totalIssued(), 0);
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

    function test_zeroProceedsRetiresOnlyTheSoldShares() public {
        uint256 a = _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(2e6);
        basket.inKind().mint(address(basket), 4e6);
        basket.setMode(3);
        (, uint256 cash) = buck.fundBalancesScaled();
        (uint128 oldShares,) = basket.holdings(id);
        _authorize(ALICE, a, 50e6, 0);
        vm.prank(ALICE);
        (uint256 relief,) = fund.reduce(a, 50e6);
        assertEq(relief, cash / 4);
        assertEq(basket.inKind().balanceOf(address(fund)), 1e6);
        (uint128 left,) = basket.holdings(id);
        assertEq(left, uint256(oldShares) - uint256(oldShares) / 4);
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
    }

    function test_issuanceCheckpointDoesNotBackdateFreshlyObservedExcess() public {
        _credit(ALICE, 1000e6, 0);
        assertTrue(buck.jubileeReserveHookEnabled());
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
        vm.expectRevert(BuckNet.NotJubilee.selector);
        buck.enableJubileeReserveHook();
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
