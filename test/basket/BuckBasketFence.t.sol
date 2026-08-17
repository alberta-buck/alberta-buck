// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}    from "forge-std/Test.sol";
import {IERC20}  from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {BuckBasketFence}     from "../../src/basket/BuckBasketFence.sol";
import {BuckBasketUniswapV3} from "../../src/basket/BuckBasketUniswapV3.sol";
import {BuckBasketStorage}   from "../../src/basket/BuckBasketStorage.sol";
import {MockBuck, BBToken, IV3Pool} from "./BuckBasketProRata.t.sol";

/// @dev K is the whole experiment here, so it has to be drivable.
contract SettableK {
    uint256 public k = 0.75e18;
    function setK(uint256 v) external { k = v; }
    function compute() external view returns (uint256) { return k; }
    function reprime() external {}
}

/// @title The K-scaled fence basket.
///
/// The measured problem this contract exists for: over 730 days BUCK_K sat at
/// its HARD FLOOR for 153 days, and on 147 of them parity was still broken by
/// more than 2%.  K had spent its whole authority and could not close the gap,
/// because the basket's own BUCK -- roughly HALF of all supply -- is minted at
/// a permanent 100% LTV by full-range pairing and is completely immune to
/// creditLimit.  These tests pin the inversion: the basket issues K x
/// collateral like every other issuer, so K falling FORCES it to contract.
contract BuckBasketFenceTest is Test {

    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO =
        1461446703485210103287273052203988822378723970342;

    address constant GOV = address(0xA0);

    MockBuck            internal buck;
    SettableK           internal ctrl;
    BuckBasketFence     internal basketC;
    BuckBasketUniswapV3 internal venueFacet;
    address             internal v3Factory;

    BBToken internal paxg;

    address internal alice = address(0xA11CE);
    address internal bob   = address(0xB0B);

    uint256 constant PAXG_PRICE = 4000e18;
    uint24  constant DEPOSIT_TIER = 3000;   // depositors' pool
    uint24  constant FENCE_TIER   = 500;    // the fence, same pair

    function setUp() public {
        buck = new MockBuck();
        ctrl = new SettableK();
        v3Factory = deployCode("out/UniswapV3Factory.sol/UniswapV3Factory.json");

        basketC = new BuckBasketFence(
            address(buck), address(ctrl), v3Factory, GOV,
            DEPOSIT_TIER, 600, 64, 500, 1e3, FENCE_TIER
        );
        venueFacet = new BuckBasketUniswapV3();
        vm.prank(GOV); basketC.setVenue(address(venueFacet));
        buck.setBasket(address(basketC));

        paxg = new BBToken("PAX Gold", "PAXG", 18);
        paxg.mint(alice, 1_000e18);
        paxg.mint(bob,   1_000e18);
        paxg.mint(address(this), 1_000_000e18);
        buck.mint(address(this), 1_000_000_000e18);
    }

    // ---- helpers --------------------------------------------------------- //

    function _open() internal returns (address fencePool) {
        vm.prank(GOV);
        basketC.addBasketToken(address(paxg), 18, PAXG_PRICE, 0, DEPOSIT_TIER);
        vm.prank(GOV);
        basketC.openFence(0);
        (fencePool,,,,,,) = basketC.fenceOf(0);
    }

    function _deposit(address who, uint256 amt) internal returns (uint256 rid) {
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

    // ---- the core claim: issuance is K-scaled ----------------------------- //

    /// A full-range basket mints ~1:1 against the deposit -- 100% LTV, K-immune.
    /// This one mints K x, which is the entire reason it exists.
    function test_deposit_mintsKTimesCollateralNotOneToOne() public {
        _open();
        uint256 rid = _deposit(alice, 1e18);          // 1 PAXG == 4000 BUCK

        (uint256 principal,,,) = basketC.deposits(rid);
        uint256 shares = basketC.shareOf(rid);

        // Shares are the REAL value contributed; principal is K x that.
        assertApproxEqRel(shares, 4000e18, 0.02e18, "shares == TOKEN value");
        assertApproxEqRel(principal, 3000e18, 0.02e18, "principal == K x value");
        assertLt(principal, shares, "LTV strictly below 100%");
    }

    /// Two depositors contributing equal real value at DIFFERENT K must get
    /// equal shares.  Using the minted BUCK as the share unit would short the
    /// second one by whatever the controller happened to be set to that day.
    function test_sharesAreKIndependent() public {
        _open();
        uint256 ridA = _deposit(alice, 1e18);
        ctrl.setK(0.40e18);
        uint256 ridB = _deposit(bob, 1e18);

        assertApproxEqRel(basketC.shareOf(ridA), basketC.shareOf(ridB), 0.03e18,
                          "equal value, equal shares");
        (uint256 pA,,,) = basketC.deposits(ridA);
        (uint256 pB,,,) = basketC.deposits(ridB);
        assertLt(pB, pA, "but the SECOND minted less BUCK, because K fell");
    }

    // ---- the lever: K falling forces the basket to contract ---------------- //

    function test_kFalling_forcesContraction() public {
        _open();
        _deposit(alice, 10e18);

        uint256 supply0 = buck.totalSupply();
        ctrl.setK(0.30e18);                        // controller tightens hard

        int256 delta = basketC.fenceRebalance(0);
        assertLt(delta, int256(0), "basket retired BUCK");
        assertLt(buck.totalSupply(), supply0, "supply actually fell");
    }

    function test_kRising_allowsExpansion() public {
        _open();
        _deposit(alice, 10e18);
        ctrl.setK(0.30e18);
        basketC.fenceRebalance(0);

        uint256 supply1 = buck.totalSupply();
        ctrl.setK(0.90e18);
        int256 delta = basketC.fenceRebalance(0);
        assertGt(delta, int256(0), "basket issued BUCK");
        assertGt(buck.totalSupply(), supply1, "supply rose");
    }

    /// The property the ops basket could not have: the basket's footprint
    /// TRACKS K rather than ignoring it.  Half the float becomes responsive.
    function test_footprintTracksKMonotonically() public {
        _open();
        _deposit(alice, 10e18);

        uint256[] memory ks = new uint256[](4);
        ks[0] = 0.90e18; ks[1] = 0.60e18; ks[2] = 0.40e18; ks[3] = 0.20e18;
        uint256 prev = type(uint256).max;
        for (uint256 j = 0; j < ks.length; j++) {
            ctrl.setK(ks[j]);
            basketC.fenceRebalance(0);
            (uint256 b,) = basketC.fenceAmounts(0);
            uint256 idle = IERC20(address(buck)).balanceOf(address(basketC));
            uint256 footprint = b + idle;
            assertLt(footprint, prev, "footprint shrinks as K falls");
            prev = footprint;
        }
    }

    // ---- the fence is a different pool: no contamination -------------------- //

    function test_fenceUsesASeparateFeeTier() public {
        address fencePool = _open();
        (,,,,,, bool live) = basketC.fenceOf(0);
        assertTrue(live, "fence open");
        (, , , , , address depositPool, , , , , ) = basketC.constituents(0);
        assertTrue(fencePool != depositPool,
                   "fence and depositor pools are distinct");
        assertTrue(fencePool != address(0));
    }

    // ---- the harvest accrues to depositors ---------------------------------- //

    /// Fees and captured premium are re-deployed WITHOUT issuing shares, so
    /// every receipt's claim rises pro-rata.  No separate accounting: the
    /// harvest is visible as fenceNav growing while totalShares does not.
    function test_harvestRaisesEveryClaimWithoutIssuingShares() public {
        address pool = _open();
        _deposit(alice, 10e18);
        _deposit(bob,   10e18);
        uint256 shares0 = basketC.totalShares();
        uint256 nav0 = basketC.fenceNav();

        // A REAL round trip: swap in, then swap back exactly what came out.
        // Two fixed-size legs are not one -- the fee makes the return leg
        // short, the price ratchets, and the basket takes genuine
        // impermanent loss that swamps the fee income being measured.
        for (uint256 r = 0; r < 6; r++) {
            uint256 t0 = paxg.balanceOf(address(this));
            _arb(pool, address(buck), 20_000e18);
            uint256 got = paxg.balanceOf(address(this)) - t0;
            if (got > 0) _arb(pool, address(paxg), got);
        }
        basketC.fenceRebalance(0);

        assertEq(basketC.totalShares(), shares0, "no shares issued");
        assertGt(basketC.fenceNav(), nav0, "claim base grew: that is the harvest");
    }

    // ---- redemption --------------------------------------------------------- //

    function test_redeem_paysInKindAndRetiresPrincipal() public {
        _open();
        uint256 ridA = _deposit(alice, 10e18);
        _deposit(bob, 10e18);

        uint256 before = paxg.balanceOf(alice);
        uint256 shares0 = basketC.totalShares();
        uint256 supply0 = buck.totalSupply();

        vm.prank(alice);
        basketC.redeem(ridA, 0);

        assertGt(paxg.balanceOf(alice), before, "paid in kind");
        assertLt(basketC.totalShares(), shares0, "shares retired");
        assertLt(buck.totalSupply(), supply0, "principal burned");
        assertEq(basketC.shareOf(ridA), 0, "receipt emptied");
    }

    function test_redeem_isProRataBetweenDepositors() public {
        _open();
        uint256 ridA = _deposit(alice, 10e18);
        uint256 ridB = _deposit(bob,   20e18);
        assertApproxEqRel(basketC.shareOf(ridB), 2 * basketC.shareOf(ridA),
                          0.03e18, "bob owns twice the basket");

        uint256 aBefore = paxg.balanceOf(alice);
        uint256 bBefore = paxg.balanceOf(bob);
        vm.prank(alice); basketC.redeem(ridA, 0);
        vm.prank(bob);   basketC.redeem(ridB, 0);

        uint256 aGot = paxg.balanceOf(alice) - aBefore;
        uint256 bGot = paxg.balanceOf(bob) - bBefore;
        assertApproxEqRel(bGot, 2 * aGot, 0.10e18, "payouts track shares");
    }

    // ---- fix 3: the claim base nets the obligation ------------------------ //

    /// A deposit of t adds t of TOKEN and mints K*t of BUCK: assets rise by
    /// (1+K)t while the obligation rises by K*t, a net t -- exactly the shares
    /// issued.  Reporting assets without netting made the basket look 1/(1+K)
    /// richer than it is; the band-only version made it look poorer, because
    /// about (1-K) of every deposit's TOKEN sits outside the band.
    function test_navNetsTheObligationAndTracksShares() public {
        _open();
        _deposit(alice, 10e18);
        _deposit(bob,   10e18);

        assertGt(basketC.fenceAssets(), basketC.fenceNav(),
                 "assets exceed NAV by the outstanding obligation");
        assertApproxEqRel(basketC.fenceNav(), basketC.totalShares(), 0.05e18,
                          "NAV tracks shares");
        assertApproxEqRel(uint256(basketC.netIssued()),
                          basketC.fenceAssets() - basketC.fenceNav(),
                          0.01e18, "the gap IS the live obligation");
    }

    /// netIssued must follow fenceRebalance, which totalOutstandingBuck does
    /// not: that counter is the sum of deposit principals and never moves
    /// when the K budget does.
    function test_netIssuedFollowsRebalanceWhereOutstandingDoesNot() public {
        _open();
        _deposit(alice, 10e18);
        int256 net0 = basketC.netIssued();
        uint256 out0 = basketC.totalOutstandingBuck();

        ctrl.setK(0.30e18);
        basketC.fenceRebalance(0);

        assertLt(basketC.netIssued(), net0, "live obligation fell with K");
        assertEq(basketC.totalOutstandingBuck(), out0,
                 "the principal ledger did not, which is why it cannot net");
    }

    // ---- fix 2: value does not get trapped in BUCK ------------------------- //

    /// Push the band so it converts to BUCK, then exit.  Paying only TOKEN
    /// left the depositor's value behind as "harvest" and put the net claim
    /// base 42% under shares in the first chain run.
    function test_redeem_returnsValueAfterBandConvertsToBuck() public {
        address pool = _open();
        uint256 rid = _deposit(alice, 10e18);
        _deposit(bob, 10e18);
        uint256 shares = basketC.shareOf(rid);

        // Buy TOKEN out of the band with BUCK: the position converts toward
        // BUCK, which is exactly the state that used to strand the claim.
        _arb(pool, address(buck), 60_000e18);

        uint256 before = paxg.balanceOf(alice);
        vm.prank(alice);
        basketC.redeem(rid, 0);
        uint256 got = paxg.balanceOf(alice) - before;
        assertGt(got, 0, "paid something");

        // Paid in kind on BOTH sides, so count both.
        uint256 valueOut = got * PAXG_PRICE / 1e18 + buck.balanceOf(alice);
        assertGt(valueOut, (shares * 3) / 4,
                 "claim is not stranded on the BUCK side");
    }

    /// The mirror: a band that came back mostly TOKEN cannot cover its burn
    /// from BUCK alone.  Selling just enough TOKEN is what keeps the
    /// obligation retiring; without it every later claim is overstated.
    function test_redeem_coversBurnWhenBandIsAllToken() public {
        address pool = _open();
        uint256 rid = _deposit(alice, 10e18);
        _deposit(bob, 10e18);

        _arb(pool, address(paxg), 30e18);            // band converts to TOKEN

        uint256 out0 = basketC.totalOutstandingBuck();
        vm.prank(alice);
        basketC.redeem(rid, 0);
        assertLt(basketC.totalOutstandingBuck(), out0, "obligation retired");
    }

    function test_openFence_isOncePerConstituent() public {
        _open();
        vm.prank(GOV);
        vm.expectRevert(BuckBasketStorage.AlreadyPresent.selector);
        basketC.openFence(0);
    }
}
