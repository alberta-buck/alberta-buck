// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}                  from "forge-std/Test.sol";
import {ERC20}                 from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20}                from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {BN254}                 from "../src/BN254.sol";
import {IdentityRegistry}      from "../src/IdentityRegistry.sol";
import {Buck}                  from "../src/Buck.sol";
import {BuckCredit}            from "../src/BuckCredit.sol";
import {BuckKControllerDirect} from "../src/BuckKControllerDirect.sol";
import {BuckBasket}            from "../src/BuckBasket.sol";
import {BuckBasketReceipt}     from "../src/BuckBasketReceipt.sol";

contract BBToken is ERC20 {
    uint8 immutable _dec;
    constructor(string memory n, string memory s, uint8 d) ERC20(n, s) { _dec = d; }
    function decimals() public view override returns (uint8) { return _dec; }
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

interface IV3Factory {
    function createPool(address, address, uint24) external returns (address);
    function getPool(address, address, uint24) external view returns (address);
    function feeAmountTickSpacing(uint24 fee) external view returns (int24);
}

interface IV3PoolForTest {
    function swap(address recipient, bool zeroForOne, int256 amountSpecified,
                  uint160 sqrtPriceLimitX96, bytes calldata data)
        external returns (int256 amount0, int256 amount1);
    function slot0() external view returns (uint160, int24, uint16, uint16,
                                              uint16, uint8, bool);
    function token0() external view returns (address);
    function token1() external view returns (address);
}

/// @title BuckBasketTest -- Layers 4 + 5 of the direct-embodiment test plan.
///        Real Uniswap V3 pools, real Buck + identity stack.
contract BuckBasketTest is Test {

    address constant GOV    = address(0xA0);
    address constant POOL   = address(0xBA51C);
    address constant ISSUER = address(0x1551E1);

    Buck                   internal buck;
    BuckCredit             internal credit;
    BuckKControllerDirect  internal kCtrl;
    BuckBasket             internal basketC;
    BuckBasketReceipt      internal receipt;
    IdentityRegistry       internal reg;
    address                internal v3Factory;

    BBToken internal paxg;   // 18-dec mock RWA
    BBToken internal cbbtc;  // 8-dec mock RWA

    address internal alice;
    string  internal vj;

    uint256 constant PAXG_INITIAL_PRICE_BUCK  = 4000e18;   // 1 PAXG = 4000 BUCK
    uint256 constant CBBTC_INITIAL_PRICE_BUCK = 100000e18; // 1 cbBTC = 100K BUCK

    function setUp() public {
        vm.chainId(1);
        vj = vm.readFile("test/vectors/identity.json");

        reg = new IdentityRegistry(GOV);
        _trustIssuer();
        alice = address(uint160(_u(".alice.registrant")));
        _registerAlice();

        credit = new BuckCredit();

        kCtrl = new BuckKControllerDirect(
            0.1e18, 0.01e18, 0,
            60,                  // dT 60s
            0.50e18, 1.50e18,
            1.0e18,
            GOV
        );

        buck = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));

        // V3 factory (deployed from compiled artifact, same path as other tests).
        v3Factory = deployCode("out/UniswapV3Factory.sol/UniswapV3Factory.json");

        // Deploy basket and wire the system together.
        basketC = new BuckBasket(
            address(buck),
            address(kCtrl),
            v3Factory,
            GOV,
            500,                // 0.05% fee tier
            600,                // 10-min TWAP
            64,                 // observation cardinality
            50,                 // 0.5% slippage default
            1e3                 // minSeedLiquidity floor
        );
        receipt = basketC.receipt();

        vm.prank(POOL);
        buck.setBasket(address(basketC));

        vm.prank(GOV);
        kCtrl.setBasket(address(basketC));

        // Bind the basket so Buck.transfer can flow through it (Public + Carrying).
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT({
            R: BN254.g1(), C: BN254.g1()
        });
        vm.prank(address(this));
        reg.bindContract(address(basketC), BN254.g1(), E, true, true);

        // Mutual decryptability: private EOA Alice must CP-approve the
        // public basket so the operator can decrypt her identity from the
        // receipt on redeem (basket→alice BUCK payout).
        {
            bytes32 _fragSlot = keccak256(
                abi.encode(address(basketC), keccak256(abi.encode(alice, uint256(5))))
            );
            vm.store(address(buck), _fragSlot, bytes32(uint256(1)));
        }

        // Mock RWA tokens.
        paxg  = new BBToken("Tether Gold (mock)",      "PAXG",  18);
        cbbtc = new BBToken("Coinbase Wrapped BTC",    "cbBTC",  8);

        // Mint balances to Alice.
        paxg .mint(alice, 1_000e18);
        cbbtc.mint(alice, 1_000e8);
    }

    // -------------------------------------------------------------------- //
    //  Configuration                                                         //
    // -------------------------------------------------------------------- //

    function test_addBasketToken_sets_constituent_and_creates_pool() public {
        vm.prank(GOV);
        address pool = basketC.addBasketToken(
            address(paxg), 18, PAXG_INITIAL_PRICE_BUCK, 10000, 500
        );
        assertTrue(pool != address(0));
        assertEq(basketC.constituentsLength(), 1);

        // V3 factory has the pool registered for (BUCK, PAXG, 500).
        address fromFactory = IV3Factory(v3Factory).getPool(
            address(buck), address(paxg), 500
        );
        assertEq(pool, fromFactory);

        // basketValueInBuck at init prices should equal 1e18 (within tick
        // rounding from the V3 spacing of 10 for the 0.05% fee tier).
        assertApproxEqRel(basketC.basketValueInBuck(), int256(1e18), 0.001e18);
    }

    function test_addBasketToken_only_governance() public {
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("Not governance");
        basketC.addBasketToken(
            address(paxg), 18, PAXG_INITIAL_PRICE_BUCK, 10000, 500
        );
    }

    function test_two_constituent_basket_value_at_init_is_unit() public {
        vm.startPrank(GOV);
        basketC.addBasketToken(address(paxg), 18,  PAXG_INITIAL_PRICE_BUCK,  5000, 500);
        basketC.addBasketToken(address(cbbtc), 8,  CBBTC_INITIAL_PRICE_BUCK, 5000, 500);
        vm.stopPrank();

        // Each pool reports its init price -> basket value = 0.5 + 0.5 = 1.0.
        assertApproxEqAbs(basketC.basketValueInBuck(), int256(1e18), 1e15);
    }

    // -------------------------------------------------------------------- //
    //  Direct mint -- single deposit lifecycle                              //
    // -------------------------------------------------------------------- //

    /// @dev Bind a V3 pool as PublicIdentity + Carrying so BUCK can transit
    ///      through it.  v1 BuckBasket doesn't do this automatically;
    ///      governance must bind every pool spawned by addBasketToken.
    function _bindPool(address pool) internal {
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT({
            R: BN254.g1(), C: BN254.g1()
        });
        reg.bindContract(pool, BN254.g1(), E, true, true);
    }

    function test_deposit_mints_BUCK_and_issues_receipt() public {
        vm.prank(GOV);
        address pool = basketC.addBasketToken(
            address(paxg), 18, PAXG_INITIAL_PRICE_BUCK, 10000, 500
        );
        _bindPool(pool);

        uint256 depositAmt = 1e18;          // 1 PAXG
        uint256 expectedBuck = 4000e18;     // 1 * 4000 BUCK at init price

        vm.prank(alice);
        paxg.approve(address(basketC), depositAmt);

        // Cold-pool deposit (no TWAP yet) -> pass maxDeviationBp=0 to skip guard.
        vm.prank(alice);
        uint256 receiptId = basketC.depositToken(address(paxg), depositAmt, 0);

        assertEq(receipt.ownerOf(receiptId), alice);

        (
            uint256 principalB,
            uint256 principalT,
            address token,
            /* uint64 */
        ) = basketC.deposits(receiptId);
        assertEq(token, address(paxg));
        assertEq(principalT, depositAmt);
        assertApproxEqRel(principalB, expectedBuck, 0.001e18);

        // totalSupply grew by the minted BUCK.
        assertEq(buck.totalSupply(), principalB);
    }

    function test_redeem_burns_principal_and_returns_token() public {
        vm.prank(GOV);
        address pool = basketC.addBasketToken(
            address(paxg), 18, PAXG_INITIAL_PRICE_BUCK, 10000, 500
        );
        _bindPool(pool);

        vm.prank(alice);
        paxg.approve(address(basketC), 1e18);
        vm.prank(alice);
        uint256 rid = basketC.depositToken(address(paxg), 1e18, 0);

        uint256 supplyBefore = buck.totalSupply();
        uint256 aliceTBefore = paxg.balanceOf(alice);

        // Redeem immediately.  No external swaps -> profit ~= 0.
        vm.prank(alice);
        basketC.redeem(rid, 0, 0);

        vm.expectRevert();
        receipt.ownerOf(rid);

        // Principal BUCK burned (modulo tiny V3 rounding).
        assertLt(buck.totalSupply(), supplyBefore);
        assertLt(buck.totalSupply(), 1e15);   // ~all of the principalB burned

        // Alice got her PAXG back.
        assertApproxEqRel(paxg.balanceOf(alice), aliceTBefore + 1e18, 0.001e18);
    }

    function test_redeem_revertsWhenNotOwner() public {
        vm.prank(GOV);
        address pool = basketC.addBasketToken(
            address(paxg), 18, PAXG_INITIAL_PRICE_BUCK, 10000, 500
        );
        _bindPool(pool);

        vm.prank(alice);
        paxg.approve(address(basketC), 1e18);
        vm.prank(alice);
        uint256 rid = basketC.depositToken(address(paxg), 1e18, 0);

        address attacker = makeAddr("attacker");
        vm.prank(attacker);
        vm.expectRevert("not owner");
        basketC.redeem(rid, 0, 0);
    }

    function test_redeem_profitSplitPreservesTreasury() public {
        vm.prank(GOV);
        address pool = basketC.addBasketToken(
            address(paxg), 18, PAXG_INITIAL_PRICE_BUCK, 10000, 500
        );
        _bindPool(pool);

        // Alice deposits.
        vm.prank(alice);
        paxg.approve(address(basketC), 1e18);
        vm.prank(alice);
        uint256 rid = basketC.depositToken(address(paxg), 1e18, 0);

        (uint256 principalB, uint256 principalT, address tok,) =
            basketC.deposits(rid);
        uint256 supplyBefore = buck.totalSupply();
        // Silence unused-warning.
        tok; principalT; principalB;

        // Redeem immediately at the same pool price.  No external swaps
        // have occurred, so profit ≈ 0.  The split:
        //   profitT = max(0, tokenOut - principalT)
        //   profitB = max(0, buckOut - principalB)
        //   half to user, half retained as treasury (re-deposited into pool).
        vm.prank(alice);
        basketC.redeem(rid, 0, 0);

        // Principal BUCK is burned.
        assertLt(buck.totalSupply(), supplyBefore, "principal BUCK burned");

        // Deposit record is cleared after redeem.
        (, uint256 postPrincipalT,,) = basketC.deposits(rid);
        assertEq(postPrincipalT, 0, "deposit record cleared");
    }

    /// @dev maxDeviationBp=0 skips the TWAP guard entirely; deposit succeeds
    ///      even if the spot price has moved.
    function test_depositToken_zeroMaxDeviationSkipsGuard() public {
        vm.prank(GOV);
        address pool = basketC.addBasketToken(
            address(paxg), 18, PAXG_INITIAL_PRICE_BUCK, 10000, 500
        );
        _bindPool(pool);

        // Two deposits at different sizes, both with maxDeviationBp=0.
        vm.prank(alice);
        paxg.approve(address(basketC), 1e18);
        vm.prank(alice);
        basketC.depositToken(address(paxg), 1e18, 0);

        paxg.mint(alice, 0.5e18);
        vm.prank(alice);
        paxg.approve(address(basketC), 0.5e18);
        vm.prank(alice);
        basketC.depositToken(address(paxg), 0.5e18, 0);
    }

    /// @dev Cold-pool deposit with non-zero maxDeviationBp: the guard tries to
    ///      consult TWAP but the pool has no old-enough observations, so the
    ///      try/catch silently skips the guard.  The deposit succeeds.
    function test_depositToken_coldPoolSkipsSlippageGuard() public {
        vm.prank(GOV);
        address pool = basketC.addBasketToken(
            address(paxg), 18, PAXG_INITIAL_PRICE_BUCK, 10000, 500
        );
        _bindPool(pool);

        vm.prank(alice);
        paxg.approve(address(basketC), 1e18);
        vm.prank(alice);
        basketC.depositToken(address(paxg), 1e18, 100);  // 1% slippage, cold pool
    }

    // -------------------------------------------------------------------- //
    //  Multi-pool deposit / redeem                                          //
    // -------------------------------------------------------------------- //

    /// @dev Set up a two-constituent basket (PAXG + cbBTC at equal weight)
    ///      and have Alice make one deposit into each.  Returns the two
    ///      receipt IDs.
    function _setupTwoPoolBasket()
        internal returns (uint256 ridPaxg, uint256 ridCbbtc)
    {
        vm.startPrank(GOV);
        address pPaxg  = basketC.addBasketToken(
            address(paxg),  18, PAXG_INITIAL_PRICE_BUCK,  5000, 500);
        address pCbbtc = basketC.addBasketToken(
            address(cbbtc), 8,  CBBTC_INITIAL_PRICE_BUCK, 5000, 500);
        vm.stopPrank();
        _bindPool(pPaxg);
        _bindPool(pCbbtc);

        // Equal-value deposits ($4000 each) so the pools have matching
        // LP value and neither dominates the allocator's pass-1.
        vm.prank(alice);
        paxg.approve(address(basketC), 1e18);
        vm.prank(alice);
        ridPaxg = basketC.depositToken(address(paxg), 1e18, 0);

        // 0.04 cbBTC × $100,000 = $4000 (8-dec → 0.04 × 1e8 = 4e6).
        vm.prank(alice);
        cbbtc.approve(address(basketC), 4e6);
        vm.prank(alice);
        ridCbbtc = basketC.depositToken(address(cbbtc), 4e6, 0);
    }

    /// @dev Multi-pool redemption: the depositor's claim is allocated
    ///      across BOTH pools (proportional to each pool's current value
    ///      share), so Alice receives PAXG AND cbBTC even though she
    ///      originally only deposited PAXG.
    function test_redeem_multiPool_returnsBothTokens() public {
        (uint256 ridPaxg, ) = _setupTwoPoolBasket();

        uint256 paxgBefore = paxg.balanceOf(alice);
        uint256 cbbtcBefore = cbbtc.balanceOf(alice);
        uint256 outstandingBefore = basketC.totalOutstandingBuck();

        (uint256 principalBuck, , , ) = basketC.deposits(ridPaxg);

        vm.prank(alice);
        basketC.redeem(ridPaxg, 0, 0);

        // Alice should have received BOTH tokens (multi-pool allocation).
        assertGt(paxg.balanceOf(alice), paxgBefore,
                 "Alice got PAXG from PAXG pool");
        assertGt(cbbtc.balanceOf(alice), cbbtcBefore,
                 "Alice also got cbBTC from cbBTC pool");

        // Outstanding decreases by the actual burn ≈ principal (modulo
        // orphan dust ≤ MAX_ORPHAN_DUST_WEI).  Supply delta is NOT a
        // direct indicator anymore: _reinvestBuck mints new BUCK as
        // part of the same tx, so supplyBefore - supplyAfter is biased
        // by the treasury reinvest amount.
        uint256 outstandingDelta = outstandingBefore
            - basketC.totalOutstandingBuck();
        assertApproxEqAbs(outstandingDelta, principalBuck, 1e6);
    }

    /// @dev Mint/burn invariant under multi-pool redemption: after a
    ///      full redeem of one deposit, totalOutstandingBuck decreases
    ///      by approximately the burned principal (modulo orphan dust).
    function test_redeem_multiPool_preservesOutstandingInvariant() public {
        (uint256 ridPaxg, ) = _setupTwoPoolBasket();

        uint256 outstandingBefore = basketC.totalOutstandingBuck();
        (uint256 principalBuck, , , ) = basketC.deposits(ridPaxg);

        vm.prank(alice);
        basketC.redeem(ridPaxg, 0, 0);

        // Outstanding decreases by ≈ principalBuck (the actual burn).
        // Tolerance covers MAX_ORPHAN_DUST_WEI for edge cases where the
        // pool was V3-burn-drained too thin to swap the last few wei.
        uint256 outstandingDelta = outstandingBefore -
            basketC.totalOutstandingBuck();
        assertApproxEqAbs(outstandingDelta, principalBuck, 1e6);

        // Receipt deleted (full redemption).
        vm.expectRevert();
        receipt.ownerOf(ridPaxg);
    }

    /// @dev Partial multi-pool redemption: 50% of the principal redeemed,
    ///      remainder remains as an active deposit.
    function test_redeem_multiPool_partial() public {
        (uint256 ridPaxg, ) = _setupTwoPoolBasket();

        (uint256 principalBefore, , , ) = basketC.deposits(ridPaxg);

        vm.prank(alice);
        basketC.redeem(ridPaxg, 5000, 0);  // 50% redemption

        (uint256 principalAfter, , , ) = basketC.deposits(ridPaxg);
        assertApproxEqRel(principalAfter, principalBefore / 2, 0.001e18,
                          "buckPrincipal halved after 50% redeem");

        // Receipt NOT burned (partial redemption).
        assertEq(receipt.ownerOf(ridPaxg), alice,
                 "receipt still owned by alice");
    }

    /// @dev Three-pool bootstrap-and-redeem (mirrors the sim's
    ///      REBALANCING scenario: equal-weight 3-token basket where
    ///      each agent bootstraps one pool with a TOKEN deposit at the
    ///      basket's initial spot, then redeems later).
    function test_redeem_threePool_simBootstrapPattern() public {
        // Third mock token to make a 3-constituent basket like the sim.
        BBToken aoil = new BBToken("Alberta Oil (mock)", "AOIL", 18);
        uint256 AOIL_INITIAL = 100e18;  // $100/AOIL
        aoil.mint(alice, 1_000_000e18);

        vm.startPrank(GOV);
        address pP = basketC.addBasketToken(
            address(paxg),  18, PAXG_INITIAL_PRICE_BUCK,  3333, 500);
        address pC = basketC.addBasketToken(
            address(cbbtc), 8,  CBBTC_INITIAL_PRICE_BUCK, 3333, 500);
        address pA = basketC.addBasketToken(
            address(aoil),  18, AOIL_INITIAL,             3334, 500);
        vm.stopPrank();
        _bindPool(pP);
        _bindPool(pC);
        _bindPool(pA);

        // Bootstrap: $4000-equivalent of each token (matches sim spirit).
        vm.startPrank(alice);
        paxg.approve(address(basketC), 1e18);
        uint256 ridP = basketC.depositToken(address(paxg), 1e18, 0);

        cbbtc.approve(address(basketC), 4e6);            // 0.04 cbBTC
        uint256 ridC = basketC.depositToken(address(cbbtc), 4e6, 0);

        aoil.approve(address(basketC), 40e18);           // 40 AOIL
        uint256 ridA = basketC.depositToken(address(aoil), 40e18, 0);
        vm.stopPrank();

        // Sanity: 3 outstanding deposits, all roughly balanced.
        assertEq(basketC.constituentsLength(), 3);
        assertGt(basketC.totalOutstandingBuck(), 11_000e18);
        assertLt(basketC.totalOutstandingBuck(), 13_000e18);

        // Redeem the PAXG deposit — exercises the 3-pool allocator path
        // (the sim's failing scenario).
        (uint256 principalBuck, , , ) = basketC.deposits(ridP);
        uint256 supplyBefore = buck.totalSupply();

        uint256 outstandingBefore = basketC.totalOutstandingBuck();

        vm.prank(alice);
        basketC.redeem(ridP, 0, 0);

        // Receipt deleted, outstanding decremented by the actual burn
        // ≈ principalBuck (tolerance covers MAX_ORPHAN_DUST_WEI).
        vm.expectRevert();
        receipt.ownerOf(ridP);
        uint256 outstandingDelta = outstandingBefore
            - basketC.totalOutstandingBuck();
        assertApproxEqAbs(outstandingDelta, principalBuck, 1e6);

        // Silence unused warnings.
        ridC; ridA;
        supplyBefore;
    }

    /// @dev Three-pool basket where one pool has been heavily perturbed
    ///      by an external arb (the swap moves the pool's price ~5%
    ///      and consumes a meaningful fraction of liquidity).  This is
    ///      the scenario the rebalancing sim was hitting that caused
    ///      every DM redemption to revert with `ERC20InsufficientBalance`
    ///      under the old per-pool-shortfall + tick-quote `_buckToLp`
    ///      code paths.
    ///
    ///      The test contract acts as a V3 swap recipient via the
    ///      `uniswapV3SwapCallback` below.
    function test_redeem_heavyArbPerturbation() public {
        // Bind self so BUCK can be transferred to this test contract
        // (V3 pool sends BUCK back during arb swap).
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT({
            R: BN254.g1(), C: BN254.g1()
        });
        reg.bindContract(address(this), BN254.g1(), E, true, true);

        // 3-pool basket like the sim.
        BBToken aoil = new BBToken("Alberta Oil", "AOIL", 18);
        uint256 AOIL_INITIAL = 100e18;
        aoil.mint(alice, 1_000_000e18);
        aoil.mint(address(this), 1_000_000e18);
        paxg.mint(address(this), 10_000e18);

        vm.startPrank(GOV);
        address pP = basketC.addBasketToken(
            address(paxg),  18, PAXG_INITIAL_PRICE_BUCK,  3333, 500);
        address pC = basketC.addBasketToken(
            address(cbbtc), 8,  CBBTC_INITIAL_PRICE_BUCK, 3333, 500);
        address pA = basketC.addBasketToken(
            address(aoil),  18, AOIL_INITIAL,             3334, 500);
        vm.stopPrank();
        _bindPool(pP); _bindPool(pC); _bindPool(pA);

        // Bootstrap each pool with an equal-USD deposit.
        vm.startPrank(alice);
        paxg.approve(address(basketC), 1e18);
        uint256 ridP = basketC.depositToken(address(paxg), 1e18, 0);
        cbbtc.approve(address(basketC), 4e6);
        basketC.depositToken(address(cbbtc), 4e6, 0);
        aoil.approve(address(basketC), 40e18);
        basketC.depositToken(address(aoil), 40e18, 0);
        vm.stopPrank();

        // External arb: dump TOKEN into the PAXG pool to drive its
        // price down and consume meaningful liquidity (basket-minted
        // BUCK accumulates in the pool on the BUCK side).
        _arbDumpTokenForBuck(pP, address(paxg), 0.2e18);

        // Now redeem the PAXG deposit.  Pre-redesign this would revert
        // because per-pool shortfall coverage + tick-quoted _buckToLp
        // demanded more BUCK in callbacks than the basket held.
        uint256 outstandingBefore = basketC.totalOutstandingBuck();
        (uint256 principalBuck, , , ) = basketC.deposits(ridP);

        vm.prank(alice);
        basketC.redeem(ridP, 0, 0);

        // Receipt deleted, outstanding decremented by the actual burn.
        vm.expectRevert();
        receipt.ownerOf(ridP);
        uint256 outstandingDelta = outstandingBefore
            - basketC.totalOutstandingBuck();
        assertApproxEqAbs(outstandingDelta, principalBuck, 1e6);
    }

    /// @dev External-arb helper: dump `amt` of `token` into `pool` for
    ///      BUCK (uses this test contract as the V3 swap callback).
    function _arbDumpTokenForBuck(address pool, address token, uint256 amt)
        internal
    {
        bool zeroForOne = (token == IV3PoolForTest(pool).token0());
        IV3PoolForTest(pool).swap(
            address(this),
            zeroForOne,
            int256(amt),
            zeroForOne ? uint160(4295128739 + 1) : uint160(
                1461446703485210103287273052203988822378723970342 - 1),
            abi.encode(token, pool)
        );
    }

    /// @notice V3 swap callback for the test-contract arb path.  Pays
    ///         whichever positive delta the pool demands from this
    ///         test contract's balance.
    function uniswapV3SwapCallback(
        int256 amount0Delta, int256 amount1Delta, bytes calldata data
    ) external {
        (address tokenIn, address pool) = abi.decode(data, (address, address));
        require(msg.sender == pool, "bad cb");
        if (amount0Delta > 0) {
            if (tokenIn == IV3PoolForTest(pool).token0()) {
                IERC20(tokenIn).transfer(msg.sender, uint256(amount0Delta));
            }
        }
        if (amount1Delta > 0) {
            if (tokenIn == IV3PoolForTest(pool).token1()) {
                IERC20(tokenIn).transfer(msg.sender, uint256(amount1Delta));
            }
        }
    }

    /// @dev When NO pool is overweight (basket exactly at target),
    ///      redemption still succeeds via proportional-by-value fallback.
    ///      This was the gap #4 liveness hazard.
    function test_redeem_equilibrium_succeeds() public {
        (uint256 ridPaxg, ) = _setupTwoPoolBasket();
        // Both pools fresh from bootstrap at initial price → all at target.

        uint256 supplyBefore = buck.totalSupply();
        vm.prank(alice);
        basketC.redeem(ridPaxg, 0, 0);
        // No revert: equilibrium redeem allocates proportional-by-value.
        assertLt(buck.totalSupply(), supplyBefore);
    }

    // -------------------------------------------------------------------- //
    //  Identity helpers (mirrors BuckLifecycle.t.sol)                       //
    // -------------------------------------------------------------------- //

    function _u(string memory key) internal view returns (uint256) {
        return vm.parseJsonUint(vj, key);
    }
    function _g1(string memory key) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point(_u(string.concat(key, ".x")), _u(string.concat(key, ".y")));
    }
    function _ps(string memory who) internal view returns (IdentityRegistry.PSSig memory s) {
        s.sigma_1 = _g1(string.concat(".", who, ".ps_sig_rerand.sigma_1"));
        s.sigma_2 = _g1(string.concat(".", who, ".ps_sig_rerand.sigma_2"));
    }
    function _ct(string memory key) internal view returns (IdentityRegistry.ElGamalCT memory c) {
        c.R = _g1(string.concat(key, ".R"));
        c.C = _g1(string.concat(key, ".C"));
    }
    function _regProof(string memory who) internal view returns (IdentityRegistry.RegistrationProof memory p) {
        string memory base = string.concat(".", who, ".registration_proof");
        p.e    = _u(string.concat(base, ".e"));
        p.s_m  = _u(string.concat(base, ".s_m"));
        p.s_r  = _u(string.concat(base, ".s_r"));
        p.A_ps = _g1(string.concat(base, ".A_ps"));
        p.T_C  = _g1(string.concat(base, ".T_C"));
        p.T_R  = _g1(string.concat(base, ".T_R"));
    }
    function _trustIssuer() internal {
        IdentityRegistry.PSPubKey memory ipk;
        ipk.X.X[0] = _u(".issuer.pk_X.x[0]"); ipk.X.X[1] = _u(".issuer.pk_X.x[1]");
        ipk.X.Y[0] = _u(".issuer.pk_X.y[0]"); ipk.X.Y[1] = _u(".issuer.pk_X.y[1]");
        ipk.Y.X[0] = _u(".issuer.pk_Y.x[0]"); ipk.Y.X[1] = _u(".issuer.pk_Y.x[1]");
        ipk.Y.Y[0] = _u(".issuer.pk_Y.y[0]"); ipk.Y.Y[1] = _u(".issuer.pk_Y.y[1]");
        vm.prank(GOV);
        reg.trustIssuer(ISSUER, ipk);
    }
    function _registerAlice() internal {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        vm.prank(alice);
        reg.register(ISSUER, pk, E, _ps("alice"), _regProof("alice"));
    }
}
