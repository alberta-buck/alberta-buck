// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IUniswapV3Pool}    from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {IERC20}            from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";
import {IBuckKController}    from "../IBuckKController.sol";
import {BasketMath}         from "./BasketMath.sol";
import {BuckBasketReceipt}  from "./BuckBasketReceipt.sol";
import {IBasketRebalancer}  from "./IBasketRebalancer.sol";

interface IUniswapV3Factory {
    function createPool(address tokenA, address tokenB, uint24 fee) external returns (address pool);
    function getPool(address tokenA, address tokenB, uint24 fee) external view returns (address pool);
    function feeAmountTickSpacing(uint24 fee) external view returns (int24);
}

interface IUniswapV3MintCallback {
    function uniswapV3MintCallback(uint256 amount0Owed, uint256 amount1Owed, bytes calldata data) external;
}

interface IUniswapV3SwapCallback {
    function uniswapV3SwapCallback(int256, int256, bytes calldata) external;
}

interface IBuckMintBurn {
    function mintFromBasket(address to, uint256 amount) external;
    function burnFromBasket(uint256 amount) external;
    function balanceOf(address) external view returns (uint256);
}

/// @title BuckBasketProRata -- pro-rata exit + treasury-split direct-mint orchestrator.
///
/// @notice Custodies one full-range Buck-owned Uniswap V3 LP position per
///         TOKEN/BUCK constituent pool and mints BUCK against deposited TOKEN.
///         Redemption is a **pro-rata in-kind exit**: a receipt redeeming a
///         fraction `θ = redeemBuck / totalOutstandingBuck` withdraws `θ` of
///         every pool's *depositor-owned* liquidity, burns its BUCK principal,
///         and returns the TOKEN side.  Because it only ever removes the
///         redeemer's own slice, it is solvent at any individual pool depth
///         (thin pools simply yield thin slices); the only revert is the
///         transient whole-basket "underwater" case (NAV < principal under deep
///         BUCK deflation), which the BuckCredit/BuckK backstop quenches.
///
/// # Two kinds of liquidity
///
///   * **Depositor liquidity** -- backs outstanding receipts; the pro-rata
///     claim base.  `depositorL = positionL - treasuryLiquidity`.
///   * **Treasury liquidity / pending BUCK** -- NAV above `totalOutstandingBuck`
///     (AMM fees, retained BUCK profit, unclaimed external-arb BUCK).  Funds
///     BUCK-system R&D/ops; re-LP'd by the rebalancer; never part of a
///     depositor's pro-rata claim.
///
/// # Profit split (realized at redeem; `treasuryBp`, default 50/50)
///
///   * Depositor keeps 100% of the withdrawn TOKEN side + `(1-treasuryBp)` of
///     any BUCK profit (`Bw - R`).
///   * Treasury keeps `treasuryBp` of BUCK profit (accrued to `treasuryBuckPending`).
///   * The principal burn is *senior* to the split: under deflation it is
///     covered first from withdrawn BUCK, then from selling the depositor's
///     own TOKEN; the treasury never takes TOKEN.
///
/// # Scaffold notes
///
///   * Shortfall conversion currently routes through the internal TOKEN/BUCK
///     pool (`_coverShortfall`).  FX multi-hop routing via `rebalancer` +
///     Uniswap `ISwapRouter` is a follow-up; the `IBasketRebalancer` wiring is
///     already in place.
///   * `RedeemPlan` (optimizer / specific-token API) and BUCK-side deposits are
///     stubbed pending the routing pass.
///   * Treasury re-LP (`sweepTreasury`) recycles accrued profit into the most
///     underweight pool as treasury-owned liquidity ("buy low"), swapping the
///     TOKEN side on the internal pool for now (FX routing is the follow-up).
contract BuckBasketProRata is IUniswapV3MintCallback, IUniswapV3SwapCallback {

    // --- Constituents ----------------------------------------------------- //

    struct Constituent {
        address token;
        uint8   decimals;
        uint24  feeTier;
        address pool;
        int24   tickLower;
        int24   tickUpper;
        bool    buckIsToken0;
        uint256 targetWeightBp;       // declared weight, sums to 10000 across all
        uint256 basketAmount;         // 18-dec; Σ basketAmount*price = 1 BUCK at init
        uint256 initialPriceInBuck;   // 18-dec
        uint128 treasuryLiquidity;    // treasury-owned L slice in this pool
    }

    Constituent[] public constituents;
    mapping(address => uint256) public indexOf;   // token -> 1+index (0 = absent)

    struct Deposit {
        uint256 buckPrincipal;     // BUCK minted at deposit (the share unit)
        uint256 tokenPrincipal;    // native-dec TOKEN deposited (ROI telemetry)
        address token;             // original deposit token
        uint64  depositTime;
    }
    mapping(uint256 => Deposit) public deposits;

    /// @notice Total outstanding BUCK principal == Σ buckPrincipal.
    uint256 public totalOutstandingBuck;

    /// @notice Treasury BUCK profit awaiting re-LP by the rebalancer.
    uint256 public treasuryBuckPending;

    /// @notice BUCK profit share to treasury, in basis points (default 50%).
    uint16 public treasuryBp;

    // --- Wiring ----------------------------------------------------------- //

    IBuckMintBurn     public immutable buck;
    BuckBasketReceipt public immutable receipt;
    IBuckKController  public immutable controller;
    IUniswapV3Factory public immutable v3Factory;
    IBasketRebalancer public rebalancer;
    address           public governance;

    uint24  public defaultFeeTier;
    uint32  public twapWindow;
    uint16  public observationCardinality;
    uint256 public defaultMaxDeviationBp;
    uint256 public minSeedLiquidity;

    // --- Constants -------------------------------------------------------- //

    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO =
        1461446703485210103287273052203988822378723970342;
    uint16  public  constant  MAX_TREASURY_BP = 9000;      // cap governance take
    /// @notice Residual burn gap tolerated as V3 burn-rounding dust (not the
    ///         underwater case): single-depositor full redemption drains its
    ///         pool, so the last few wei can't be swap-covered.  The principal
    ///         is still fully retired from `totalOutstandingBuck`; the unburned
    ///         dust is a bounded supply leak (≤ MAX_DUST_WEI per redemption).
    uint256 internal constant MAX_DUST_WEI = 1e9;          // 1e-9 BUCK
    /// @notice Floor for treasury re-LP: below this, profit stays as pending
    ///         BUCK (V3 mint of a dust position can fail / waste gas).
    uint256 internal constant MIN_REINVEST_BUCK = 1e15;    // 0.001 BUCK

    // --- Callback guards -------------------------------------------------- //
    address internal _callbackPool;
    address internal _swapCallbackPool;

    // --- Events ----------------------------------------------------------- //

    event ConstituentAdded(address indexed token, uint256 weightBp, uint256 initialPriceInBuck, address pool);
    event Deposited(address indexed who, uint256 indexed receiptId, address token, uint256 tokenAmount, uint256 buckMinted, uint128 liquidity);
    event Redeemed(address indexed who, uint256 indexed receiptId, uint256 burned, uint256 depositorBuck, uint256 treasuryBuck, uint256 remainingBp);
    event TreasuryAccrued(uint256 amount, uint256 pending);
    event TreasuryWithdrawn(address indexed to, uint256 amount);
    event TreasuryReinvested(uint256 indexed poolIdx, uint256 buckConsumed, uint128 liquidity);
    event RebalancerSet(address indexed rebalancer);
    event TreasuryBpSet(uint16 treasuryBp);

    constructor(
        address _buck,
        address _controller,
        address _v3Factory,
        address _governance,
        uint24  _defaultFeeTier,
        uint32  _twapWindow,
        uint16  _observationCardinality,
        uint256 _defaultMaxDeviationBp,
        uint256 _minSeedLiquidity
    ) {
        require(_buck != address(0) && _controller != address(0)
                && _v3Factory != address(0) && _governance != address(0), "zero addr");
        buck                   = IBuckMintBurn(_buck);
        controller             = IBuckKController(_controller);
        v3Factory              = IUniswapV3Factory(_v3Factory);
        governance             = _governance;
        defaultFeeTier         = _defaultFeeTier;
        twapWindow             = _twapWindow;
        observationCardinality = _observationCardinality;
        defaultMaxDeviationBp  = _defaultMaxDeviationBp;
        minSeedLiquidity       = _minSeedLiquidity;
        treasuryBp             = 5000;

        receipt = new BuckBasketReceipt(address(this));
    }

    modifier onlyGov() {
        require(msg.sender == governance, "Not governance");
        _;
    }

    // --- Governance ------------------------------------------------------- //

    function setGovernance(address _governance) external onlyGov {
        require(_governance != address(0), "gov=0");
        governance = _governance;
    }

    function setRebalancer(address _rebalancer) external onlyGov {
        rebalancer = IBasketRebalancer(_rebalancer);
        emit RebalancerSet(_rebalancer);
    }

    function setTreasuryBp(uint16 _treasuryBp) external onlyGov {
        require(_treasuryBp <= MAX_TREASURY_BP, "treasuryBp too high");
        treasuryBp = _treasuryBp;
        emit TreasuryBpSet(_treasuryBp);
    }

    /// @notice Draw accumulated treasury BUCK profit to fund operations.
    function treasuryWithdraw(address to, uint256 amount) external onlyGov {
        require(to != address(0), "to=0");
        require(amount <= treasuryBuckPending, "exceeds pending");
        treasuryBuckPending -= amount;
        IERC20(address(buck)).transfer(to, amount);
        emit TreasuryWithdrawn(to, amount);
    }

    /// @notice Recycle accrued treasury BUCK profit into the most underweight
    ///         pool as treasury-owned liquidity -- the "buy low" leg, decoupled
    ///         from redemption.  Permissionless (any keeper); no-op below the
    ///         re-LP floor.
    function sweepTreasury() external {
        if (treasuryBuckPending >= MIN_REINVEST_BUCK) {
            _reinvestTreasury(treasuryBuckPending);
        }
    }

    /// @notice Treasury-owned liquidity in constituent `i` (excluded from the
    ///         depositor pro-rata claim base).
    function treasuryLiquidityOf(uint256 i) external view returns (uint128) {
        return constituents[i].treasuryLiquidity;
    }

    /// @notice Register a basket constituent.  Renormalizes existing declared
    ///         weights to keep Σ targetWeightBp == 10000, preserving each
    ///         existing constituent's price (basketAmount scaled by the same
    ///         ratio, not re-priced at current spot).
    function addConstituent(
        address token,
        uint8   decimals,
        uint256 initialPriceInBuck,
        uint256 weightBp,
        uint24  feeTier
    ) external onlyGov returns (address pool) {
        require(token != address(0) && token != address(buck), "bad token");
        require(weightBp <= 10000, "bad weight");
        require(indexOf[token] == 0, "already present");
        require(initialPriceInBuck > 0, "bad price");

        uint256 N = constituents.length + 1;
        uint256 newW = weightBp > 0 ? weightBp : 10000 / N;
        require(newW <= 10000 && newW > 0, "bad target weight");

        uint256 oldW = 10000 - newW;
        uint256 oldSumW = 0;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            c.targetWeightBp = UniswapV3OracleLib.mulDiv(c.targetWeightBp, oldW, 10000);
            require(c.targetWeightBp > 0 && c.targetWeightBp < 10000, "invalid rescale");
            oldSumW += c.targetWeightBp;
            c.basketAmount = UniswapV3OracleLib.mulDiv(c.basketAmount, oldW, 10000);
        }
        if (constituents.length > 0) {
            require(oldSumW < 10000, "scaled weights incorrect");
            newW = 10000 - oldSumW;
        }

        uint256 weightUnit  = UniswapV3OracleLib.mulDiv(newW, 1e18, 10000);
        uint256 basketAmount = UniswapV3OracleLib.mulDiv(weightUnit, 1e18, initialPriceInBuck);

        pool = _findOrCreatePool(token, feeTier);
        bool buckIsToken0 = address(buck) < token;
        uint160 sqrtPriceX96 = BasketMath.sqrtPriceFromBuckRate(
            buckIsToken0, initialPriceInBuck, decimals);
        try IUniswapV3Pool(pool).initialize(sqrtPriceX96) {} catch {}
        IUniswapV3Pool(pool).increaseObservationCardinalityNext(observationCardinality);

        int24 spacing = v3Factory.feeAmountTickSpacing(feeTier);
        (int24 tickLower, int24 tickUpper) = BasketMath.fullRangeTicks(spacing);

        constituents.push(Constituent({
            token: token,
            decimals: decimals,
            feeTier: feeTier,
            pool: pool,
            tickLower: tickLower,
            tickUpper: tickUpper,
            buckIsToken0: buckIsToken0,
            targetWeightBp: newW,
            basketAmount: basketAmount,
            initialPriceInBuck: initialPriceInBuck,
            treasuryLiquidity: 0
        }));
        indexOf[token] = constituents.length;

        controller.reprime();
        emit ConstituentAdded(token, newW, initialPriceInBuck, pool);
    }

    function constituentsLength() external view returns (uint256) {
        return constituents.length;
    }

    // --- Process variable for the controller ------------------------------ //

    function basketValueInBuck() external view returns (int256) {
        if (constituents.length == 0) return int256(1e18);
        int256 total = 0;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            uint256 priceInBuck = _readPoolPrice(c, twapWindow);
            total += int256(UniswapV3OracleLib.mulDiv(c.basketAmount, priceInBuck, 1e18));
        }
        return total;
    }

    // --- Direct mint ------------------------------------------------------ //

    /// @notice Deposit a registered basket TOKEN: mint BUCK at the pool's spot
    ///         price and LP the (TOKEN, BUCK) pair full-range.  Returns an
    ///         ERC-721 receipt.
    /// @dev    BUCK-side deposits (underweight routing) are a follow-up.
    function depositToken(address token, uint256 tokenAmount, uint256 maxDeviationBp)
        external returns (uint256 receiptId)
    {
        require(tokenAmount > 0, "amount=0");
        require(token != address(buck), "BUCK deposit: TODO");
        uint256 idx = indexOf[token];
        require(idx > 0, "not in basket");
        Constituent storage c = constituents[idx - 1];

        _enforceSlippageGuard(c, _readPoolPrice(c, 0), maxDeviationBp);
        IERC20(token).transferFrom(msg.sender, address(this), tokenAmount);

        // Bind liquidity to the exact TOKEN deposited and compute the floor
        // partner BUCK for that L at the current sqrtP.  This is the receipt's
        // principal; a full redemption recovers it modulo a couple wei of V3
        // burn rounding (absorbed by MAX_DUST_WEI on redeem).
        (uint160 sqrtP,,,,,,) = IUniswapV3Pool(c.pool).slot0();
        uint160 sqrtLow  = UniswapV3OracleLib.getSqrtRatioAtTick(c.tickLower);
        uint160 sqrtHigh = UniswapV3OracleLib.getSqrtRatioAtTick(c.tickUpper);
        uint128 liquidity;
        uint256 buckToMint;
        if (c.buckIsToken0) {
            liquidity  = UniswapV3OracleLib.getLiquidityForAmount1(sqrtLow, sqrtP, tokenAmount);
            buckToMint = UniswapV3OracleLib.getAmount0ForLiquidity(sqrtP, sqrtHigh, liquidity);
        } else {
            liquidity  = UniswapV3OracleLib.getLiquidityForAmount0(sqrtP, sqrtHigh, tokenAmount);
            buckToMint = UniswapV3OracleLib.getAmount1ForLiquidity(sqrtLow, sqrtP, liquidity);
        }
        require(liquidity > 0, "L=0");
        require(buckToMint > 0, "buck=0");
        if (_isFirstPositionInPool(c)) {
            require(liquidity >= minSeedLiquidity, "seed too small");
        }

        // Mint principal + 1 wei: V3 mint rounds the owed BUCK up past the floor
        // estimate by at most 1 wei, so this guarantees the callback is covered.
        // Any unconsumed wei stays idle in the basket (a negligible, bounded
        // buffer) -- far simpler than over-minting and refunding the remainder.
        buck.mintFromBasket(address(this), buckToMint + 1);
        _callbackPool = c.pool;
        IUniswapV3Pool(c.pool).mint(
            address(this), c.tickLower, c.tickUpper, liquidity, abi.encode(token));
        _callbackPool = address(0);

        receiptId = receipt.mint(msg.sender);
        deposits[receiptId] = Deposit({
            buckPrincipal: buckToMint,
            tokenPrincipal: tokenAmount,
            token: token,
            depositTime: uint64(block.timestamp)
        });
        totalOutstandingBuck += buckToMint;

        controller.compute();
        emit Deposited(msg.sender, receiptId, token, tokenAmount, buckToMint, liquidity);
    }

    // --- Redemption (pro-rata in-kind exit) ------------------------------- //

    /// @notice Redeem all (`redeemBp == 0`) or a basis-point fraction of a
    ///         receipt.  Withdraws θ of every pool's depositor-owned liquidity,
    ///         burns the BUCK principal (covering any deflation shortfall from
    ///         the withdrawn TOKEN), splits BUCK profit with the treasury, and
    ///         returns the TOKEN side to the redeemer.
    function redeem(uint256 receiptId, uint256 redeemBp) public {
        require(receipt.ownerOf(receiptId) == msg.sender, "not owner");
        Deposit memory d = deposits[receiptId];
        require(d.buckPrincipal > 0, "empty deposit");
        uint256 redeemShare = redeemBp == 0 ? 10000 : redeemBp;
        require(redeemShare <= 10000, "bp>10000");
        require(totalOutstandingBuck > 0, "no outstanding");

        uint256 R = redeemShare == 10000
            ? d.buckPrincipal
            : d.buckPrincipal * redeemShare / 10000;
        require(R > 0, "redeem zero");

        // Phase 1: pro-rata withdraw θ = R/totalOutstandingBuck of every pool's
        // depositor liquidity (computed per pool as depositorL*R/outstanding to
        // avoid a fixed-point intermediate).
        uint256 N = constituents.length;
        uint256[] memory perPoolTok = new uint256[](N);
        uint256 Bw = 0;
        bool anyWithdrawn = false;
        for (uint256 i = 0; i < N; i++) {
            Constituent storage c = constituents[i];
            uint128 totalL = _positionLiquidity(c);
            if (totalL <= c.treasuryLiquidity) continue;
            uint128 depositorL = totalL - c.treasuryLiquidity;
            uint128 burnL = uint128(uint256(depositorL) * R / totalOutstandingBuck);
            if (burnL == 0) continue;
            (uint256 tok, uint256 b) = _decreaseAndCollect(c, burnL);
            perPoolTok[i] = tok;
            Bw += b;
            anyWithdrawn = true;
        }
        require(anyWithdrawn, "no LP withdrawn");

        // Phase 2: settle the burn against withdrawn BUCK, covering any
        // deflation shortfall by selling withdrawn TOKEN.
        uint256 depositorBuck = 0;
        uint256 treasuryBuck  = 0;
        uint256 have;
        bool    isShort;
        if (Bw >= R) {
            have = Bw;
        } else {
            have = Bw + _coverShortfall(perPoolTok, R - Bw);
            isShort = true;
        }
        uint256 burned = have >= R ? R : have;
        // Gaps beyond V3 burn-rounding dust mean the whole basket is
        // underwater (NAV < principal under deep deflation): revert, wait for
        // the BuckCredit/BuckK backstop.
        require(R - burned <= MAX_DUST_WEI, "underwater");
        uint256 excess = have - burned;
        if (excess > 0) {
            if (isShort) {
                // No depositor BUCK profit under deflation; over-swap excess
                // is treasury equity.
                treasuryBuck = excess;
            } else {
                (treasuryBuck, depositorBuck) = BasketMath.splitProfit(excess, treasuryBp);
            }
        }

        // Phase 3: burn principal, pay out.
        if (burned > 0) buck.burnFromBasket(burned);
        if (treasuryBuck > 0) {
            treasuryBuckPending += treasuryBuck;
            emit TreasuryAccrued(treasuryBuck, treasuryBuckPending);
        }
        if (depositorBuck > 0) {
            IERC20(address(buck)).transfer(msg.sender, depositorBuck);
        }
        for (uint256 i = 0; i < N; i++) {
            if (perPoolTok[i] > 0) {
                IERC20(constituents[i].token).transfer(msg.sender, perPoolTok[i]);
            }
        }

        // Phase 4: finalize bookkeeping.
        if (redeemShare == 10000) {
            delete deposits[receiptId];
            receipt.burn(receiptId);
        } else {
            deposits[receiptId].buckPrincipal -= R;
            deposits[receiptId].tokenPrincipal -= d.tokenPrincipal * redeemShare / 10000;
        }
        totalOutstandingBuck -= R;

        controller.compute();
        emit Redeemed(
            msg.sender, receiptId, burned, depositorBuck, treasuryBuck,
            redeemShare == 10000 ? 0 : 10000 - redeemShare);
    }

    // --- Shortfall cover (scaffold: internal TOKEN/BUCK pools) ------------ //

    /// @notice Raise `shortfall` BUCK by greedily swapping the withdrawn TOKEN
    ///         (most-TOKEN pool first) into BUCK on the internal pools.
    ///         Mutates `perPoolTok` to reflect TOKEN spent.  Returns BUCK
    ///         gained; the caller checks `Bw + gained >= R` (else underwater).
    /// @dev    FX multi-hop routing via `rebalancer` is the follow-up; for now
    ///         this is internal-pool only.
    function _coverShortfall(uint256[] memory perPoolTok, uint256 shortfall)
        internal returns (uint256 gained)
    {
        uint256 N = constituents.length;
        uint256 remaining = shortfall;
        // A near-exact per-pool estimate means one pass usually clears a pool;
        // the cap allows a couple of correction passes and multi-pool spread.
        uint256 maxPasses = N * 2 + 4;
        for (uint256 pass = 0; pass < maxPasses && remaining > 0; pass++) {
            // Pick the pool with the most withdrawn TOKEN that still has
            // in-range liquidity to swap against.  A pool we just fully drained
            // (single-depositor full redeem) has liquidity()==0 and is skipped;
            // its TOKEN simply flows to the depositor unconverted.
            uint256 bestIdx = type(uint256).max;
            uint256 bestTok = 0;
            for (uint256 i = 0; i < N; i++) {
                if (perPoolTok[i] > bestTok
                    && IUniswapV3Pool(constituents[i].pool).liquidity() > 0) {
                    bestIdx = i; bestTok = perPoolTok[i];
                }
            }
            if (bestIdx == type(uint256).max) break;

            Constituent storage c = constituents[bestIdx];
            uint256 tokenIn = _tokenInForBuckOut(c, remaining);
            if (tokenIn == 0 || tokenIn > perPoolTok[bestIdx]) {
                tokenIn = perPoolTok[bestIdx];   // sell all available here
            }
            (uint256 spent, uint256 received) = _swapTokenForBuckExactIn(c, tokenIn);
            perPoolTok[bestIdx] -= spent;
            gained += received;
            remaining = received >= remaining ? 0 : remaining - received;
            if (received == 0) break;   // no progress; avoid spinning
        }
    }

    /// @notice TOKEN-in to extract `buckOut` BUCK from `c.pool`, via the
    ///         constant-product exact-output formula on the pool's *actual*
    ///         reserves (full-range V3 ⇒ pool balances are the virtual
    ///         reserves), inflated by the pool fee.  Near-exact, so a single
    ///         exact-input swap clears the shortfall even on a thinned pool.
    ///         Returns `type(uint256).max` when the pool can't cover `buckOut`
    ///         (caller falls back to selling all available TOKEN).
    function _tokenInForBuckOut(Constituent storage c, uint256 buckOut)
        internal view returns (uint256 tokenIn)
    {
        uint256 tokRes  = IERC20(c.token).balanceOf(c.pool);
        uint256 buckRes = IERC20(address(buck)).balanceOf(c.pool);
        if (buckRes <= buckOut || tokRes == 0) return type(uint256).max;
        // ideal (fee-free) tokenIn = tokRes * buckOut / (buckRes - buckOut)
        uint256 ideal = UniswapV3OracleLib.mulDiv(tokRes, buckOut, buckRes - buckOut);
        // inflate by 1/(1-fee); +1 wei round-up so the swap delivers ≥ buckOut.
        tokenIn = UniswapV3OracleLib.mulDiv(ideal, 1e6, 1e6 - c.feeTier) + 1;
    }

    /// @notice Exact-input TOKEN→BUCK swap on `c.pool`.
    function _swapTokenForBuckExactIn(Constituent storage c, uint256 tokenIn)
        internal returns (uint256 spent, uint256 received)
    {
        require(tokenIn > 0, "tokenIn=0");
        _swapCallbackPool = c.pool;
        (int256 d0, int256 d1) = IUniswapV3Pool(c.pool).swap(
            address(this),
            !c.buckIsToken0,                                  // zeroForOne for TOKEN→BUCK
            int256(tokenIn),                                  // positive ⇒ exact-input
            c.buckIsToken0 ? MAX_SQRT_RATIO - 1 : MIN_SQRT_RATIO + 1,
            abi.encode(c.token)
        );
        _swapCallbackPool = address(0);
        int256 buckDelta = c.buckIsToken0 ? d0 : d1;
        int256 tokDelta  = c.buckIsToken0 ? d1 : d0;
        require(buckDelta <= 0 && tokDelta >= 0, "swap delta sign");
        spent    = uint256(tokDelta);
        received = uint256(-buckDelta);
    }

    // --- Treasury re-LP (recycle-to-buy-low) ------------------------------ //

    /// @notice Convert `buckAmount` treasury BUCK into a treasury-owned LP
    ///         position in the most underweight pool: swap ~half for the pool's
    ///         TOKEN, LP both sides, and tag the minted L as treasury.  Only
    ///         the BUCK actually consumed leaves `treasuryBuckPending`; any
    ///         remainder stays pending for the next sweep.
    /// @dev    The TOKEN-side swap uses the internal BUCK/TOKEN pool.  When an
    ///         FX route is registered this is where the rebalancer plugs in a
    ///         deeper external path (see `BASKET-REDESIGN.md` §7).
    function _reinvestTreasury(uint256 buckAmount) internal {
        uint256 i = _mostUnderweightPool();
        Constituent storage c = constituents[i];

        (uint256 buckSpent, uint256 tok) = _swapBuckForTokenExactIn(c, buckAmount / 2);
        uint256 buckForLp = buckAmount - buckSpent;        // remainder pairs with TOKEN

        uint128 liquidity = _liquidityForAmounts(c, tok, buckForLp);
        require(liquidity > 0, "reinvest L=0");

        _callbackPool = c.pool;
        (uint256 a0, uint256 a1) = IUniswapV3Pool(c.pool).mint(
            address(this), c.tickLower, c.tickUpper, liquidity, abi.encode(c.token));
        _callbackPool = address(0);

        c.treasuryLiquidity += liquidity;
        uint256 consumed = buckSpent + (c.buckIsToken0 ? a0 : a1);   // swap + LP BUCK side
        treasuryBuckPending -= consumed;
        emit TreasuryReinvested(i, consumed, liquidity);
    }

    /// @notice Exact-input BUCK→TOKEN swap on `c.pool`.
    function _swapBuckForTokenExactIn(Constituent storage c, uint256 buckIn)
        internal returns (uint256 spent, uint256 received)
    {
        require(buckIn > 0, "buckIn=0");
        _swapCallbackPool = c.pool;
        (int256 d0, int256 d1) = IUniswapV3Pool(c.pool).swap(
            address(this),
            c.buckIsToken0,                                  // zeroForOne for BUCK→TOKEN
            int256(buckIn),                                  // positive ⇒ exact-input
            c.buckIsToken0 ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1,
            abi.encode(c.token)
        );
        _swapCallbackPool = address(0);
        int256 buckDelta = c.buckIsToken0 ? d0 : d1;
        int256 tokDelta  = c.buckIsToken0 ? d1 : d0;
        require(buckDelta >= 0 && tokDelta <= 0, "swap delta sign");
        spent    = uint256(buckDelta);
        received = uint256(-tokDelta);
    }

    /// @notice Index of the pool most underweight by value/target ratio (empty
    ///         pools, ratio 0, sort first).  Target = basketAmount * spot price.
    function _mostUnderweightPool() internal view returns (uint256 idx) {
        int256 best = type(int256).max;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            uint256 v = _poolLpValue(c);
            uint256 p = _readPoolPrice(c, 0);
            uint256 tgt = UniswapV3OracleLib.mulDiv(c.basketAmount, p, 1e18);
            int256 ratio = tgt > 0 ? int256(v * 1e18 / tgt) : type(int256).max;
            if (ratio < best) { best = ratio; idx = i; }
        }
    }

    /// @notice BUCK value of a pool's reserves (full-range ⇒ pool balances are
    ///         the virtual reserves).  0 if unseeded.
    function _poolLpValue(Constituent storage c) internal view returns (uint256 valueBuck) {
        uint256 tokBal = IERC20(c.token).balanceOf(c.pool);
        if (tokBal == 0) return 0;
        uint256 spotPrice = _readPoolPrice(c, 0);
        uint256 buckBal = IERC20(address(buck)).balanceOf(c.pool);
        valueBuck = UniswapV3OracleLib.mulDiv(tokBal, spotPrice, 10 ** c.decimals) + buckBal;
    }

    /// @notice V3 liquidity for (tokenAmount, buckAmount) at the pool's current
    ///         price -- min of the two sides (leftover stays in the basket).
    function _liquidityForAmounts(Constituent storage c, uint256 tokenAmount, uint256 buckAmount)
        internal view returns (uint128)
    {
        (uint160 sqrtP,,,,,,) = IUniswapV3Pool(c.pool).slot0();
        uint160 sqrtLow  = UniswapV3OracleLib.getSqrtRatioAtTick(c.tickLower);
        uint160 sqrtHigh = UniswapV3OracleLib.getSqrtRatioAtTick(c.tickUpper);
        (uint256 amount0, uint256 amount1) = c.buckIsToken0
            ? (buckAmount, tokenAmount)
            : (tokenAmount, buckAmount);
        return UniswapV3OracleLib.getLiquidityForAmounts(sqrtP, sqrtLow, sqrtHigh, amount0, amount1);
    }

    // --- Migration / unwind ----------------------------------------------- //

    /// @notice (Scaffold stub) Hand the receipt authority to a successor basket.
    ///         The full LP + outstanding handoff lands with the migration pass.
    function adoptReceiptTo(address successor) external onlyGov {
        receipt.adopt(successor);
    }

    // --- V3 callbacks ----------------------------------------------------- //

    function uniswapV3MintCallback(uint256 amount0Owed, uint256 amount1Owed, bytes calldata data)
        external override
    {
        require(msg.sender == _callbackPool, "bad callback");
        address token = abi.decode(data, (address));
        Constituent storage c = constituents[indexOf[token] - 1];
        if (c.buckIsToken0) {
            if (amount0Owed > 0) IERC20(address(buck)).transfer(msg.sender, amount0Owed);
            if (amount1Owed > 0) IERC20(c.token).transfer(msg.sender, amount1Owed);
        } else {
            if (amount0Owed > 0) IERC20(c.token).transfer(msg.sender, amount0Owed);
            if (amount1Owed > 0) IERC20(address(buck)).transfer(msg.sender, amount1Owed);
        }
    }

    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data)
        external override
    {
        require(msg.sender == _swapCallbackPool, "bad swap callback");
        address token = abi.decode(data, (address));
        Constituent storage c = constituents[indexOf[token] - 1];
        if (c.buckIsToken0) {
            if (amount0Delta > 0) IERC20(address(buck)).transfer(msg.sender, uint256(amount0Delta));
            if (amount1Delta > 0) IERC20(c.token).transfer(msg.sender, uint256(amount1Delta));
        } else {
            if (amount0Delta > 0) IERC20(c.token).transfer(msg.sender, uint256(amount0Delta));
            if (amount1Delta > 0) IERC20(address(buck)).transfer(msg.sender, uint256(amount1Delta));
        }
    }

    // --- Internal helpers ------------------------------------------------- //

    function _findOrCreatePool(address token, uint24 feeTier) internal returns (address pool) {
        pool = v3Factory.getPool(address(buck), token, feeTier);
        if (pool == address(0)) {
            pool = v3Factory.createPool(address(buck), token, feeTier);
        }
    }

    function _positionLiquidity(Constituent storage c) internal view returns (uint128 liquidity) {
        (liquidity,,,,) = IUniswapV3Pool(c.pool).positions(
            keccak256(abi.encodePacked(address(this), c.tickLower, c.tickUpper))
        );
    }

    function _isFirstPositionInPool(Constituent storage c) internal view returns (bool) {
        return _positionLiquidity(c) == 0;
    }

    function _readPoolPrice(Constituent storage c, uint32 secondsAgo)
        internal view returns (uint256 priceInBuck)
    {
        int24 tick;
        if (secondsAgo == 0) {
            (, tick,,,,,) = IUniswapV3Pool(c.pool).slot0();
        } else {
            try this.consultTickExternal(c.pool, secondsAgo) returns (int24 t) {
                tick = t;
            } catch {
                (, tick,,,,,) = IUniswapV3Pool(c.pool).slot0();
            }
        }
        priceInBuck = UniswapV3OracleLib.getQuoteAtTick(
            tick, uint128(10 ** c.decimals), c.token, address(buck));
    }

    function consultTickExternal(address pool, uint32 secondsAgo) external view returns (int24) {
        return UniswapV3OracleLib.consult(pool, secondsAgo);
    }

    function _enforceSlippageGuard(Constituent storage c, uint256 spotPrice, uint256 maxDeviationBp)
        internal view
    {
        if (maxDeviationBp == 0) return;
        try this.peekTwap(c.pool, twapWindow) returns (uint256 twapPrice) {
            if (twapPrice == 0) return;
            uint256 dev = spotPrice > twapPrice ? spotPrice - twapPrice : twapPrice - spotPrice;
            require(dev * 10000 <= twapPrice * maxDeviationBp, "slippage");
        } catch {
            return;
        }
    }

    function peekTwap(address pool, uint32 secondsAgo) external view returns (uint256) {
        int24 tick = UniswapV3OracleLib.consult(pool, secondsAgo);
        Constituent storage c = constituents[indexOf[_poolToToken(pool)] - 1];
        return UniswapV3OracleLib.getQuoteAtTick(
            tick, uint128(10 ** c.decimals), c.token, address(buck));
    }

    function _poolToToken(address pool) internal view returns (address) {
        for (uint256 i = 0; i < constituents.length; i++) {
            if (constituents[i].pool == pool) return constituents[i].token;
        }
        revert("unknown pool");
    }

    function _decreaseAndCollect(Constituent storage c, uint128 liquidity)
        internal returns (uint256 tokenOut, uint256 buckOut)
    {
        IUniswapV3Pool(c.pool).burn(c.tickLower, c.tickUpper, liquidity);
        (uint128 a0, uint128 a1) = IUniswapV3Pool(c.pool).collect(
            address(this), c.tickLower, c.tickUpper, type(uint128).max, type(uint128).max);
        if (c.buckIsToken0) {
            buckOut  = a0;
            tokenOut = a1;
        } else {
            tokenOut = a0;
            buckOut  = a1;
        }
    }
}
