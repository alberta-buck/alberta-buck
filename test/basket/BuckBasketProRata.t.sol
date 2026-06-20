// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}    from "forge-std/Test.sol";
import {ERC20}   from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20}  from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {BuckBasketProRata} from "../../src/basket/BuckBasketProRata.sol";
import {BuckBasketReceipt} from "../../src/basket/BuckBasketReceipt.sol";

/// @dev Minimal BUCK: plain ERC-20 + the basket mint/burn hooks.  Avoids the
///      identity/carrying machinery of the production Buck so the pro-rata
///      redeem logic can be exercised in isolation.
contract MockBuck is ERC20 {
    address public basket;
    constructor() ERC20("Buck", "BUCK") {}
    function setBasket(address b) external { basket = b; }
    function mintFromBasket(address to, uint256 amt) external {
        require(msg.sender == basket, "!basket"); _mint(to, amt);
    }
    function burnFromBasket(uint256 amt) external {
        require(msg.sender == basket, "!basket"); _burn(msg.sender, amt);
    }
    function mint(address to, uint256 amt) external { _mint(to, amt); }   // test helper
}

contract MockController {
    function compute() external returns (uint256) { return 1e18; }
    function reprime() external {}
}

contract BBToken is ERC20 {
    uint8 immutable _dec;
    constructor(string memory n, string memory s, uint8 d) ERC20(n, s) { _dec = d; }
    function decimals() public view override returns (uint8) { return _dec; }
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

interface IV3Factory {
    function feeAmountTickSpacing(uint24 fee) external view returns (int24);
}

interface IV3Pool {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function liquidity() external view returns (uint128);
    function swap(address recipient, bool zeroForOne, int256 amountSpecified,
                  uint160 sqrtPriceLimitX96, bytes calldata data)
        external returns (int256 amount0, int256 amount1);
}

/// @title Pro-rata redeem scaffold tests.
contract BuckBasketProRataTest is Test {

    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO =
        1461446703485210103287273052203988822378723970342;

    address constant GOV = address(0xA0);

    MockBuck       internal buck;
    MockController internal ctrl;
    BuckBasketProRata internal basketC;
    BuckBasketReceipt internal receipt;
    address        internal v3Factory;

    BBToken internal paxg;    // 18-dec
    BBToken internal cbbtc;   // 8-dec

    address internal alice = address(0xA11CE);
    address internal bob   = address(0xB0B);

    uint256 constant PAXG_PRICE  = 4000e18;     // 1 PAXG = 4000 BUCK
    uint256 constant CBBTC_PRICE = 100000e18;   // 1 cbBTC = 100000 BUCK

    function setUp() public {
        buck = new MockBuck();
        ctrl = new MockController();
        v3Factory = deployCode("out/UniswapV3Factory.sol/UniswapV3Factory.json");

        basketC = new BuckBasketProRata(
            address(buck), address(ctrl), v3Factory, GOV,
            500,    // fee tier 0.05%
            600,    // twap window
            64,     // observation cardinality
            500,    // 5% spot/TWAP manipulation guard (cold pools self-skip)
            1e3     // min seed liquidity
        );
        buck.setBasket(address(basketC));
        receipt = basketC.receipt();

        paxg  = new BBToken("PAX Gold",   "PAXG",  18);
        cbbtc = new BBToken("cbBTC",      "cbBTC",  8);

        paxg.mint(alice, 1_000e18);
        paxg.mint(bob,   1_000e18);
        cbbtc.mint(alice, 1_000e8);

        // Test contract holds reserves to act as an external arb on the pools.
        paxg.mint(address(this), 1_000_000e18);
        buck.mint(address(this), 1_000_000_000e18);
    }

    // ---- helpers --------------------------------------------------------- //

    function _addPaxg() internal returns (address pool) {
        vm.prank(GOV);
        pool = basketC.addConstituent(address(paxg), 18, PAXG_PRICE, 0, 500);
    }

    function _depositPaxg(address who, uint256 amt) internal returns (uint256 rid) {
        vm.prank(who); paxg.approve(address(basketC), amt);
        vm.prank(who); rid = basketC.depositToken(address(paxg), amt, 0);
    }

    /// @dev Push the pool price by swapping `amountIn` of `tokenIn` into it.
    ///      tokenIn = BUCK ⇒ buys TOKEN ⇒ pool BUCK-heavy (inflation).
    ///      tokenIn = TOKEN ⇒ sells TOKEN ⇒ pool BUCK-light (deflation).
    function _arb(address pool, address tokenIn, uint256 amountIn) internal {
        bool zeroForOne = IV3Pool(pool).token0() == tokenIn;
        uint160 limit = zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1;
        IV3Pool(pool).swap(address(this), zeroForOne, int256(amountIn), limit, "");
    }

    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata) external {
        if (a0 > 0) IERC20(IV3Pool(msg.sender).token0()).transfer(msg.sender, uint256(a0));
        if (a1 > 0) IERC20(IV3Pool(msg.sender).token1()).transfer(msg.sender, uint256(a1));
    }

    /// @dev Age the pool past the TWAP window.  The deposit's mint already wrote
    ///      an observation; V3 `observe` forward-extrapolates from it, so warping
    ///      > twapWindow is enough for `consult` to return a real TWAP.
    function _warmTwap() internal {
        vm.warp(block.timestamp + 700);   // > 600s window
    }

    // ---- tests ----------------------------------------------------------- //

    function test_deposit_mints_and_issues_receipt() public {
        _addPaxg();
        uint256 rid = _depositPaxg(alice, 1e18);   // 1 PAXG
        (uint256 principal,,,) = basketC.deposits(rid);
        assertApproxEqRel(principal, PAXG_PRICE, 0.01e18, "principal ~ 4000 BUCK");
        assertEq(basketC.totalOutstandingBuck(), principal, "outstanding tracks principal");
        assertEq(receipt.ownerOf(rid), alice, "alice owns receipt");
    }

    function test_redeem_stable_burnsPrincipal_returnsToken() public {
        _addPaxg();
        uint256 rid = _depositPaxg(alice, 1e18);
        uint256 paxgBefore = paxg.balanceOf(alice);

        vm.prank(alice);
        basketC.redeem(rid, 0);

        assertEq(basketC.totalOutstandingBuck(), 0, "outstanding cleared");
        // Stable: no profit; depositor gets ~all PAXG back (minus dust).
        assertApproxEqRel(paxg.balanceOf(alice), paxgBefore + 1e18, 0.01e18, "PAXG returned");
        vm.expectRevert();
        receipt.ownerOf(rid);   // burned
    }

    function test_redeem_partial_halvesPrincipal() public {
        _addPaxg();
        uint256 rid = _depositPaxg(alice, 1e18);
        uint256 outBefore = basketC.totalOutstandingBuck();

        vm.prank(alice);
        basketC.redeem(rid, 5000);   // 50%

        (uint256 principal,,,) = basketC.deposits(rid);
        assertApproxEqRel(principal, outBefore / 2, 0.001e18, "principal halved");
        assertApproxEqRel(basketC.totalOutstandingBuck(), outBefore / 2, 0.001e18, "outstanding halved");
        assertEq(receipt.ownerOf(rid), alice, "receipt survives partial");
    }

    function test_redeem_thinSecondPool_stillSucceeds() public {
        // PAXG pool deeply seeded; cbBTC pool seeded very thin.  A pro-rata
        // redeem must touch both without reverting.
        _addPaxg();
        vm.prank(GOV);
        basketC.addConstituent(address(cbbtc), 8, CBBTC_PRICE, 0, 500);

        _depositPaxg(alice, 10e18);                 // deep PAXG
        vm.prank(alice); cbbtc.approve(address(basketC), 1e5);
        vm.prank(alice); basketC.depositToken(address(cbbtc), 1e5, 0);  // thin cbBTC (0.001)

        uint256 ridPaxg = 1;   // first receipt
        uint256 paxgBefore = paxg.balanceOf(alice);
        uint256 cbbtcBefore = cbbtc.balanceOf(alice);

        vm.prank(alice);
        basketC.redeem(ridPaxg, 0);

        // Pro-rata across both pools ⇒ alice receives PAXG and a sliver of cbBTC.
        assertGt(paxg.balanceOf(alice), paxgBefore, "got PAXG");
        assertGt(cbbtc.balanceOf(alice), cbbtcBefore, "got cbBTC from thin pool");
    }

    function test_redeem_inflation_splitsProfitWithTreasury() public {
        address pool = _addPaxg();
        uint256 rid = _depositPaxg(alice, 1e18);

        // Inflation: external arb buys PAXG with BUCK ⇒ pool BUCK-heavy.
        _arb(pool, address(buck), 2000e18);

        uint256 buckBefore = buck.balanceOf(alice);
        vm.prank(alice);
        basketC.redeem(rid, 0);

        assertEq(basketC.totalOutstandingBuck(), 0, "principal retired");
        assertGt(buck.balanceOf(alice), buckBefore, "depositor got BUCK profit share");
        assertGt(basketC.treasuryBuckPending(), 0, "treasury accrued its share");
    }

    function test_redeem_deflation_coversShortfall() public {
        address pool = _addPaxg();
        // Two depositors so alice's redeem (θ<1) leaves pool liquidity for the
        // shortfall-cover swap.
        uint256 ridA = _depositPaxg(alice, 1e18);
        _depositPaxg(bob, 1e18);

        // Mild deflation: arb sells PAXG for BUCK ⇒ pool BUCK-light.
        _arb(pool, address(paxg), 0.4e18);

        uint256 outBefore = basketC.totalOutstandingBuck();
        (uint256 principalA,,,) = basketC.deposits(ridA);

        uint256 paxgBefore = paxg.balanceOf(alice);
        // Generous loss budget (20%): the thinned-pool conversion is costly, but
        // the caller opts in to confirm the cover mechanism works.
        vm.prank(alice);
        basketC.redeem(ridA, 0, 2000);

        assertApproxEqAbs(basketC.totalOutstandingBuck(), outBefore - principalA, 1e9,
            "principal fully retired");
        assertGt(paxg.balanceOf(alice), paxgBefore, "depositor still gets (reduced) PAXG");
    }

    function test_redeem_deflation_defaultBudgetReverts() public {
        address pool = _addPaxg();
        uint256 ridA = _depositPaxg(alice, 1e18);
        _depositPaxg(bob, 1e18);

        // Same deflation, but the default 1% budget refuses the costly thin-pool
        // conversion — protecting the caller from a large haircut.
        _arb(pool, address(paxg), 0.4e18);

        vm.prank(alice);
        vm.expectRevert(bytes("conversion loss"));
        basketC.redeem(ridA, 0);   // default 1% budget
    }

    function test_redeem_underwater_reverts() public {
        address pool = _addPaxg();
        uint256 ridA = _depositPaxg(alice, 1e18);
        _depositPaxg(bob, 1e18);

        // Extreme deflation: dump a large PAXG amount so BUCK in the pool is
        // crushed far below principal — even selling the whole slice can't burn R.
        _arb(pool, address(paxg), 50e18);

        vm.prank(alice);
        vm.expectRevert(bytes("underwater"));
        basketC.redeem(ridA, 0);
    }

    function test_redeem_sellHigh_drawsFromOverweightPool() public {
        address pPaxg = _addPaxg();
        vm.prank(GOV);
        basketC.addConstituent(address(cbbtc), 8, CBBTC_PRICE, 0, 500);

        // Equal-value deposits → 50/50 at target.
        uint256 ridPaxg = _depositPaxg(alice, 1e18);              // ~4000 BUCK
        vm.prank(alice); cbbtc.approve(address(basketC), 4e6);
        vm.prank(alice); basketC.depositToken(address(cbbtc), 4e6, 0);  // ~4000 BUCK

        // Inflate the PAXG pool (arb buys PAXG with BUCK) → PAXG overweight,
        // cbBTC underweight.
        _arb(pPaxg, address(buck), 4000e18);

        uint256 paxgBefore  = paxg.balanceOf(alice);
        uint256 cbbtcBefore = cbbtc.balanceOf(alice);

        // Small redemption: sell-high draws entirely from the overweight PAXG
        // pool, leaving the underweight cbBTC pool untouched.
        vm.prank(alice);
        basketC.redeem(ridPaxg, 1000, 2000);   // 10%

        assertGt(paxg.balanceOf(alice), paxgBefore, "drew from overweight PAXG");
        assertEq(cbbtc.balanceOf(alice), cbbtcBefore, "left underweight cbBTC untouched");
    }

    // ---- single-TOKEN payout --------------------------------------------- //

    function test_redeem_singleToken_drawsOnlyFromThatPool() public {
        _addPaxg();
        vm.prank(GOV);
        basketC.addConstituent(address(cbbtc), 8, CBBTC_PRICE, 0, 500);

        uint256 ridPaxg = _depositPaxg(alice, 1e18);          // ~4000 claim
        // Deep cbBTC pool (bob) so it can source alice's claim (f < 1).
        cbbtc.mint(bob, 1_000e8);
        vm.prank(bob); cbbtc.approve(address(basketC), 40e6);
        vm.prank(bob); basketC.depositToken(address(cbbtc), 40e6, 0);  // ~40000

        uint256 paxgBefore  = paxg.balanceOf(alice);
        uint256 cbbtcBefore = cbbtc.balanceOf(alice);

        // Redeem the PAXG receipt entirely into cbBTC — only the cbBTC pool is touched.
        vm.prank(alice);
        basketC.redeem(ridPaxg, 0, address(cbbtc), 2000);

        assertGt(cbbtc.balanceOf(alice), cbbtcBefore, "paid out in cbBTC");
        assertEq(paxg.balanceOf(alice), paxgBefore, "PAXG pool untouched");
    }

    function test_redeem_singleToken_tooThinReverts() public {
        _addPaxg();
        vm.prank(GOV);
        basketC.addConstituent(address(cbbtc), 8, CBBTC_PRICE, 0, 500);

        uint256 ridPaxg = _depositPaxg(alice, 1e18);          // ~4000 claim
        vm.prank(alice); cbbtc.approve(address(basketC), 1e5);
        vm.prank(alice); basketC.depositToken(address(cbbtc), 1e5, 0);  // ~100 pool

        // cbBTC pool can't source the ~4000 claim → f > 1.
        vm.prank(alice);
        vm.expectRevert(bytes("token too thin"));
        basketC.redeem(ridPaxg, 0, address(cbbtc), 2000);
    }

    function test_redeem_singleToken_deflationWithinPoolConversion() public {
        address pool = _addPaxg();
        uint256 ridA = _depositPaxg(alice, 1e18);
        _depositPaxg(bob, 1e18);

        _arb(pool, address(paxg), 0.4e18);   // deflate the PAXG pool

        // Single-token into PAXG covers the burn by converting PAXG->BUCK on the
        // PAXG pool itself (within-pool), under a generous budget.
        uint256 paxgBefore = paxg.balanceOf(alice);
        vm.prank(alice);
        basketC.redeem(ridA, 0, address(paxg), 2000);

        assertGt(paxg.balanceOf(alice), paxgBefore, "got PAXG via within-pool conversion");
    }

    // ---- TWAP manipulation guard ----------------------------------------- //

    function test_redeem_guardReverts_onSpotManipulation() public {
        address pool = _addPaxg();
        uint256 rid = _depositPaxg(alice, 1e18);
        _warmTwap();   // establish TWAP history at the deposit price

        // Sandwich the value read: push spot far off TWAP just before redeem.
        _arb(pool, address(buck), 3000e18);

        vm.prank(alice);
        vm.expectRevert(bytes("slippage"));
        basketC.redeem(rid, 0);
    }

    function test_redeem_guardPasses_warmPoolNoManipulation() public {
        _addPaxg();
        uint256 rid = _depositPaxg(alice, 1e18);
        _warmTwap();   // spot == TWAP, no manipulation

        vm.prank(alice);
        basketC.redeem(rid, 0);
        assertEq(basketC.totalOutstandingBuck(), 0, "guard passes, redeemed");
    }

    function test_sweepTreasury_reinvestsProfit() public {
        address pool = _addPaxg();
        uint256 ridA = _depositPaxg(alice, 1e18);
        _depositPaxg(bob, 1e18);   // keeps pool liquid after alice exits

        // Inflation: arb buys PAXG with BUCK ⇒ pool BUCK-heavy ⇒ alice's redeem
        // yields BUCK profit, half of which the treasury keeps.
        _arb(pool, address(buck), 2000e18);

        vm.prank(alice);
        basketC.redeem(ridA, 0);

        uint256 pendingBefore = basketC.treasuryBuckPending();
        assertGt(pendingBefore, 0, "treasury accrued profit");
        assertEq(basketC.treasuryLiquidityOf(0), 0, "no treasury LP yet");

        basketC.sweepTreasury();   // permissionless keeper call

        assertGt(basketC.treasuryLiquidityOf(0), 0, "treasury re-LP'd into pool");
        assertLt(basketC.treasuryBuckPending(), pendingBefore, "pending consumed");
    }

    function test_sweepTreasury_excludedFromDepositorRedeem() public {
        address pool = _addPaxg();
        uint256 ridA = _depositPaxg(alice, 1e18);
        uint256 ridB = _depositPaxg(bob, 1e18);
        _arb(pool, address(buck), 2000e18);

        vm.prank(alice);
        basketC.redeem(ridA, 0);
        basketC.sweepTreasury();
        uint128 trL = basketC.treasuryLiquidityOf(0);
        assertGt(trL, 0, "treasury L present");

        // Bob (last depositor) exits fully; the treasury L is NOT part of his
        // pro-rata claim, so it stays in the pool.
        vm.prank(bob);
        basketC.redeem(ridB, 0);
        assertEq(basketC.totalOutstandingBuck(), 0, "all depositors out");
        assertEq(basketC.treasuryLiquidityOf(0), trL, "treasury L preserved");
    }

    function test_receipt_tokenURI_returnsDataUri() public {
        _addPaxg();
        uint256 rid = _depositPaxg(alice, 1e18);

        // Exercises the receipt -> basket.deposits() cross-read.
        bytes memory uri = bytes(receipt.tokenURI(rid));
        bytes memory prefix = bytes("data:application/json;base64,");
        assertGt(uri.length, prefix.length, "non-empty data uri");
        for (uint256 i = 0; i < prefix.length; i++) {
            assertEq(uri[i], prefix[i], "data uri prefix");
        }
    }
}
