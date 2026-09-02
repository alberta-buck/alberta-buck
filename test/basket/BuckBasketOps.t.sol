// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}    from "forge-std/Test.sol";
import {ERC20}   from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20}  from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {BuckBasketOps}       from "../../src/basket/BuckBasketOps.sol";
import {BuckBasketUniswapV3} from "../../src/basket/BuckBasketUniswapV3.sol";
import {BuckBasketStorage}   from "../../src/basket/BuckBasketStorage.sol";
import {MockBuck, MockController, BBToken, IV3Pool}
    from "./BuckBasketProRata.t.sol";

/// @dev Stands in for PairsRebalanceDirector's monetary surface so the
///      basket's quadrant logic can be driven directly.  The director's own
///      common-mode arithmetic is tested in BasketRebalanceDirector.t.sol.
///      The shell wraps every director call in try/catch, so implementing
///      only these two is safe.
contract MockMonetaryDirector {
    int32   public effortBp;
    bool    public outright;
    uint32  public epoch;

    function set(int32 e, bool o) external { effortBp = e; outright = o; }
    function bump() external { epoch += 1; }
    function epochNow() external view returns (uint32) { return epoch; }
    function monetaryEffort() external view returns (int32, bool) {
        return (effortBp, outright);
    }
}

/// @title The two-mode basket: monetary operations on the common mode.
///
/// The invariant this file exists for is the LAST one: a depositor's claim
/// must be untouched by anything the monetary desk does.  Everything else is
/// mechanism; that one is the reason the book is held as balances rather than
/// as liquidity, and it is the assertion that would catch a silent transfer
/// away from depositors if the book ever leaked into the payout path.
contract BuckBasketOpsTest is Test {

    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO =
        1461446703485210103287273052203988822378723970342;

    address constant GOV = address(0xA0);

    MockBuck            internal buck;
    MockController      internal ctrl;
    BuckBasketOps       internal basketC;
    BuckBasketUniswapV3 internal venueFacet;
    MockMonetaryDirector internal dir;
    address             internal v3Factory;

    BBToken internal paxg;

    address internal alice = address(0xA11CE);
    address internal bob   = address(0xB0B);

    uint256 constant PAXG_PRICE = 4000e18;

    function setUp() public {
        buck = new MockBuck();
        ctrl = new MockController();
        v3Factory = deployCode("out/UniswapV3Factory.sol/UniswapV3Factory.json");

        basketC = new BuckBasketOps(
            address(buck), address(ctrl), v3Factory, GOV,
            500, 600, 64, 500, 1e3
        );
        venueFacet = new BuckBasketUniswapV3();
        vm.prank(GOV); basketC.setVenue(address(venueFacet));

        dir = new MockMonetaryDirector();
        vm.prank(GOV); basketC.setDirector(address(dir));

        buck.setBasket(address(basketC));

        paxg = new BBToken("PAX Gold", "PAXG", 18);
        paxg.mint(alice, 1_000e18);
        paxg.mint(bob,   1_000e18);
        paxg.mint(address(this), 1_000_000e18);
        buck.mint(address(this), 1_000_000_000e18);
    }

    // ---- helpers --------------------------------------------------------- //

    function _enable() internal {
        vm.prank(GOV);
        basketC.setOpsParams(BuckBasketOps.OpsParams({
            maxLegBp: 200, maxPositionBp: 1000, maxOutrightBp: 1000, enabled: true
        }));
    }

    function _addPaxg() internal returns (address pool) {
        vm.prank(GOV);
        pool = basketC.addBasketToken(address(paxg), 18, PAXG_PRICE, 0, 500);
    }

    function _depositPaxg(address who, uint256 amt) internal returns (uint256 rid) {
        vm.prank(who); paxg.approve(address(basketC), amt);
        vm.prank(who); rid = basketC.depositToken(address(paxg), amt, 0);
    }

    function _arb(address pool, address tokenIn, uint256 amountIn) internal {
        bool zeroForOne = IV3Pool(pool).token0() == tokenIn;
        uint160 limit = zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1;
        IV3Pool(pool).swap(address(this), zeroForOne, int256(amountIn), limit, "");
    }

    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata) external {
        if (a0 > 0) IERC20(IV3Pool(msg.sender).token0()).transfer(msg.sender, uint256(a0));
        if (a1 > 0) IERC20(IV3Pool(msg.sender).token1()).transfer(msg.sender, uint256(a1));
    }

    /// @dev Run one operation at `effortBp`, advancing the director's epoch so
    ///      the once-per-epoch guard lets it through.
    function _operate(int32 effortBp, bool outright) internal returns (uint8) {
        dir.set(effortBp, outright);
        dir.bump();
        return basketC.monetaryOperation();
    }

    // ---- the desk is off by default -------------------------------------- //

    function test_disabledByDefault_revertsAndChangesNothing() public {
        _addPaxg();
        _depositPaxg(alice, 1e18);
        uint256 outstandingBefore = basketC.totalOutstandingBuck();

        dir.set(-40, false);
        vm.expectRevert(BuckBasketStorage.MonetaryIdle.selector);
        basketC.monetaryOperation();

        assertEq(basketC.totalOutstandingBuck(), outstandingBefore);
        assertEq(basketC.monetaryOutstanding(), 0);
    }

    function test_zeroEffort_isNoAdvice() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        dir.set(0, false); dir.bump();
        vm.expectRevert(BuckBasketStorage.NoAdvice.selector);
        basketC.monetaryOperation();
    }

    function test_oncePerEpoch() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        _operate(40, true);                      // Q4
        dir.set(40, true);                       // same epoch, no bump
        vm.expectRevert(BuckBasketStorage.StepAlreadyDone.selector);
        basketC.monetaryOperation();
    }

    // ---- Q4 ISSUE: mint against basket TOKEN and sell ---------------------- //

    function test_q4_issue_raisesSupplyAndBuysRealAssets() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();

        uint256 supply0 = buck.totalSupply();
        uint8 q = _operate(40, true);            // BUCK dear, persistent

        assertEq(q, 4, "Q4 issue");
        assertGt(basketC.monetaryOutstanding(), 0, "issued into circulation");
        assertGt(basketC.monetaryTokenHeld(0), 0, "acquired real assets");
        assertGt(buck.totalSupply(), supply0, "supply rose");
    }

    /// The second thing no agent can do: mintFromBasket is backed by basket
    /// TOKEN, not by insured-asset credit, so it is not capped by creditLimit.
    function test_q4_issuesWithoutAnyCreditLine() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        _operate(40, true);
        assertGt(basketC.monetaryOutstanding(), 0);
    }

    // ---- Q1 ABSORB: buy the dump and hold --------------------------------- //

    function test_q1_absorb_holdsInventoryWithoutChangingSupply() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        _operate(40, true);                      // Q4 first: acquire TOKEN

        uint256 supply1 = buck.totalSupply();
        int256  out1    = basketC.monetaryOutstanding();

        uint8 q = _operate(-40, false);          // BUCK cheap, not persistent
        assertEq(q, 1, "Q1 absorb");
        assertGt(basketC.monetaryBuckHeld(), 0, "inventory held");
        assertEq(basketC.monetaryOutstanding(), out1, "temporary: book unchanged");
        assertEq(buck.totalSupply(), supply1, "temporary: supply unchanged");
    }

    // ---- Q2 RETIRE: burn float the basket did not issue -------------------- //

    /// The whole reason this lives in the contract.  An agent removes float
    /// only while its own signed balance is drawn -- supply is
    /// sum_a max(0, signedRaw(a)) -- so it can never retire more than it
    /// issued.  burnFromBasket destroys BUCK bought from anyone.
    function test_q2_retire_burnsAndSupplyFalls() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        _operate(40, true);                      // Q4: acquire TOKEN to spend

        uint256 supply1 = buck.totalSupply();
        int256  out1    = basketC.monetaryOutstanding();

        uint8 q = _operate(-40, true);           // BUCK cheap, persistent
        assertEq(q, 2, "Q2 retire");
        assertLt(buck.totalSupply(), supply1, "supply fell");
        assertLt(basketC.monetaryOutstanding(), out1, "book contracted");
        assertEq(basketC.monetaryBuckHeld(), 0, "burned, not held");
    }

    // ---- Q3 SUPPLY: release inventory before opening a new position -------- //

    function test_q3_unwindsTemporaryBeforeOutright() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        _operate(40, true);                      // Q4
        _operate(-40, false);                    // Q1: build inventory
        uint256 held = basketC.monetaryBuckHeld();
        assertGt(held, 0);

        int256 out1 = basketC.monetaryOutstanding();
        uint8 q = _operate(40, true);            // dear again, persistent
        assertEq(q, 3, "Q3 releases inventory before Q4 opens more");
        assertLt(basketC.monetaryBuckHeld(), held, "inventory released");
        assertEq(basketC.monetaryOutstanding(), out1, "no new issuance yet");
    }

    // ---- the bounds -------------------------------------------------------- //

    function test_positionBound_stopsAbsorbing() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        _operate(40, true);
        // A zero position ceiling makes any inventory too much.
        vm.prank(GOV);
        basketC.setOpsParams(BuckBasketOps.OpsParams({
            maxLegBp: 200, maxPositionBp: 1000, maxOutrightBp: 1000, enabled: true
        }));
        _operate(-40, false);                    // Q1 builds some inventory
        vm.prank(GOV);
        basketC.setOpsParams(BuckBasketOps.OpsParams({
            maxLegBp: 200, maxPositionBp: 0, maxOutrightBp: 1000, enabled: true
        }));
        dir.set(-40, false); dir.bump();
        vm.expectRevert(BuckBasketStorage.MonetaryBound.selector);
        basketC.monetaryOperation();
    }

    function test_outrightBound_stopsIssuing() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        _operate(40, true);
        vm.prank(GOV);
        basketC.setOpsParams(BuckBasketOps.OpsParams({
            maxLegBp: 200, maxPositionBp: 1000, maxOutrightBp: 0, enabled: true
        }));
        dir.set(40, true); dir.bump();
        vm.expectRevert(BuckBasketStorage.MonetaryBound.selector);
        basketC.monetaryOperation();
    }

    function test_legBound_rejectsOversizedPolicy() public {
        vm.prank(GOV);
        vm.expectRevert(BuckBasketStorage.Bp10000.selector);
        basketC.setOpsParams(BuckBasketOps.OpsParams({
            maxLegBp: 501, maxPositionBp: 1000, maxOutrightBp: 1000, enabled: true
        }));
    }

    /// Escalation must be reachable FROM THE STATE THAT TRIGGERS IT.
    ///
    /// The inventory ceiling exists because a large absorbed position that
    /// has not come back is the one signal the desk's own action cannot
    /// suppress -- so being pinned at it is exactly when the desk should
    /// escalate to an outright retirement.  The first cut tested the ceiling
    /// at the top of _absorb and reverted before Q2 was ever considered, so
    /// a pinned desk could never escalate: Q2 fired zero times across two
    /// 365-day chain runs while the ceiling bound on 150 and 95 days.
    function test_q2_escalatesEvenWhenPinnedAtPositionCeiling() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        _operate(40, true);                       // Q4: acquire TOKEN
        _operate(-40, false);                     // Q1: build inventory
        uint256 held = basketC.monetaryBuckHeld();
        assertGt(held, 0, "inventory present");

        // Pin it: any inventory at all is now over the ceiling.
        vm.prank(GOV);
        basketC.setOpsParams(BuckBasketOps.OpsParams({
            maxLegBp: 200, maxPositionBp: 0, maxOutrightBp: 1000, enabled: true
        }));

        uint256 supply1 = buck.totalSupply();
        int256  out1    = basketC.monetaryOutstanding();
        uint8 q = _operate(-40, true);            // cheap AND persistent
        assertEq(q, 2, "pinned desk still escalates to Q2");
        assertLt(basketC.monetaryBuckHeld(), held, "inventory burned down");
        assertLt(buck.totalSupply(), supply1, "supply fell");
        // The book CONTRACTS.  It does not necessarily go negative here --
        // this sequence opens with Q4, so the desk is retiring its own
        // issuance first; retiring past that is what a longer excursion does.
        assertLt(basketC.monetaryOutstanding(), out1, "book contracted");
    }

    /// Burning inventory needs no TOKEN, which is the point: by the time
    /// persistence is established the desk has usually spent its reserves
    /// absorbing, and requiring a fresh purchase made Q2 unreachable.
    function test_q2_retiresWithNoTokenReservesLeft() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        _operate(40, true);
        _operate(-40, false);                     // spends the TOKEN book
        assertEq(basketC.monetaryTokenHeld(0), 0, "reserves spent");
        assertGt(basketC.monetaryBuckHeld(), 0);

        assertEq(_operate(-40, true), 2, "Q2 from inventory alone");
    }

    // ---- THE INVARIANT: depositors are untouched --------------------------- //

    /// A monetary operation must never move `totalOutstandingBuck`.  That
    /// counter is the denominator of every depositor payout (theta = R/O), so
    /// letting monetary issuance into it would dilute every receipt in the
    /// basket by exactly the amount issued.
    function test_monetaryOps_neverTouchOutstandingBuck() public {
        _addPaxg();
        _depositPaxg(alice, 1e18);
        _depositPaxg(bob,   1e18);
        _enable();

        uint256 o0 = basketC.totalOutstandingBuck();
        assertGt(o0, 0);

        // Three issues first: Q1 and Q2 are funded from the TOKEN the desk's
        // own issuance bought, so a one-operation book is spent by the first
        // absorb and the retire has nothing left to work with.
        for (uint256 i = 0; i < 3; i++) {
            _operate(40, true);                   // Q4 issue
            assertEq(basketC.totalOutstandingBuck(), o0, "issue did not dilute");
        }

        assertEq(_operate(-40, false), 1, "Q1 absorb");
        assertEq(basketC.totalOutstandingBuck(), o0, "absorb did not dilute");

        assertEq(_operate(-40, true), 2, "Q2 retire");
        assertEq(basketC.totalOutstandingBuck(), o0, "retire did not dilute");
    }

    /// The desk's assets are not part of a pro-rata claim.  After EVERY
    /// depositor has exited, the monetary book must still be there: if it
    /// were inside `depL` it would have been paid out along the way.
    function test_monetaryBook_survivesFullDepositorExit() public {
        _addPaxg();
        uint256 ridA = _depositPaxg(alice, 1e18);
        uint256 ridB = _depositPaxg(bob,   1e18);
        _enable();

        _operate(40, true);                       // Q4: the desk buys TOKEN
        uint256 deskToken = basketC.monetaryTokenHeld(0);
        assertGt(deskToken, 0, "desk holds TOKEN");

        vm.prank(alice); basketC.redeem(ridA, 0);
        assertEq(basketC.monetaryTokenHeld(0), deskToken, "untouched by exit 1");

        vm.prank(bob);   basketC.redeem(ridB, 0);
        assertEq(basketC.totalOutstandingBuck(), 0, "all depositors out");
        assertEq(basketC.monetaryTokenHeld(0), deskToken, "untouched by exit 2");
        assertGt(basketC.monetaryOutstanding(), 0, "book still open");
    }

    /// The book is held as BALANCES, never as liquidity, which is why none of
    /// the redemption allocator needed to change.  If a future edit ever LP'd
    /// it, this catches that: the desk's BUCK would show up as basket
    /// liquidity and start being paid out.
    function test_monetaryBuck_isHeldAsBalanceNotLiquidity() public {
        address pool = _addPaxg();
        _depositPaxg(alice, 1e18);
        _enable();
        _operate(40, true);
        _operate(-40, false);                     // Q1: absorb into inventory

        uint256 held = basketC.monetaryBuckHeld();
        assertGt(held, 0);
        assertGe(buck.balanceOf(address(basketC)), held,
                 "inventory is a basket balance");
        pool;
    }
}
