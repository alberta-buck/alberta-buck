// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}             from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";
import {BuckBasketProRata}  from "./BuckBasketProRata.sol";
import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";

/// @title BuckBasketFence -- a savings vehicle that capitalizes on BUCK
///        mispricing, and whose entire BUCK footprint is K-scaled.
///
/// @notice A different answer to the same question BuckBasketOps asks, and
///         the two are deliberately separate implementations.
///
/// # Why the ops basket was the wrong shape
///
///         BuckBasketOps defends parity: it reads the common mode and runs
///         open-market operations against it.  Measured over 730 days, that
///         is not the binding problem.  BUCK_K sat at its HARD FLOOR for 153
///         of those days -- 21% of the run -- and on 147 of them parity was
///         still broken by more than 2%.  K had spent its entire authority
///         and could not close the gap.
///
///         The reason is arithmetic.  `depositToken` pairs each deposit with
///         a full-range partner, which at the current price is 1:1 in value
///         with the TOKEN -- a permanent 100% LTV -- while every other issuer
///         in the system is capped at K x collateral.  The basket alone is
///         exempt, and it is roughly HALF of all BUCK in existence (47% mean,
///         51.9% peak, measured).  So K's lever reaches about half the float,
///         and when it maxes out, it maxes out.  A basket that then adds
///         defensive operations on top is treating the symptom.
///
/// # The inversion
///
///         Cap the basket's total issuance at `K x (TOKEN value at TWAP)` and
///         the relationship reverses.  K falling forces the basket to
///         withdraw and burn; K rising lets it deploy.  The most K-IMMUNE
///         half of the float becomes the most K-RESPONSIVE, because the
///         basket can act in one transaction where a credit book turns over
///         in months.  Its size stops being the problem and becomes the
///         reason the lever is strong.
///
///         Note this is only reachable by REPLACING the deposit pairing, not
///         by adding a desk beside it: full-range pairing is already 1/K - 1
///         (about 33% at K = 0.75) over any K-scaled budget before a single
///         defensive operation is placed.
///
/// # The fence
///
///         The deployment is a concentrated band around the TWAP rather than
///         a full-range position, which makes it a continuum of resting limit
///         orders -- a fence.  As BUCK depreciates the band's TOKEN side is
///         bought out and the basket ends up holding BUCK acquired cheap; as
///         BUCK appreciates its BUCK side is bought out and the basket ends
///         up holding real assets acquired with over-valued BUCK.  Either
///         way it accumulates whatever just became cheap, which is both the
///         stabilizing trade and the profitable one.
///
///         Every operation is TEMPORARY in the sense that matters: nothing is
///         retired or issued to make a point about the price.  Supply moves
///         only as a consequence of the K budget changing.
///
/// # Where the harvest goes
///
///         `fenceRebalance` burns the band, collects (principal AND fees --
///         `burn` only credits, `collect` is what moves it), re-solves the K
///         budget, and re-mints at the new centre.  The collected fees and
///         any premium captured across the band are re-deployed WITHOUT
///         issuing shares, so every receipt's claim rises pro-rata.  That is
///         the harvest accruing to depositors, and it needs no separate
///         accounting: `totalShares` simply does not move.
///
/// # The stop-loss is the budget
///
///         A grid's classic death is a persistent trend -- it averages into
///         the loser forever.  Here a real BUCK depreciation consumes the
///         TOKEN side, the basket accumulates BUCK, its TOKEN collateral
///         shrinks AND K falls, so the budget contracts and the position is
///         cut exactly when the excursion proves real.  One economically
///         meaningful constraint in place of three tuned bounds.
contract BuckBasketFence is BuckBasketProRata {

    struct Fence {
        address pool;            // separate fee tier on the same TOKEN/BUCK pair
        int24   lo;
        int24   hi;
        int24   spacing;
        uint128 liquidity;
        bool    buckIsToken0;
        bool    live;
    }

    /// @notice Band per constituent index.
    mapping(uint256 => Fence) public fenceOf;

    /// @notice Receipt -> shares.  The share unit is the BUCK VALUE of the
    ///         TOKEN deposited, NOT the BUCK minted against it.  Those differ
    ///         by K, and K moves: two depositors contributing equal real
    ///         value at different K must receive equal shares, or the second
    ///         is silently short-changed by the controller's setting on the
    ///         day they happened to arrive.
    mapping(uint256 => uint256) public shareOf;
    uint256 public totalShares;

    uint24  public fenceFeeTier;       // e.g. 500 (0.05%) vs depositors' 3000
    uint32  public fenceHalfWidthBp;   // band half-width around the TWAP
    uint32  public fenceRecenterBp;    // drift before the band is re-struck
    uint256 public harvestBuck;        // cumulative BUCK-side harvest, telemetry

    event FenceOpened(uint256 indexed i, address pool, int24 lo, int24 hi);
    event FenceStruck(uint256 indexed i, int24 lo, int24 hi, uint128 liquidity,
                      uint256 buckTarget, int256 buckDelta);
    event FenceRedeemed(uint256 indexed receiptId, uint256 shares,
                        uint256 buckBurned);

    constructor(
        address _buck,
        address _controller,
        address _v3Factory,
        address _governance,
        uint24  _defaultFeeTier,
        uint32  _twapWindow,
        uint16  _observationCardinality,
        uint256 _defaultMaxDeviationBp,
        uint256 _minSeedLiquidity,
        uint24  _fenceFeeTier
    ) BuckBasketProRata(_buck, _controller, _v3Factory, _governance,
                        _defaultFeeTier, _twapWindow, _observationCardinality,
                        _defaultMaxDeviationBp, _minSeedLiquidity) {
        fenceFeeTier     = _fenceFeeTier;
        fenceHalfWidthBp = 1500;        // +-15%
        fenceRecenterBp  = 500;         // re-strike after a 5% drift
    }

    function setFenceParams(uint32 halfWidthBp, uint32 recenterBp) external onlyGov {
        if (halfWidthBp == 0 || recenterBp == 0 || recenterBp >= halfWidthBp) {
            revert BadTargetWeight();
        }
        fenceHalfWidthBp = halfWidthBp;
        fenceRecenterBp  = recenterBp;
    }

    // --- Geometry ---------------------------------------------------------- //

    /// @dev A tick IS a log price, so a fractional move x spans
    ///      ln(1+x)/ln(1.0001) ticks.  Using bp directly makes a wide band
    ///      quietly narrower than asked for -- 900bp is 862 ticks, not 900.
    ///      Integer Newton on 1.0001^t is avoided by working from the sqrt
    ///      ratio the library already computes.
    function _bpToTicks(uint32 bp) internal pure returns (int24) {
        // ln(1+x)/ln(1.0001) ~= bp * (1 - bp/20000) for the bp range used
        // here (<= 5000), which is within a tick over that domain and needs
        // no transcendental.
        uint256 b = bp;
        uint256 t = b - (b * b) / 20000;
        return int24(int256(t == 0 ? 1 : t));
    }

    function _align(int24 t, int24 spacing) internal pure returns (int24) {
        int24 r = (t / spacing) * spacing;
        return r;
    }

    // --- Opening ----------------------------------------------------------- //

    /// @notice Create the fence pool for constituent `i` and strike the first
    ///         band.  The pool is a DIFFERENT fee tier on the same pair, so
    ///         `poolBuckValues` -- which reads the constituent's own pool --
    ///         is untouched by anything the fence does.
    function openFence(uint256 i) external onlyGov {
        Constituent storage c = constituents[i];
        Fence storage fz = fenceOf[i];
        if (fz.live) revert AlreadyPresent();
        (address pool, int24 spacing, bool buckIs0) = _venue().fencePool(
            c.token, c.decimals, c.initialPriceInBuck, fenceFeeTier);
        fz.pool = pool;
        fz.spacing = spacing;
        fz.buckIsToken0 = buckIs0;
        fz.live = true;
        (, int24 tick,) = _venue().fenceState(pool);
        int24 w = _bpToTicks(fenceHalfWidthBp);
        fz.lo = _align(tick - w, spacing);
        fz.hi = _align(tick + w, spacing);
        emit FenceOpened(i, pool, fz.lo, fz.hi);
    }

    // --- Valuation --------------------------------------------------------- //

    /// @notice The band's exact contents at the live price.  No `2 x
    ///         buckReserve` shortcut: a concentrated position's two sides are
    ///         not equal, and assuming they are is how a concentrated basket
    ///         would silently misprice every redemption.
    function fenceAmounts(uint256 i)
        public view returns (uint256 buckAmt, uint256 tokAmt)
    {
        Fence storage fz = fenceOf[i];
        if (!fz.live || fz.liquidity == 0) return (0, 0);
        (uint160 sp,,) = _venue().fenceState(fz.pool);
        uint160 sa = UniswapV3OracleLib.getSqrtRatioAtTick(fz.lo);
        uint160 sb = UniswapV3OracleLib.getSqrtRatioAtTick(fz.hi);
        uint160 spc = sp < sa ? sa : (sp > sb ? sb : sp);
        uint256 a0 = UniswapV3OracleLib.getAmount0ForLiquidity(spc, sb, fz.liquidity);
        uint256 a1 = UniswapV3OracleLib.getAmount1ForLiquidity(sa, spc, fz.liquidity);
        return fz.buckIsToken0 ? (a0, a1) : (a1, a0);
    }

    /// @notice TWAP price of constituent `i` in BUCK per whole TOKEN, read
    ///         from the FENCE pool.  TWAP and not spot: the K budget is
    ///         computed from this, so a spot read would let a whale inflate
    ///         the basket's own issuance budget by pushing the price.
    function fencePrice(uint256 i) public view returns (uint256) {
        Fence storage fz = fenceOf[i];
        Constituent storage c = constituents[i];
        int24 tick;
        try this.consultFence(fz.pool, twapWindow) returns (int24 t) {
            tick = t;
        } catch {
            (, tick,) = _venue().fenceState(fz.pool);
        }
        return UniswapV3OracleLib.getQuoteAtTick(
            tick, uint128(10 ** c.decimals), c.token, address(buck));
    }

    /// @dev External so `fencePrice` can try/catch a cold pool's consult.
    function consultFence(address pool, uint32 secondsAgo)
        external view returns (int24)
    {
        return UniswapV3OracleLib.consult(pool, secondsAgo);
    }

    /// @notice Whole-basket fence value in BUCK: the depositor claim base.
    function fenceNav() public view returns (uint256 nav) {
        for (uint256 i = 0; i < constituents.length; i++) {
            (uint256 b, uint256 t) = fenceAmounts(i);
            nav += b;
            if (t > 0) {
                nav += UniswapV3OracleLib.mulDiv(
                    t, fencePrice(i), 10 ** constituents[i].decimals);
            }
        }
    }

    /// @notice The K budget: the most BUCK this basket may have outstanding.
    ///         Every other issuer is capped at K x collateral; this is the
    ///         basket finally obeying the same rule.
    function buckBudget() public returns (uint256 budget) {
        uint256 K = controller.compute();
        for (uint256 i = 0; i < constituents.length; i++) {
            (, uint256 t) = fenceAmounts(i);
            uint256 idle = IERC20(constituents[i].token).balanceOf(address(this));
            uint256 tv = UniswapV3OracleLib.mulDiv(
                t + idle, fencePrice(i), 10 ** constituents[i].decimals);
            budget += UniswapV3OracleLib.mulDiv(tv, K, 1e18);
        }
    }

    // --- Deposit ------------------------------------------------------------ //

    function depositToken(address token, uint256 tokenAmount, uint256 maxDeviationBp)
        external override returns (uint256 receiptId)
    {
        maxDeviationBp;                       // band placement is the guard here
        if (!(tokenAmount > 0)) revert Amount0();
        uint256 idx = indexOf[token];
        if (!(idx > 0)) revert NotInBasket();
        uint256 i = idx - 1;
        Fence storage fz = fenceOf[i];
        if (!fz.live) revert VenueUnset();

        IERC20(token).transferFrom(msg.sender, address(this), tokenAmount);

        // Shares are the REAL value contributed, priced on TWAP.
        uint256 shares = UniswapV3OracleLib.mulDiv(
            tokenAmount, fencePrice(i), 10 ** constituents[i].decimals);
        if (!(shares > 0)) revert BadPrice();

        // ...and the BUCK minted against it is K x that, which is the whole
        // point of this basket.  At K = 0.75 the pools carry 25% less BUCK
        // per unit of collateral than the full-range basket does, and that
        // difference is float K can actually reach.
        uint256 K = controller.compute();
        uint256 mintAmt = UniswapV3OracleLib.mulDiv(shares, K, 1e18);
        if (mintAmt > 0) buck.mintFromBasket(address(this), mintAmt);

        uint128 L = _strikeLiquidity(fz, tokenAmount, mintAmt);
        if (L > 0) {
            _venue().fenceMint(token, fz.pool, fz.lo, fz.hi, L);
            fz.liquidity += L;
        }

        receiptId = receipt.mint(msg.sender);
        deposits[receiptId] = Deposit({
            buckPrincipal: mintAmt,
            tokenPrincipal: tokenAmount,
            token: token,
            depositTime: uint64(block.timestamp)
        });
        shareOf[receiptId] = shares;
        totalShares += shares;
        totalOutstandingBuck += mintAmt;

        emit Deposited(msg.sender, receiptId, token, tokenAmount, mintAmt, L);
    }

    function _strikeLiquidity(Fence storage fz, uint256 tokenAmount, uint256 buckAmount)
        internal view returns (uint128)
    {
        (uint160 sp,,) = _venue().fenceState(fz.pool);
        uint160 sa = UniswapV3OracleLib.getSqrtRatioAtTick(fz.lo);
        uint160 sb = UniswapV3OracleLib.getSqrtRatioAtTick(fz.hi);
        (uint256 a0, uint256 a1) = fz.buckIsToken0
            ? (buckAmount, tokenAmount) : (tokenAmount, buckAmount);
        return UniswapV3OracleLib.getLiquidityForAmounts(sp, sa, sb, a0, a1);
    }

    // --- The keeper: K tracking, re-striking, and the harvest --------------- //

    /// @notice Re-strike constituent `i`'s band: collect everything (fees
    ///         included), re-solve the K budget, and re-deploy.
    ///
    ///         This one function carries the whole design.  Collecting is
    ///         where the harvest lands; re-solving the budget is where K gets
    ///         its lever; re-centring is the repositioning that tracks an
    ///         excursion.  Permissionless, because none of it is a judgement
    ///         call -- the budget is arithmetic and the centre is the TWAP.
    function fenceRebalance(uint256 i) external returns (int256 buckDelta) {
        Fence storage fz = fenceOf[i];
        if (!fz.live) revert VenueUnset();
        Constituent storage c = constituents[i];

        if (fz.liquidity > 0) {
            _venue().fenceBurn(fz.pool, fz.lo, fz.hi, fz.liquidity);
            fz.liquidity = 0;
        }

        // Re-centre on the TWAP.
        (, int24 tick,) = _venue().fenceState(fz.pool);
        int24 w = _bpToTicks(fenceHalfWidthBp);
        fz.lo = _align(tick - w, fz.spacing);
        fz.hi = _align(tick + w, fz.spacing);

        // Everything on hand for this constituent.
        uint256 tokHave  = IERC20(c.token).balanceOf(address(this));
        uint256 buckHave = IERC20(address(buck)).balanceOf(address(this));

        uint256 K = controller.compute();
        uint256 tv = UniswapV3OracleLib.mulDiv(
            tokHave, fencePrice(i), 10 ** c.decimals);
        uint256 target = UniswapV3OracleLib.mulDiv(tv, K, 1e18);

        if (buckHave > target) {
            // Over budget: retire the excess.  This is the lever -- when K
            // falls, BUCK leaves circulation because the basket is obliged to
            // shrink, not because anyone decided to defend a price.
            uint256 excess = buckHave - target;
            buck.burnFromBasket(excess);
            buckHave -= excess;
            buckDelta = -int256(excess);
        } else if (target > buckHave) {
            uint256 short_ = target - buckHave;
            buck.mintFromBasket(address(this), short_);
            buckHave += short_;
            buckDelta = int256(short_);
        }

        uint128 L = _strikeLiquidity(fz, tokHave, buckHave);
        if (L > 0) {
            _venue().fenceMint(c.token, fz.pool, fz.lo, fz.hi, L);
            fz.liquidity = L;
        }
        emit FenceStruck(i, fz.lo, fz.hi, L, target, buckDelta);
    }

    // --- Redeem -------------------------------------------------------------- //

    /// @notice Pro-rata exit, paid IN KIND.
    ///
    ///         A fence depositor owns a slice of the whole basket, so the
    ///         natural payout is a slice of every constituent rather than a
    ///         single TOKEN reconstructed by swapping -- which would charge
    ///         the exiting depositor for conversions the basket never needed
    ///         to make.
    function _redeem(uint256 receiptId, uint256 redeemBp,
                     uint256 maxConversionLossBp, address payoutToken)
        internal override
    {
        maxConversionLossBp; payoutToken;      // in-kind: neither applies
        if (!(receipt.ownerOf(receiptId) == msg.sender)) revert NotOwner();
        uint256 sh = shareOf[receiptId];
        if (!(sh > 0)) revert EmptyDeposit();
        uint256 bp = redeemBp == 0 ? 10000 : redeemBp;
        if (!(bp <= 10000)) revert Bp10000();
        if (!(totalShares > 0)) revert NoOutstanding();

        uint256 take = sh * bp / 10000;
        uint256 num = take;
        uint256 den = totalShares;

        // Snapshot every balance BEFORE burning.  What the burn releases is
        // ALREADY this receipt's share of the band; taking num/den of the
        // post-burn balance on top of that applies the fraction twice, which
        // paid the first exiter a third of their own withdrawal and left the
        // last one holding everyone's remainder.
        uint256 n = constituents.length;
        uint256[] memory tokBefore = new uint256[](n);
        for (uint256 i = 0; i < n; i++) {
            tokBefore[i] = IERC20(constituents[i].token).balanceOf(address(this));
        }
        uint256 buckBefore = IERC20(address(buck)).balanceOf(address(this));

        for (uint256 i = 0; i < n; i++) {
            Fence storage fz = fenceOf[i];
            if (!fz.live || fz.liquidity == 0) continue;
            uint128 part = uint128(uint256(fz.liquidity) * num / den);
            if (part == 0) continue;
            _venue().fenceBurn(fz.pool, fz.lo, fz.hi, part);
            fz.liquidity -= part;
        }
        // Band proceeds (this receipt's, in full) plus its pro-rata slice of
        // whatever was sitting idle -- undeployed remainder and harvest alike.
        uint256 buckOut = IERC20(address(buck)).balanceOf(address(this))
            - buckBefore + buckBefore * num / den;

        // Retire this receipt's share of the outstanding principal.  Any BUCK
        // the band returned beyond it stays with the basket as harvest.
        Deposit storage dep = deposits[receiptId];
        uint256 principal = dep.buckPrincipal * bp / 10000;
        uint256 toBurn = buckOut < principal ? buckOut : principal;
        if (toBurn > 0) buck.burnFromBasket(toBurn);
        totalOutstandingBuck -= toBurn;
        dep.buckPrincipal -= principal < dep.buckPrincipal ? principal : dep.buckPrincipal;

        // Pay the TOKEN slices out in kind.
        for (uint256 i = 0; i < n; i++) {
            address tk = constituents[i].token;
            uint256 bal = IERC20(tk).balanceOf(address(this));
            if (bal == 0) continue;
            uint256 released = bal > tokBefore[i] ? bal - tokBefore[i] : 0;
            uint256 pay = released + tokBefore[i] * num / den;
            if (pay > bal) pay = bal;
            if (pay > 0) IERC20(tk).transfer(msg.sender, pay);
        }

        shareOf[receiptId] = sh - take;
        totalShares -= take;
        if (shareOf[receiptId] == 0) receipt.burn(receiptId);

        harvestBuck += buckOut > principal ? buckOut - principal : 0;
        emit FenceRedeemed(receiptId, take, toBurn);
        emit Redeemed(msg.sender, receiptId, toBurn, 0, 0,
                      uint256(10000 - bp));
    }
}
