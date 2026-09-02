// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test, Vm} from "forge-std/Test.sol";
import {IERC20}   from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {BuckBasketProRata}   from "../../src/basket/BuckBasketProRata.sol";
import {BuckBasketUniswapV3} from "../../src/basket/BuckBasketUniswapV3.sol";
import {BuckBasketReceipt}   from "../../src/basket/BuckBasketReceipt.sol";
import {BuckBasketStorage}   from "../../src/basket/BuckBasketStorage.sol";
import {IBuckBasketVenue}    from "../../src/basket/IBuckBasketVenue.sol";
import {MockBuck, MockController, BBToken, IV3Pool} from "./BuckBasketProRata.t.sol";

/// @title WP-5: the stress fee on duress exits (CARRY-CONVEXITY D2).
///
/// A redemption that lands in the deflation branch (Bw < R, BUCK dear) while
/// the TWAP deviation below par exceeds the deadband pays a fee proportional
/// to the deviation; the fee is re-LP'd as depositor liquidity so it accrues
/// to the remaining holders, never to the treasury.
///
/// Manipulation resistance: the deviation is the TWAP-based
/// `basketValueInBuck()`.  Uniswap V3 writes an observation carrying the
/// PRE-swap tick at the first tick-moving swap of a block, and every later
/// read in that block resolves to that observation -- so a flash sandwich
/// cannot push a redeemer into the fee (or out of it) within one block; see
/// `test_twapDeviation_immuneToSameBlockSandwich`.
contract BuckBasketStressFeeTest is Test {

    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO =
        1461446703485210103287273052203988822378723970342;

    bytes32 internal constant STRESS_FEE_SIG =
        keccak256("StressFee(uint256,uint256,uint256,uint256)");

    event StressFee(uint256 indexed receiptId, uint256 deviation1e18, uint256 feeBp, uint256 feeValueBuck);
    event StressFeeSet(uint256 deadbandBp, uint256 slopeBp, uint256 maxBp);

    address constant GOV = address(0xA0);

    MockBuck            internal buck;
    MockController      internal ctrl;
    BuckBasketProRata   internal basketC;
    BuckBasketUniswapV3 internal venueFacet;
    BuckBasketReceipt   internal receipt;
    address             internal v3Factory;

    BBToken internal paxg;    // 18-dec
    BBToken internal cbbtc;   // 8-dec

    address internal alice = address(0xA11CE);
    address internal bob   = address(0xB0B);
    address internal carol = address(0xCA201);

    uint256 constant PAXG_PRICE  = 4000e18;     // 1 PAXG = 4000 BUCK
    uint256 constant CBBTC_PRICE = 100000e18;   // 1 cbBTC = 100000 BUCK
    uint256 constant DEP         = 10e18;       // 10 PAXG = 40,000 BUCK per depositor

    function setUp() public {
        buck = new MockBuck();
        ctrl = new MockController();
        v3Factory = deployCode("out/UniswapV3Factory.sol/UniswapV3Factory.json");

        basketC = new BuckBasketProRata(
            address(buck), address(ctrl), v3Factory, GOV,
            500, 600, 64,
            500,    // 5% spot/TWAP manipulation guard
            1e3);
        venueFacet = new BuckBasketUniswapV3();
        vm.prank(GOV);
        basketC.setVenue(address(venueFacet));

        buck.setBasket(address(basketC));
        receipt = basketC.receipt();

        paxg  = new BBToken("PAX Gold", "PAXG",  18);
        cbbtc = new BBToken("cbBTC",    "cbBTC",  8);

        paxg.mint(alice, 1_000e18);
        paxg.mint(bob,   1_000e18);
        paxg.mint(carol, 1_000e18);
        cbbtc.mint(alice, 1_000e8);
        cbbtc.mint(bob,   1_000e8);

        // The test contract is the external arb on the pools.
        paxg.mint(address(this), 1_000_000e18);
        cbbtc.mint(address(this), 1_000_000e8);
        buck.mint(address(this), 1_000_000_000e18);
    }

    // ---- helpers --------------------------------------------------------- //

    function _addPaxg() internal returns (address pool) {
        vm.prank(GOV);
        pool = basketC.addBasketToken(address(paxg), 18, PAXG_PRICE, 0, 500);
    }

    function _addCbbtc() internal returns (address pool) {
        vm.prank(GOV);
        pool = basketC.addBasketToken(address(cbbtc), 8, CBBTC_PRICE, 0, 500);
    }

    function _depositPaxg(address who, uint256 amt) internal returns (uint256 rid) {
        vm.prank(who); paxg.approve(address(basketC), amt);
        vm.prank(who); rid = basketC.depositToken(address(paxg), amt, 0);
    }

    function _depositCbbtc(address who, uint256 amt) internal returns (uint256 rid) {
        vm.prank(who); cbbtc.approve(address(basketC), amt);
        vm.prank(who); rid = basketC.depositToken(address(cbbtc), amt, 0);
    }

    /// @dev tokenIn = BUCK buys TOKEN (pool BUCK-heavy, inflation);
    ///      tokenIn = TOKEN sells TOKEN (pool BUCK-light, deflation).
    function _arb(address pool, address tokenIn, uint256 amountIn) internal {
        bool zeroForOne = IV3Pool(pool).token0() == tokenIn;
        uint160 limit = zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1;
        IV3Pool(pool).swap(address(this), zeroForOne, int256(amountIn), limit, "");
    }

    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata) external {
        if (a0 > 0) IERC20(IV3Pool(msg.sender).token0()).transfer(msg.sender, uint256(a0));
        if (a1 > 0) IERC20(IV3Pool(msg.sender).token1()).transfer(msg.sender, uint256(a1));
    }

    /// @dev Let the TWAP catch up with spot (window is 600 s).
    function _warmTwap() internal {
        vm.warp(block.timestamp + 700);
    }

    /// @dev Move the pool's price and let the TWAP converge to it, so the
    ///      measured deviation is a settled one (not a same-block flash).
    function _settle(address pool, address tokenIn, uint256 amountIn) internal {
        _arb(pool, tokenIn, amountIn);
        _warmTwap();
    }

    function _stressFeeLogs(Vm.Log[] memory logs) internal pure returns (uint256 n) {
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].topics[0] == STRESS_FEE_SIG) n++;
        }
    }

    function _lastStressFee(Vm.Log[] memory logs)
        internal pure returns (uint256 dev, uint256 feeBp, uint256 feeValue)
    {
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].topics[0] == STRESS_FEE_SIG) {
                (dev, feeBp, feeValue) = abi.decode(logs[i].data, (uint256, uint256, uint256));
            }
        }
    }

    /// @dev The value claim V = theta*NAV a full redemption of `rid` carries
    ///      right now, by the same arithmetic `_allocate*` uses.
    function _claimValue(uint256 rid) internal view returns (uint256 V) {
        (uint256 principal,,,) = basketC.deposits(rid);
        (, , uint256 B, ) = IBuckBasketVenue(address(basketC)).poolBuckValues();
        V = principal * 2 * B / basketC.totalOutstandingBuck();
    }

    function _expectedFeeBp(uint256 dev) internal view returns (uint256 feeBp) {
        uint256 band = basketC.stressFeeDeadbandBp() * 1e14;
        if (dev <= band) return 0;
        feeBp = (dev - band) * basketC.stressFeeSlopeBp() / 1e16;
        uint256 cap = basketC.stressFeeMaxBp();
        if (feeBp > cap) feeBp = cap;
    }

    // ---- (a) no fee at par / inside the deadband ------------------------- //

    function test_noFee_atPar() public {
        _addPaxg();
        uint256 ridA = _depositPaxg(alice, DEP);
        _depositPaxg(bob, DEP);
        _warmTwap();

        (uint256 dev, uint256 feeBp) = basketC.stressFeeQuote();
        assertLt(dev, 1e15, "at par: deviation is tick rounding only");
        assertEq(feeBp, 0, "at par: no fee");

        vm.recordLogs();
        vm.prank(alice);
        basketC.redeem(ridA, 0, 0);
        assertEq(_stressFeeLogs(vm.getRecordedLogs()), 0, "no StressFee event");
        assertEq(basketC.stressBonusPrincipal(), 0, "no bonus principal");
    }

    function test_noFee_insideDeadband() public {
        address pool = _addPaxg();
        uint256 ridA = _depositPaxg(alice, DEP);
        _depositPaxg(bob, DEP);
        _settle(pool, address(paxg), 0.1e18);      // ~ -1%: BUCK dear, but inside 2%

        (uint256 dev, uint256 feeBp) = basketC.stressFeeQuote();
        assertGt(dev, 0.005e18, "a measured deviation");
        assertLt(dev, 0.02e18,  "inside the deadband");
        assertEq(feeBp, 0, "no fee inside the deadband");

        uint256 outBefore = basketC.totalOutstandingBuck();
        (uint256 principalA,,,) = basketC.deposits(ridA);
        vm.recordLogs();
        vm.prank(alice);
        basketC.redeem(ridA, 0, 0);                // deflation branch (Bw < R), no fee
        assertEq(_stressFeeLogs(vm.getRecordedLogs()), 0, "no StressFee event");
        assertEq(basketC.stressBonusPrincipal(), 0, "no bonus principal");
        assertEq(basketC.totalOutstandingBuck(), outBefore - principalA,
                 "outstanding drops by exactly the principal");
    }

    // ---- (b) no fee in the inflation branch ------------------------------ //

    function test_noFee_inflationBranch_farFromPar() public {
        address pool = _addPaxg();
        uint256 ridA = _depositPaxg(alice, DEP);
        _depositPaxg(bob, DEP);
        _settle(pool, address(buck), 20_000e18);   // BUCK cheap: ~ +56% on PAXG

        (uint256 dev, uint256 feeBp) = basketC.stressFeeQuote();
        assertEq(dev, 0,   "above par: deviation is 0");
        assertEq(feeBp, 0, "above par: no fee");

        // Byte-for-byte: the fee-disabled run and the default run agree.
        uint256 snap = vm.snapshotState();
        vm.prank(GOV); basketC.setStressFee(0, 0, 0);
        vm.prank(alice); basketC.redeem(ridA, 0, 0);
        uint256 paxgOff  = paxg.balanceOf(alice);
        uint256 treasOff = basketC.treasuryBuckPending();
        vm.revertToState(snap);

        vm.recordLogs();
        vm.prank(alice); basketC.redeem(ridA, 0, 0);
        assertEq(_stressFeeLogs(vm.getRecordedLogs()), 0, "no StressFee event");
        assertEq(paxg.balanceOf(alice), paxgOff, "payout identical with the fee armed");
        assertEq(basketC.treasuryBuckPending(), treasOff, "treasury identical");
        assertGt(treasOff, 0, "inflation profit went to the treasury");
        assertEq(basketC.stressBonusPrincipal(), 0, "no bonus principal");
    }

    // ---- (c) fee in the deflation branch: proportional, capped ----------- //

    function test_fee_deflation_proportionalToDeviation() public {
        address pool = _addPaxg();
        uint256 ridA = _depositPaxg(alice, DEP);
        _depositPaxg(bob, DEP);
        uint256 snap = vm.snapshotState();

        // Mild duress: ~ -4.8% -> 2.8% beyond the deadband -> ~140 bp.
        _settle(pool, address(paxg), 0.5e18);
        (uint256 dev1, uint256 q1) = basketC.stressFeeQuote();
        assertGt(dev1, 0.02e18, "beyond the deadband");
        assertEq(q1, _expectedFeeBp(dev1), "quote follows the slope");
        assertGt(q1, 0); assertLt(q1, basketC.stressFeeMaxBp(), "below the cap");

        uint256 V1 = _claimValue(ridA);
        vm.recordLogs();
        vm.expectEmit(true, false, false, false);
        emit StressFee(ridA, 0, 0, 0);
        vm.prank(alice);
        basketC.redeem(ridA, 0, 0);
        (uint256 evDev, uint256 evBp, uint256 evVal) = _lastStressFee(vm.getRecordedLogs());
        assertEq(evDev, dev1, "event carries the TWAP deviation");
        assertEq(evBp, q1, "event carries the quoted bp");
        assertApproxEqRel(evVal, V1 * q1 / 10000, 0.02e18, "fee value = feeBp of V");
        // The partner BUCK LP'd against the fee TOKEN is the bonus principal,
        // and it is outstanding.  It is struck at the post-settlement spot
        // (the cover swap sold TOKEN, so spot is a little lower than the
        // guarded pre-redeem spot the event values the fee at): at most the
        // fee value, and within the settlement's own price impact of it.
        uint256 S1 = basketC.stressBonusPrincipal();
        assertLe(S1, evVal, "bonus principal <= fee value at the pre-settlement spot");
        assertApproxEqRel(S1, evVal, 0.1e18, "bonus principal ~ fee value");
        (uint256 principalB,,,) = basketC.deposits(2);
        assertEq(basketC.totalOutstandingBuck(), principalB + S1,
                 "outstanding == sum principal + bonus");
        assertEq(basketC.treasuryBuckPending(), 0, "nothing of it to the treasury");

        // Deeper duress: ~ -9.3% -> a larger bp, linear in the excess.
        vm.revertToState(snap);
        _settle(pool, address(paxg), 1e18);
        (uint256 dev2, uint256 q2) = basketC.stressFeeQuote();
        assertGt(dev2, dev1, "deeper deviation");
        assertEq(q2, _expectedFeeBp(dev2), "quote follows the slope");
        assertGt(q2, q1, "fee grows with the deviation");
        uint256 band = basketC.stressFeeDeadbandBp() * 1e14;
        assertApproxEqRel(q2 * (dev1 - band), q1 * (dev2 - band), 0.01e18,
                          "proportional to the excess beyond the deadband");

        vm.recordLogs();
        vm.prank(alice);
        basketC.redeem(ridA, 0, 0);
        (, uint256 evBp2,) = _lastStressFee(vm.getRecordedLogs());
        assertEq(evBp2, q2, "charged at the quoted bp");
    }

    function test_fee_cappedAtMax() public {
        address pool = _addPaxg();
        uint256 ridA = _depositPaxg(alice, DEP);
        _depositPaxg(bob, DEP);
        _settle(pool, address(paxg), 3e18);        // ~ -24%: far past the cap

        (uint256 dev, uint256 feeBp) = basketC.stressFeeQuote();
        assertGt((dev - 0.02e18) * 50 / 1e16, 500, "uncapped slope would exceed the cap");
        assertEq(feeBp, 500, "capped at stressFeeMaxBp");

        vm.recordLogs();
        vm.prank(alice);
        basketC.redeem(ridA, 0, 0);
        (, uint256 evBp,) = _lastStressFee(vm.getRecordedLogs());
        assertEq(evBp, 500, "charged the cap");
    }

    // ---- (d) the fee accrues to the remaining depositors ----------------- //

    /// The A/B: alice exits under duress with the fee off (A) and on (B); bob
    /// then exits with the fee OFF in both runs (so his own fee does not
    /// confound the comparison).  His payout in B exceeds A by ~his share of
    /// alice's fee, and the treasury's accrual from alice's exit is identical.
    function test_fee_accruesToRemaining_treasuryUnchanged() public {
        address pool = _addPaxg();
        uint256 ridA = _depositPaxg(alice, DEP);
        uint256 ridB = _depositPaxg(bob,   DEP);
        _depositPaxg(carol, DEP);
        _settle(pool, address(paxg), 1e18);        // ~ -9.3%
        uint256 snap = vm.snapshotState();

        // A: no fee anywhere.
        vm.prank(GOV); basketC.setStressFee(0, 0, 0);
        vm.prank(alice); basketC.redeem(ridA, 0, 0);
        uint256 treasA = basketC.treasuryBuckPending();
        _warmTwap();
        uint256 bobBeforeA = paxg.balanceOf(bob);
        vm.prank(bob); basketC.redeem(ridB, 0, 0);
        uint256 bobGotA = paxg.balanceOf(bob) - bobBeforeA;
        vm.revertToState(snap);

        // B: alice pays the fee; bob exits with it switched off.
        vm.recordLogs();
        vm.prank(alice); basketC.redeem(ridA, 0, 0);
        (,, uint256 feeVal) = _lastStressFee(vm.getRecordedLogs());
        assertGt(feeVal, 0, "alice paid");
        uint256 treasB = basketC.treasuryBuckPending();
        assertEq(treasB, treasA, "treasury accrual unchanged by the fee");
        assertEq(basketC.treasuryLiquidityOf(0), 0, "no treasury liquidity from the fee");
        vm.prank(GOV); basketC.setStressFee(0, 0, 0);
        _warmTwap();
        uint256 bobBeforeB = paxg.balanceOf(bob);
        vm.prank(bob); basketC.redeem(ridB, 0, 0);
        uint256 bobGotB = paxg.balanceOf(bob) - bobBeforeB;

        assertGt(bobGotB, bobGotA, "a remaining receipt is worth more after the fee");
        // Bob and carol split the fee by principal (1/2 each); bob's gain is
        // ~half the fee's TOKEN, net of the slightly larger burn he retires.
        uint256 spot = _spotPaxg(pool);
        uint256 gainBuck = (bobGotB - bobGotA) * spot / 1e18;
        assertGt(gainBuck, feeVal * 3 / 10, "bob's gain is a real share of the fee");
        assertLt(gainBuck, feeVal * 7 / 10, "...and not more than his share");
    }

    function _spotPaxg(address pool) internal view returns (uint256) {
        // BUCK per PAXG from the pool balances (full-range => balances are reserves).
        return buck.balanceOf(pool) * 1e18 / paxg.balanceOf(pool);
    }

    // ---- (e) the burn invariant survives the bonus principal ------------- //

    function test_burnInvariant_bonusRetiredByLastRedeemers() public {
        address pool = _addPaxg();
        uint256 ridA = _depositPaxg(alice, DEP);
        uint256 ridB = _depositPaxg(bob,   DEP);
        uint256 ridC = _depositPaxg(carol, DEP);
        _settle(pool, address(paxg), 1e18);

        vm.prank(alice); basketC.redeem(ridA, 0, 0);     // duress exit: bonus minted
        uint256 S = basketC.stressBonusPrincipal();
        assertGt(S, 0, "bonus principal outstanding");
        (uint256 pB,,,) = basketC.deposits(ridB);
        (uint256 pC,,,) = basketC.deposits(ridC);
        assertEq(basketC.totalOutstandingBuck(), pB + pC + S, "invariant after the fee");

        // Bob leaves under a mild inflation (Bw >= Rb, no conversion) and
        // retires his pro-rata slice of the bonus: exactly half of it.
        _settle(pool, address(buck), 20_000e18);
        vm.prank(bob); basketC.redeem(ridB, 0, 0);
        assertApproxEqAbs(basketC.stressBonusPrincipal(), S - S * pB / (pB + pC), 1,
                          "bob retired his share of the bonus");
        assertEq(basketC.totalOutstandingBuck(), pC + basketC.stressBonusPrincipal(),
                 "invariant after bob");

        // Carol is last: she retires the rest.  Nothing is left outstanding
        // and the basket holds no BUCK beyond the treasury's accrual (dust:
        // the +1 wei each LP mint over-mints, V3 burn rounding).
        vm.prank(carol); basketC.redeem(ridC, 0, 0);
        assertEq(basketC.totalOutstandingBuck(), 0, "everything minted was burned");
        assertEq(basketC.stressBonusPrincipal(), 0, "bonus fully retired");
        uint256 orphan = buck.balanceOf(address(basketC)) - basketC.treasuryBuckPending();
        assertLe(orphan, 1e9, "no orphaned BUCK beyond treasury");
        vm.expectRevert(); receipt.ownerOf(ridC);
    }

    // ---- (f) single-token mode pays the fee too -------------------------- //

    function test_fee_singleTokenMode() public {
        address pool = _addPaxg();
        _addCbbtc();
        uint256 ridA = _depositPaxg(alice, DEP);
        _depositPaxg(bob, DEP);
        _depositCbbtc(bob, 1e8);                    // a second pool, untouched below
        _settle(pool, address(paxg), 1e18);         // PAXG pool deflated

        (, uint256 q) = basketC.stressFeeQuote();
        assertGt(q, 0, "duress");
        uint256 cbbtcBefore = cbbtc.balanceOf(alice);

        vm.recordLogs();
        vm.expectEmit(true, false, false, false);
        emit StressFee(ridA, 0, 0, 0);
        vm.prank(alice);
        basketC.redeem(ridA, 0, address(paxg), 0);  // single-TOKEN, within-pool cover
        (, uint256 evBp, uint256 evVal) = _lastStressFee(vm.getRecordedLogs());
        assertEq(evBp, q, "charged at the quoted bp");
        assertGt(evVal, 0, "fee taken from the single payout token");
        assertGt(basketC.stressBonusPrincipal(), 0, "re-LP'd as bonus principal");
        assertEq(cbbtc.balanceOf(alice), cbbtcBefore, "other pool untouched");
    }

    // ---- balanced multi-pool: the fee is pro rata across the payout ------- //

    function test_fee_balanced_proRataAcrossPools() public {
        address pPaxg  = _addPaxg();
        address pCbbtc = _addCbbtc();
        uint256 ridA = _depositPaxg(alice, DEP);
        _depositCbbtc(alice, 0.4e8);                // ~40,000 BUCK, equal weight
        _depositPaxg(bob, DEP);
        _depositCbbtc(bob, 0.4e8);
        // Deflate both pools by a similar fraction so the balanced draw
        // touches both.
        _arb(pPaxg,  address(paxg),  1e18);
        _arb(pCbbtc, address(cbbtc), 0.04e8);
        _warmTwap();
        (, uint256 q) = basketC.stressFeeQuote();
        assertGt(q, 0, "duress");
        uint256 snap = vm.snapshotState();

        // A: fee off.  Alice's balanced draw takes liquidity from both pools.
        vm.prank(GOV); basketC.setStressFee(0, 0, 0);
        vm.prank(alice); basketC.redeem(ridA, 0, 0);
        _warmTwap();                                // the cover swap moved spot
        uint128 lPaxgA  = _depositorL(0);
        uint128 lCbbtcA = _depositorL(1);
        vm.revertToState(snap);

        // B: fee on.  A slice of EACH pool's payout is re-LP'd back into its
        // own pool as depositor liquidity, so both pools end with more
        // depositor L than the fee-free draw left.
        vm.recordLogs();
        vm.prank(alice); basketC.redeem(ridA, 0, 0);
        assertEq(_stressFeeLogs(vm.getRecordedLogs()), 1, "one StressFee event");
        assertGt(basketC.stressBonusPrincipal(), 0, "bonus principal");
        _warmTwap();
        assertGt(_depositorL(0), lPaxgA,  "PAXG pool credited with its fee leg");
        assertGt(_depositorL(1), lCbbtcA, "cbBTC pool credited with its fee leg");
    }

    function _depositorL(uint256 i) internal view returns (uint128) {
        (, uint128[] memory depL, , ) = IBuckBasketVenue(address(basketC)).poolBuckValues();
        return depL[i];
    }

    // ---- (g) governance ---------------------------------------------------- //

    function test_setStressFee_governanceAndEvent() public {
        assertEq(basketC.stressFeeDeadbandBp(), 200, "default deadband 2%");
        assertEq(basketC.stressFeeSlopeBp(),    50,  "default slope 50 bp / 1%");
        assertEq(basketC.stressFeeMaxBp(),      500, "default cap 5%");

        vm.prank(alice);
        vm.expectRevert(BuckBasketStorage.NotGovernance.selector);
        basketC.setStressFee(100, 25, 300);

        vm.prank(GOV);
        vm.expectRevert(BuckBasketStorage.Bp10000.selector);
        basketC.setStressFee(100, 25, 10001);

        vm.expectEmit(false, false, false, true);
        emit StressFeeSet(100, 25, 300);
        vm.prank(GOV);
        basketC.setStressFee(100, 25, 300);
        assertEq(basketC.stressFeeDeadbandBp(), 100);
        assertEq(basketC.stressFeeSlopeBp(),    25);
        assertEq(basketC.stressFeeMaxBp(),      300);
    }

    function test_setStressFee_zeroMaxDisables() public {
        address pool = _addPaxg();
        uint256 ridA = _depositPaxg(alice, DEP);
        _depositPaxg(bob, DEP);
        _settle(pool, address(paxg), 1e18);
        vm.prank(GOV); basketC.setStressFee(200, 50, 0);
        (, uint256 q) = basketC.stressFeeQuote();
        assertEq(q, 0, "disabled");
        vm.recordLogs();
        vm.prank(alice); basketC.redeem(ridA, 0, 0);
        assertEq(_stressFeeLogs(vm.getRecordedLogs()), 0, "no fee");
        assertEq(basketC.stressBonusPrincipal(), 0);
    }

    // ---- manipulation resistance ------------------------------------------ //

    /// The deviation is the TWAP `basketValueInBuck()`.  A same-block spot
    /// move -- a flash sandwich around the redemption, inside the 5% spot/TWAP
    /// guard -- leaves the quote untouched: (1) at a settled par, pushing spot
    /// down 3% puts the redemption in the deflation branch but charges no fee;
    /// (2) at a settled -9.3%, buying spot back 2.6% in the same block neither
    /// lowers the quoted bp nor the bp actually charged.
    ///
    /// Residual surface, bounded by the same guard: the BRANCH is decided on
    /// spot (Bw < Rb), so a same-block buy large enough to lift Bw to Rb
    /// escapes the fee -- possible only while the shortfall is under ~2.5%
    /// of the claim's BUCK half (a 5% price move shifts a CPMM's reserve by
    /// ~2.5%), i.e. only in the shallow end where the fee is small.
    function test_twapDeviation_immuneToSameBlockSandwich() public {
        address pool = _addPaxg();
        uint256 ridA = _depositPaxg(alice, DEP);
        _depositPaxg(bob, DEP);
        _warmTwap();                                  // settled at par
        uint256 snap = vm.snapshotState();

        // (1) Push INTO the fee?  Spot -3% in the redeem block: no.
        _arb(pool, address(paxg), 0.3e18);
        (uint256 dev, uint256 q) = basketC.stressFeeQuote();
        assertLt(dev, 1e15, "TWAP still at par after the same-block sell");
        assertEq(q, 0, "no fee quoted");
        vm.recordLogs();
        vm.prank(alice); basketC.redeem(ridA, 0, 0);  // deflation branch on spot
        assertEq(_stressFeeLogs(vm.getRecordedLogs()), 0, "no fee charged");
        assertEq(basketC.stressBonusPrincipal(), 0);

        // (2) Push OUT of the fee?  Settled duress, then a same-block buy-back.
        vm.revertToState(snap);
        _settle(pool, address(paxg), 1e18);
        (uint256 devSettled, uint256 qSettled) = basketC.stressFeeQuote();
        assertGt(qSettled, 0, "duress");
        _arb(pool, address(buck), 1_000e18);          // ~ +2.6% spot, within the guard
        (uint256 devNow, uint256 qNow) = basketC.stressFeeQuote();
        assertEq(devNow, devSettled, "TWAP deviation unchanged by the same-block buy");
        assertEq(qNow, qSettled, "quoted bp unchanged");
        vm.recordLogs();
        vm.prank(alice); basketC.redeem(ridA, 0, 0);
        (, uint256 evBp,) = _lastStressFee(vm.getRecordedLogs());
        assertEq(evBp, qSettled, "charged the settled bp");
    }

    // ---- partial redemption carries the bonus slice ----------------------- //

    function test_partialRedeem_retiresProRataBonus() public {
        address pool = _addPaxg();
        uint256 ridA = _depositPaxg(alice, DEP);
        uint256 ridB = _depositPaxg(bob,   DEP);
        _settle(pool, address(paxg), 1e18);
        vm.prank(alice); basketC.redeem(ridA, 0, 0);
        uint256 S = basketC.stressBonusPrincipal();
        (uint256 pB,,,) = basketC.deposits(ridB);

        // Bob redeems 25%: outstanding drops by 25% of (pB + S), his principal
        // by 25% of pB, the bonus by 25% of S.  (Still under duress: he pays a
        // fee of his own, which re-adds bonus; disable it to read the slice.)
        vm.prank(GOV); basketC.setStressFee(0, 0, 0);
        _warmTwap();
        uint256 oBefore = basketC.totalOutstandingBuck();
        vm.prank(bob); basketC.redeem(ridB, 2500, 0);
        (uint256 pB2,,,) = basketC.deposits(ridB);
        assertEq(pB2, pB - pB * 2500 / 10000, "principal down 25%");
        assertApproxEqAbs(basketC.stressBonusPrincipal(), S - S / 4, 1, "bonus down 25%");
        assertApproxEqAbs(basketC.totalOutstandingBuck(), oBefore - (pB + S) / 4, 2,
                          "outstanding down 25% of principal + bonus");
        assertEq(basketC.totalOutstandingBuck(), pB2 + basketC.stressBonusPrincipal(),
                 "invariant");
    }
}
