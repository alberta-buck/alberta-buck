// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}    from "forge-std/Test.sol";
import {ERC20}   from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20}  from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {BuckBasketOps}       from "../../src/basket/BuckBasketOps.sol";
import {BuckBasketUniswapV3} from "../../src/basket/BuckBasketUniswapV3.sol";
import {BuckBasketStorage}   from "../../src/basket/BuckBasketStorage.sol";
import {IBuckBasketVenue}    from "../../src/basket/IBuckBasketVenue.sol";
import {IStabilizer}         from "../../src/basket/IStabilizer.sol";
import {BuckKControllerDirect} from "../../src/BuckKControllerDirect.sol";
import {BuckKControllerShadow} from "../../src/BuckKControllerShadow.sol";
import {ShadowObserver}      from "../../src/basket/ShadowObserver.sol";
import {MockBuck, MockController, BBToken, IV3Pool}
    from "./BuckBasketProRata.t.sol";

/// @dev A level-1 stabilizer with a settable book, for the observer's
///      registry arithmetic (WP-3a).
contract MockStabilizer is IStabilizer {
    int256  public netInventory;
    uint256 public saturation;
    uint256 public cap = 1_000_000e6;      // WP-13: readable by default
    bool    public capReverts;
    function set(int256 inv, uint256 sat) external { netInventory = inv; saturation = sat; }
    function setCap(uint256 c) external { cap = c; }
    function setCapReverts(bool r) external { capReverts = r; }
    function capacity() external view returns (uint256) { return 1e18 - saturation; }
    function positionCap() external view returns (uint256) {
        require(!capReverts, "nav down");
        return cap;
    }
}

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
    ShadowObserver      internal obs;      // WP-3a
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
        obs = new ShadowObserver(address(basketC), GOV);

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

    // ======================================================================= //
    // WP-3a: the desk as an IStabilizer, and the ShadowObserver over it
    // ======================================================================= //

    function _venueView() internal view returns (IBuckBasketVenue) {
        return IBuckBasketVenue(address(basketC));
    }

    function _nav() internal view returns (uint256) {
        (, , uint256 B, ) = _venueView().poolBuckValues();
        return 2 * B;
    }

    /// netInventory = monetaryBuckHeld - max(monetaryOutstanding, 0), as the
    /// NatSpec derives it from the book.
    function _expectedNet() internal view returns (int256) {
        int256 o = basketC.monetaryOutstanding();
        return int256(basketC.monetaryBuckHeld()) - (o > 0 ? o : int256(0));
    }

    function _room(uint256 cap, uint256 used) internal pure returns (uint256) {
        if (cap == 0 || used >= cap) return 0;
        return (cap - used) * 1e18 / cap;
    }

    function _expectedCapacity() internal view returns (uint256) {
        (, uint32 posBp, uint32 outBp, bool enabled) = basketC.opsParams();
        if (!enabled) return 0;
        uint256 nav = _nav();
        int256 o = basketC.monetaryOutstanding();
        uint256 posRoom = _room(nav * posBp / 10000, basketC.monetaryBuckHeld());
        uint256 outRoom = _room(nav * outBp / 10000, o < 0 ? uint256(-o) : uint256(o));
        return posRoom < outRoom ? posRoom : outRoom;
    }

    function _setOps(uint32 posBp, uint32 outBp, bool enabled) internal {
        vm.prank(GOV);
        basketC.setOpsParams(BuckBasketOps.OpsParams({
            maxLegBp: 200, maxPositionBp: posBp, maxOutrightBp: outBp, enabled: enabled
        }));
    }

    function _newShadow() internal returns (BuckKControllerShadow k) {
        k = new BuckKControllerShadow(5e11, 2e7, 0, 3600, 0, 0.95e18, 0.75e18, GOV);
        vm.prank(GOV); k.setBasket(address(basketC));
        vm.prank(GOV); k.setObserver(address(obs));
    }

    /// @dev The S0 design tag: the position loop carries the price loop's
    ///      gains, so mode S reproduces D4 as built (WP-13).
    function _newShadowS0() internal returns (BuckKControllerShadow k) {
        k = _newShadow();
        vm.prank(GOV); k.setPositionGains(5e11, 2e7, 0);
    }

    function _newDirect() internal returns (BuckKControllerDirect k) {
        k = new BuckKControllerDirect(5e11, 2e7, 0, 3600, 0, 0.95e18, 0.75e18, GOV);
        vm.prank(GOV); k.setBasket(address(basketC));
    }

    // ---- IStabilizer: the desk's book ------------------------------------- //

    /// The seam reports the desk's own book: issued negative, absorbed
    /// positive, a Q2 burn of retired-beyond-issuance NOT counted, and the
    /// formula holding at every step of a Q4 -> Q1 -> Q2 walk.
    function test_stabilizer_netInventory_tracksTheBook() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _depositPaxg(bob, 1e18); _enable();
        assertEq(basketC.netInventory(), 0, "empty book");

        for (uint256 i = 0; i < 3; i++) _operate(40, true);      // Q4 x3
        int256 afterIssue = basketC.netInventory();
        assertLt(afterIssue, 0, "issued counts negative");
        assertEq(afterIssue, -basketC.monetaryOutstanding(), "= -issued while nothing held");
        assertEq(afterIssue, _expectedNet());

        assertEq(_operate(-40, false), 1);                        // Q1 absorb
        int256 afterAbsorb = basketC.netInventory();
        assertGt(afterAbsorb, afterIssue, "absorbing raises net inventory");
        assertEq(afterAbsorb, _expectedNet());

        assertEq(_operate(-40, true), 2);                         // Q2 retire from inventory
        assertEq(basketC.netInventory(), _expectedNet(), "formula after the burn");
    }

    /// Retiring PAST the desk's own issuance (monetaryOutstanding < 0) is the
    /// level-2 escalation: permanent, out of the book, not inventory.
    function test_stabilizer_retiredBeyondIssuance_isNotInventory() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        _operate(40, true);                                       // Q4: TOKEN to spend
        _operate(-40, true);                                      // Q2: burns fresh purchase
        // Keep retiring until the book is net negative (or the desk runs dry).
        for (uint256 i = 0; i < 4 && basketC.monetaryOutstanding() >= 0; i++) {
            _operate(40, true);
            _operate(-40, true);
        }
        int256 o = basketC.monetaryOutstanding();
        if (o < 0) {
            assertEq(basketC.netInventory(), int256(basketC.monetaryBuckHeld()),
                     "negative outstanding contributes nothing");
        }
        assertEq(basketC.netInventory(), _expectedNet());
    }

    function test_stabilizer_capacity_fullWhenIdle() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        assertEq(basketC.capacity(), 1e18);
        assertEq(basketC.saturation(), 0);
    }

    /// capacity is the SMALLER remaining fraction under the two book
    /// bounds, with the contract's own integer arithmetic.
    function test_stabilizer_capacity_shrinksWithTheBook() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        _operate(40, true);                                       // Q4: |outstanding| up
        assertLt(basketC.capacity(), 1e18);
        assertEq(basketC.capacity(), _expectedCapacity());
        assertEq(basketC.saturation(), 1e18 - basketC.capacity());

        _operate(-40, false);                                     // Q1: inventory up
        assertEq(basketC.capacity(), _expectedCapacity());
    }

    function test_stabilizer_saturates_atEitherBound() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        _operate(40, true);
        _operate(-40, false);                                     // some inventory
        assertGt(basketC.monetaryBuckHeld(), 0);

        _setOps(0, 1000, true);                                   // position bound hit
        assertEq(basketC.capacity(), 0);
        assertEq(basketC.saturation(), 1e18, "position bound => saturated");

        _setOps(1000, 0, true);                                   // outright bound hit
        assertEq(basketC.saturation(), 1e18, "outright bound => saturated");

        _setOps(1000, 1000, true);
        assertLt(basketC.saturation(), 1e18, "bounds relaxed");
    }

    function test_stabilizer_disabledDesk_reportsPinned() public {
        _addPaxg(); _depositPaxg(alice, 1e18);
        assertEq(basketC.capacity(), 0, "off by default: cannot act");
        assertEq(basketC.saturation(), 1e18);
        assertEq(basketC.positionCap(), 0, "WP-13: disabled reads cap 0, no revert");
    }

    /// An unreadable NAV (empty basket -> poolBuckValues reverts NoValue)
    /// must not revert the view: it is on K's compute() path.
    function test_stabilizer_emptyBasket_doesNotRevert() public {
        _enable();
        assertEq(basketC.capacity(), 0);
        assertEq(basketC.saturation(), 1e18);
        assertEq(basketC.netInventory(), 0);
        assertEq(basketC.shadowDepth(), 0);
        // WP-13: the cap is the one view that REVERTS on an unreadable NAV
        // -- the observer holds its last good cap behind try/catch.
        vm.expectRevert(BuckBasketOps.NavUnreadable.selector);
        basketC.positionCap();
    }

    // ---- the observer: registry --------------------------------------------- //

    function test_observer_registry_onlyGovernance() public {
        MockStabilizer s = new MockStabilizer();
        vm.expectRevert(ShadowObserver.NotGovernance.selector);
        obs.addStabilizer(address(s), 1e18);
        vm.expectRevert(ShadowObserver.NotGovernance.selector);
        obs.removeStabilizer(address(s));
        vm.expectRevert(ShadowObserver.NotGovernance.selector);
        obs.setStabilizerLambda(address(s), 1e18);
        vm.expectRevert(ShadowObserver.NotGovernance.selector);
        obs.setShadowLambda(1e18);
        vm.expectRevert(ShadowObserver.NotGovernance.selector);
        obs.setShadowOffset(1);
    }

    function test_observer_registry_addRemoveSetLambda() public {
        MockStabilizer a = new MockStabilizer();
        MockStabilizer b = new MockStabilizer();
        MockStabilizer c = new MockStabilizer();

        vm.prank(GOV); obs.addStabilizer(address(a), 1e18);
        vm.prank(GOV); obs.addStabilizer(address(b), 0.5e18);
        vm.prank(GOV); obs.addStabilizer(address(c), 2e18);
        assertEq(obs.stabilizerCount(), 3);
        assertEq(obs.stabilizerIndex(address(a)), 1);
        assertEq(obs.stabilizerIndex(address(c)), 3);

        vm.prank(GOV);
        vm.expectRevert(ShadowObserver.AlreadyPresent.selector);
        obs.addStabilizer(address(a), 1e18);
        vm.prank(GOV);
        vm.expectRevert(ShadowObserver.Stabilizer0.selector);
        obs.addStabilizer(address(0), 1e18);
        vm.prank(GOV);
        vm.expectRevert(ShadowObserver.LambdaTooLarge.selector);
        obs.addStabilizer(makeAddr("big"), 1_001e18);
        vm.prank(GOV);
        vm.expectRevert(ShadowObserver.StabilizerUnknown.selector);
        obs.removeStabilizer(makeAddr("nobody"));
        vm.prank(GOV);
        vm.expectRevert(ShadowObserver.StabilizerUnknown.selector);
        obs.setStabilizerLambda(makeAddr("nobody"), 1e18);

        // swap-and-pop: removing the first moves the last into its slot.
        vm.prank(GOV); obs.removeStabilizer(address(a));
        assertEq(obs.stabilizerCount(), 2);
        (address s0, uint256 l0, , , , ) = obs.stabilizers(0);
        assertEq(s0, address(c)); assertEq(l0, 2e18);
        assertEq(obs.stabilizerIndex(address(c)), 1);
        assertEq(obs.stabilizerIndex(address(a)), 0, "gone");

        // removing the last needs no move.
        vm.prank(GOV); obs.removeStabilizer(address(c));
        assertEq(obs.stabilizerCount(), 1);
        (s0, l0, , , , ) = obs.stabilizers(0);
        assertEq(s0, address(b)); assertEq(l0, 0.5e18);

        vm.prank(GOV); obs.setStabilizerLambda(address(b), 0.25e18);
        (, l0, , , , ) = obs.stabilizers(0);
        assertEq(l0, 0.25e18);

        // re-adding a removed one works.
        vm.prank(GOV); obs.addStabilizer(address(a), 3e18);
        assertEq(obs.stabilizerIndex(address(a)), 2);
    }

    // ---- the observer: arithmetic ------------------------------------------- //

    /// shadowValueInBuck = bvib + sum_i lambda_i * netInventory_i / D, with D
    /// the pools' BUCK balance, in the contract's own integer arithmetic.
    function test_observer_shadowValue_weightsAndDepth() public {
        address pool = _addPaxg(); _depositPaxg(alice, 1e18);
        MockStabilizer a = new MockStabilizer();
        MockStabilizer b = new MockStabilizer();
        a.set(int256(7e18), 0);                   // absorbed
        b.set(-int256(3e18), 0);                  // issued
        vm.prank(GOV); obs.addStabilizer(address(a), 1e18);
        vm.prank(GOV); obs.addStabilizer(address(b), 0.5e18);

        uint256 depth = basketC.shadowDepth();
        assertEq(depth, buck.balanceOf(pool), "D = the pools' BUCK reserve");
        assertGt(depth, 0);

        int256 bvib = _venueView().basketValueInBuck();
        int256 weighted = int256(1e18) * int256(7e18) + int256(0.5e18) * (-int256(3e18));
        assertEq(obs.shadowValueInBuck(), bvib + weighted / int256(depth));
        assertGt(obs.shadowValueInBuck(), bvib, "net absorbed raises the value");

        // A stabilizer at lambda 0 is not consulted; the rest still count.
        vm.prank(GOV); obs.setStabilizerLambda(address(a), 0);
        assertEq(obs.shadowValueInBuck(),
                 bvib + (int256(0.5e18) * (-int256(3e18))) / int256(depth));
        assertLt(obs.shadowValueInBuck(), bvib, "net issued lowers the value");
    }

    /// The sim-only pseudo-stabilizer: absorbed positive raises, issued
    /// negative lowers, and lambda 0 restores the identity exactly.
    function test_observer_shadowOffset_signAndIdentity() public {
        _addPaxg(); _depositPaxg(alice, 1e18);
        int256  bvib  = _venueView().basketValueInBuck();
        uint256 depth = basketC.shadowDepth();

        vm.prank(GOV); obs.setShadowLambda(1e18);
        vm.prank(GOV); obs.setShadowOffset(int256(5e18));
        assertEq(obs.shadowValueInBuck(), bvib + int256(1e18) * int256(5e18) / int256(depth));
        assertGt(obs.shadowValueInBuck(), bvib);

        vm.prank(GOV); obs.setShadowOffset(-int256(5e18));
        assertEq(obs.shadowValueInBuck(), bvib - int256(1e18) * int256(5e18) / int256(depth));

        vm.prank(GOV); obs.setShadowLambda(0);
        assertEq(obs.shadowValueInBuck(), bvib, "lambda 0: exactly bvib");

        vm.prank(GOV); obs.setShadowLambda(2e18);
        vm.prank(GOV); obs.setShadowOffset(0);
        assertEq(obs.shadowValueInBuck(), bvib, "zero inventory: exactly bvib");
    }

    function test_observer_shadowValue_noStabilizers_isBvib() public {
        _addPaxg(); _depositPaxg(alice, 1e18);
        assertEq(obs.shadowValueInBuck(), _venueView().basketValueInBuck());
        assertEq(obs.shadowSaturation(), 0);
    }

    /// saturation = max_i min(1, g_i * min(1, |q_i| / heldCap_i)), computed
    /// by the OBSERVER from the book and the held cap (WP-13); g_i is
    /// lambda_i under S and w_i under V.  The stabilizer's own saturation()
    /// is no longer consulted.
    function test_observer_saturation_lambdaWeightedMax() public {
        MockStabilizer a = new MockStabilizer();
        MockStabilizer b = new MockStabilizer();
        a.setCap(100e6); b.setCap(100e6);
        a.set(30e6, 1e18);                        // fill 0.3 (its own sat, 1, is ignored)
        b.set(-100e6, 0);                         // fill 1 (issued: |q|)
        vm.prank(GOV); obs.addStabilizer(address(a), 1e18);
        vm.prank(GOV); obs.addStabilizer(address(b), 0.5e18);
        assertEq(obs.shadowSaturation(), 0.5e18, "max(1*0.3, 0.5*1)");

        vm.prank(GOV); obs.setStabilizerLambda(address(a), 2e18);
        assertEq(obs.shadowSaturation(), 0.6e18, "max(2*0.3, 0.5*1)");

        a.set(100e6, 0);
        assertEq(obs.shadowSaturation(), 1e18, "2*1 capped at 1");

        vm.prank(GOV); obs.setStabilizerLambda(address(a), 0);
        vm.prank(GOV); obs.setStabilizerLambda(address(b), 0);
        assertEq(obs.shadowSaturation(), 0, "lambda 0 stabilizers cannot schedule K");

        // Under V the weights take lambda's place.
        vm.prank(GOV); obs.setMode(ShadowObserver.Mode.V);
        vm.prank(GOV); obs.setStabilizerWeight(address(a), 0.25e18);
        vm.prank(GOV); obs.setStabilizerWeight(address(b), 0.5e18);
        assertEq(obs.shadowSaturation(), 0.5e18, "max(0.25*1, 0.5*1)");
        b.setCap(0); obs.refresh();               // b disabled: excluded
        assertEq(obs.shadowSaturation(), 0.25e18);
        b.setCap(100e6); b.setCapReverts(true); obs.refresh();   // still held at 0
        assertEq(obs.shadowSaturation(), 0.25e18, "held cap 0 until a good read");
    }

    // ---- the desk as the first registered stabilizer, end to end ------------ //

    /// With the desk registered at lambda 0 the shadow controller IS the
    /// direct controller on the real book.
    function test_observer_deskAtLambdaZero_isIdentity() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        BuckKControllerShadow kS = _newShadow();
        BuckKControllerDirect kD = _newDirect();
        vm.prank(GOV); obs.addStabilizer(address(basketC), 0);
        _operate(40, true);                                       // Q4: a real book
        assertLt(basketC.netInventory(), 0);
        assertEq(obs.shadowValueInBuck(), _venueView().basketValueInBuck());
        vm.warp(block.timestamp + 3601);
        kD.compute(); kS.compute();
        assertEq(kS.buckK(), kD.buckK());
        assertEq(kS.I(), kD.I());
        assertEq(kS.P(), kD.P());
    }

    /// Issued inventory on the real desk: the shadow value backs the desk's
    /// sale out of bvib, and K lands ABOVE Direct's (7.3).
    function test_observer_deskIssued_raisesKvsDirect() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        BuckKControllerShadow kS = _newShadowS0();
        BuckKControllerDirect kD = _newDirect();
        vm.prank(GOV); obs.addStabilizer(address(basketC), 1e18);
        _operate(40, true);                                       // Q4 issue
        assertLt(obs.shadowValueInBuck(), _venueView().basketValueInBuck());
        vm.warp(block.timestamp + 3601);
        kD.compute(); kS.compute();
        assertGt(kS.buckK(), kD.buckK(), "issued: K rises relative to Direct");
    }

    /// Absorbed inventory booked through the pseudo-stabilizer: K lands
    /// BELOW Direct's.
    function test_observer_offsetAbsorbed_lowersKvsDirect() public {
        _addPaxg(); _depositPaxg(alice, 1e18);
        BuckKControllerShadow kS = _newShadowS0();
        BuckKControllerDirect kD = _newDirect();
        int256 twoPctOfDepth = int256(basketC.shadowDepth() / 50);
        vm.prank(GOV); obs.setShadowLambda(1e18);
        vm.prank(GOV); obs.setShadowOffset(twoPctOfDepth);
        vm.warp(block.timestamp + 3601);
        kD.compute(); kS.compute();
        assertLt(kS.buckK(), kD.buckK(), "absorbed: K falls relative to Direct");
    }

    /// The desk full against its HELD cap schedules K's integral gain
    /// through the observer, and only when its lambda is non-zero.  A
    /// desk with a zero cap is DISABLED, not pinned: excluded, no schedule
    /// (WP-13, decision 9).
    function test_observer_deskSaturation_schedulesGamma() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        BuckKControllerShadow kS = _newShadow();
        vm.prank(GOV); kS.setGamma(1e18);
        vm.prank(GOV); obs.addStabilizer(address(basketC), 1e18);
        assertEq(kS.integralBoost(), 1e18, "idle desk: no boost");
        _operate(40, true);                                       // Q4: net inventory -40 bp of NAV
        assertLt(basketC.netInventory(), 0);
        assertGt(kS.integralBoost(), 1e18, "a partial fill schedules in proportion");
        assertLt(kS.integralBoost(), 2e18);
        _setOps(1, 1000, true); obs.refresh();                    // cap 1 bp of NAV < |q|
        assertEq(obs.stabilizerSaturation(address(basketC)), 1e18, "full against the held cap");
        assertEq(kS.integralBoost(), 2e18, "full desk doubles Ki");
        vm.prank(GOV); obs.setStabilizerLambda(address(basketC), 0);
        assertEq(kS.integralBoost(), 1e18, "lambda 0: no scheduling either");
        vm.prank(GOV); obs.setStabilizerLambda(address(basketC), 1e18);
        _setOps(0, 1000, true); obs.refresh();                    // cap 0: disabled
        assertTrue(obs.excluded(address(basketC)));
        assertEq(kS.integralBoost(), 1e18, "a disabled desk cannot schedule K");
    }

    // ======================================================================= //
    // WP-13: positionCap(), the held cap and the sensor-fault policy on the
    // real desk (decision 9)
    // ======================================================================= //

    /// The desk's cap is maxPositionBp x NAV (decision 11), the bound
    /// _absorb reverts on; it tracks the ops params and the NAV.
    function test_desk_positionCap_isMaxPositionBpOfNav() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        assertEq(basketC.positionCap(), _nav() * 1000 / 10000);
        _setOps(250, 1000, true);
        assertEq(basketC.positionCap(), _nav() * 250 / 10000);
        _depositPaxg(bob, 1e18);
        assertEq(basketC.positionCap(), _nav() * 250 / 10000, "tracks NAV");
        _setOps(250, 1000, false);
        assertEq(basketC.positionCap(), 0, "disabled: 0");
        _setOps(0, 1000, true);
        assertEq(basketC.positionCap(), 0, "no inventory allowed: 0 (excluded)");
    }

    /// A spot pushed off its TWAP inside the window (the redemption guard)
    /// makes NAV unreadable: positionCap() reverts NavUnreadable, while
    /// netInventory() -- the position -- is unchanged by any of it.
    function test_desk_positionCap_revertsNavUnreadable_onGuardTrip() public {
        address pool = _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        _operate(40, true);                                       // Q4: a real book
        int256 q = basketC.netInventory();
        assertLt(q, 0);
        vm.warp(vm.getBlockTimestamp() + 700);                    // TWAP history at this price
        uint256 capBefore = basketC.positionCap();
        assertGt(capBefore, 0);

        _arb(pool, address(buck), 3000e18);                       // spot far off TWAP
        vm.expectRevert(BuckBasketStorage.Slippage.selector);
        _venueView().poolBuckValues();
        vm.expectRevert(BuckBasketOps.NavUnreadable.selector);
        basketC.positionCap();
        assertEq(basketC.netInventory(), q, "position always readable");
        assertEq(basketC.capacity(), 0, "capacity() keeps its WP-3a pinned reading");

        vm.warp(vm.getBlockTimestamp() + 700);                    // the window catches up
        assertGt(basketC.positionCap(), 0, "readable again");
    }

    /// The observer over the real desk: a guard trip keeps the held cap,
    /// the position, the saturation and the aggregate, sets stale(); the
    /// first successful read clears it and refreshes the cap.
    function test_observer_deskGuardTrip_holdsCap_flagsStale() public {
        address pool = _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        vm.prank(GOV); obs.setMode(ShadowObserver.Mode.V);
        vm.prank(GOV); obs.addStabilizer(address(basketC), 1e18);
        // dT 1 s, so a cycle can land INSIDE the 600 s TWAP window the trip lasts.
        BuckKControllerShadow kS = new BuckKControllerShadow(5e11, 2e7, 0, 1, 0, 0.95e18, 0.75e18, GOV);
        vm.prank(GOV); kS.setBasket(address(basketC));
        vm.prank(GOV); kS.setObserver(address(obs));
        vm.prank(GOV); kS.setPositionGains(5e11, 2e7, 0);
        _operate(40, true);                                       // Q4: issued, net -40 bp of NAV
        vm.warp(vm.getBlockTimestamp() + 3601);                   // TWAP history at this price
        kS.compute();                                             // observe(): readable
        assertFalse(obs.stale(address(basketC)));
        uint256 cap0 = obs.heldCap(address(basketC));
        assertEq(cap0, basketC.positionCap());
        int256  pos0 = obs.position(address(basketC));
        uint256 sat0 = obs.stabilizerSaturation(address(basketC));
        int256  s0   = obs.aggregatePosition();
        assertEq(pos0, basketC.netInventory() * 1e18 / int256(cap0));
        assertLt(pos0, 0);
        assertEq(s0, pos0, "one stabilizer at weight 1: s is its fill");

        _arb(pool, address(buck), 3000e18);                       // the trip
        vm.expectRevert(BuckBasketOps.NavUnreadable.selector);
        basketC.positionCap();
        vm.warp(vm.getBlockTimestamp() + 100);                    // inside the window
        kS.compute();                                             // observe() refreshes
        assertTrue(obs.stale(address(basketC)), "stale");
        assertFalse(obs.excluded(address(basketC)), "included on the held cap");
        assertEq(obs.heldCap(address(basketC)), cap0, "cap held");
        assertEq(obs.position(address(basketC)), pos0, "position unchanged");
        assertEq(obs.stabilizerSaturation(address(basketC)), sat0, "saturation unchanged");
        assertEq(obs.aggregatePosition(), s0, "aggregate unchanged");
        assertEq(kS.lastS(), s0, "K integrated the held position");
        BuckKControllerShadow.Terms memory t = kS.terms();
        assertEq(t.staleMask, 1); assertEq(t.excludedMask, 0);

        vm.warp(vm.getBlockTimestamp() + 700);                    // the window catches up
        kS.compute();
        assertFalse(obs.stale(address(basketC)), "first successful read clears stale");
        assertEq(obs.heldCap(address(basketC)), basketC.positionCap(), "cap refreshed");
        assertTrue(obs.heldCap(address(basketC)) != cap0, "the NAV moved with the price");
        (uint256 st, ) = obs.flags();
        assertEq(st, 0);
    }

    /// A disabled desk is EXCLUDED from s (both modes) and the weights
    /// renormalized without it; re-enabling brings it back on the next read.
    function test_observer_disabledDesk_excludedFromS() public {
        _addPaxg(); _depositPaxg(alice, 1e18); _enable();
        vm.prank(GOV); obs.addStabilizer(address(basketC), 1e18);
        MockStabilizer m = new MockStabilizer();
        m.setCap(100e6); m.set(50e6, 0);                          // fill 0.5
        vm.prank(GOV); obs.addStabilizer(address(m), 1e18);
        vm.prank(GOV); obs.setStabilizerWeight(address(m), 0.5e18);
        _operate(40, true);                                       // Q4: desk issued
        assertLt(basketC.netInventory(), 0);
        int256 bvib = _venueView().basketValueInBuck();
        assertLt(obs.shadowValueInBuck(), bvib, "S: the desk's issuance in the shadow value");

        // Disable: the desk drops out of both aggregations.
        _setOps(1000, 1000, false); obs.refresh();
        assertTrue(obs.excluded(address(basketC)));
        assertFalse(obs.stale(address(basketC)), "a good read of 0");
        assertEq(obs.position(address(basketC)), 0);
        uint256 depth = basketC.shadowDepth();
        assertEq(obs.shadowValueInBuck(), bvib + int256(1e18) * int256(50e6) / int256(depth),
                 "S: only the mock remains");
        vm.prank(GOV); obs.setMode(ShadowObserver.Mode.V);
        assertEq(obs.aggregatePosition(), int256(0.5e18), "V: renormalized to the mock alone");
        (, uint256 ex) = obs.flags();
        assertEq(ex, 1, "desk excluded (bit 0); nothing booked on the pseudo");
        vm.prank(GOV); obs.setShadowOffset(1);
        (, ex) = obs.flags();
        assertEq(ex, 1 | (1 << 255), "a booked pseudo inventory with no V cap is flagged");
        vm.prank(GOV); obs.setShadowOffset(0);

        // Re-enable: back on the next read, at its fill.
        _setOps(1000, 1000, true); obs.refresh();
        assertFalse(obs.excluded(address(basketC)));
        int256 fill = basketC.netInventory() * 1e18 / int256(obs.heldCap(address(basketC)));
        assertEq(obs.aggregatePosition(),
                 (int256(1e18) * fill + int256(0.5e18) * int256(0.5e18)) / int256(1.5e18));
    }
}
