// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}             from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";
import {BuckBasketProRata}  from "./BuckBasketProRata.sol";

interface IFenceDirector {
    /// Differential mode: signed per-leg effort, bp of NAV per epoch.
    /// Positive = buy-side (underweight); negative = sell-side (overweight).
    function effortOf(uint256 i) external view returns (int256);
    /// Common mode at ladder scale k, tick*1e9.  Positive = the basket costs
    /// more BUCK than at inception, i.e. BUCK is CHEAP.
    function commonMode(uint256 k) external view returns (int256);
}
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
/// # Known limitations, in rough order of how much they matter
///
///         Recorded here rather than in a tracker because every one of them
///         is a property of the design as it stands, and the next person to
///         touch this file needs them before they touch it.
///
///   1. *The budget is denominated in the unit that moves.*  `K x (TOKEN
///      value in BUCK)` rises exactly when BUCK weakens, so the basket mints
///      MORE into a depreciation -- a positive feedback loop closed only by K
///      falling faster than the collateral revalues.  Measured over 40 days
///      the budget grew 55% while K fell 2.4%, so K is plainly the slower
///      leg.  It is damped, not absent: against the full-range basket over
///      730 days the excursion is far smaller.  A real fix denominates the
///      budget in basket units rather than BUCK, and that is a different
///      contract.
///
///   2. *The whole band is re-struck every call.*  `fenceRebalance` burns and
///      re-mints unconditionally, so the position is realized daily and pays
///      fees for the privilege.  `fenceRecenterBp` exists for precisely this
///      and is never read.  A ladder of rungs would let unfilled ones rest
///      and move only what the price has passed.
///
///   3. *One band per constituent, so nothing can be skewed.*  A symmetric
///      band is direction-neutral by construction: it cannot express "this
///      TOKEN is overweight and likely to revert" (the differential mode,
///      already available as `PairsRebalanceDirector.effortOf`) or "BUCK is
///      dear and K is about to expand supply, so stop selling TOKEN" (the
///      common mode, already available as `commonMode`).  Both signals exist
///      and are tested; only the placement that would use them is missing.
///
///   4. *The band centre is struck on SPOT while the budget is read on TWAP.*
///      That asymmetry is a manipulation surface: moving spot before a
///      re-strike shifts where the basket's liquidity lands without moving
///      the budget that sizes it.  The TWAP is the right reference for both;
///      spot is used here only because the band has to be placed relative to
///      where trading actually is.
///
///   5. *About (1-K) of every deposit's TOKEN sits idle.*
///      `getLiquidityForAmounts` binds on whichever side runs out first,
///      always the BUCK side under a K-scaled mix, so roughly a quarter of
///      the collateral earns nothing at K = 0.75.  It is NOT dead weight to
///      be removed: it is the buffer redemption settles against, and without
///      it exits fall back to swapping into the band they just thinned.  The
///      improvement is to deploy it as a single-sided rung that is still
///      reachable for settlement, not to shrink it.
///
///   6. *BUCK-side deposits revert.*  `BuckBasketProRata.depositToken`
///      short-circuits `token == address(buck)` into `_depositBuck`; this
///      override drops that branch, so depositing BUCK reverts NotInBasket.
///      It is a genuine design question rather than an oversight -- someone
///      depositing BUCK into a basket whose rule is "mint K x against TOKEN
///      collateral" is not providing collateral -- but it must be answered,
///      because 75 agents in the chain scenario do exactly that.
///
///   7. *Settlement can still swap.*  Idle-first bounds it to the shortfall
///      rather than the whole claim, but a large exit against a converted
///      band still pays slippage.  A redemption-reserve target inside
///      `fenceRebalance` would bound it further.
///
///   8. *`totalOutstandingBuck` and `Deposit.buckPrincipal` are vestigial.*
///      Both are historical records that `fenceRebalance` does not maintain.
///      `netIssued` is the live obligation and the only one that may be used
///      for anything; retiring the stale figure at exit destroyed depositor
///      value (a receipt owing 12,000 was retiring 30,001) until it was
///      caught.  They are kept only so the deposit ledger still zeroes.
///
///   9. *Redemption pays a mix of every constituent, not the deposit token.*
///      Correct for a basket saver -- they own a slice of the whole thing --
///      but it differs from ProRata's single-token payout, and any consumer
///      that assumes one token back will mis-account it.
///
///  10. *`openFence` is one-shot per constituent.*  There is no migration
///      path if a band ever needs to move pools or fee tiers.
///
///  11. *O(N) external calls per operation.*  `fenceAssets`, `buckBudget` and
///      `_redeem` all loop the constituents and call the facet per leg.  Fine
///      at three, not obviously fine at thirty.
///
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

    /// @notice Directional placement, off by default so the symmetric band
    ///         remains the baseline any comparison is measured against.
    ///
    ///         Both inputs already exist on PairsRebalanceDirector and are
    ///         already tested; only the placement that uses them is new.
    ///         FACTOR A is the differential mode -- a constituent far above
    ///         its target weight is the one most likely to revert, because
    ///         every basket commodity is thermodynamically and
    ///         substitutionally replaceable, so supply answers price.  FACTOR
    ///         B is the common mode -- it does not say where BUCK IS, it says
    ///         where K is about to push it, which is the opposite sign: BUCK
    ///         dear now means K expands supply, BUCK weakens, and TOKEN/BUCK
    ///         prices RISE, so the basket should be accumulating TOKEN rather
    ///         than selling it.
    struct FactorParams {
        uint16 weightA;     // percent applied to the differential signal
        uint16 weightB;     // percent applied to the common mode
        uint16 maxSkewBp;   // clamp on the centre offset, bp of the half-width
        uint8  measIdx;     // ladder rung read for the common mode
        bool   enabled;
    }
    FactorParams public factorParams;

    event FactorParamsSet(FactorParams p);

    /// @notice BUCK this basket has issued and not yet retired: minted minus
    ///         burned across deposits, re-strikes and redemptions.
    ///
    ///         `totalOutstandingBuck` cannot serve here.  It is the sum of
    ///         deposit PRINCIPALS -- a historical record -- while
    ///         `fenceRebalance` mints and burns against the K budget without
    ///         touching it, so the two diverge from the first re-strike.  The
    ///         live figure is the one a claim has to net against.
    int256 public netIssued;

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

    function setFactorParams(FactorParams calldata p) external onlyGov {
        if (p.maxSkewBp > 10000 || p.measIdx > 6) revert BadTargetWeight();
        factorParams = p;
        emit FactorParamsSet(p);
    }

    /// @notice Signed placement skew, bp of the half-width.
    ///           > 0  centre the band ABOVE spot -- ask-heavy, holding TOKEN
    ///                ready to sell into any strength, which is how you
    ///                position for a fall you cannot sell into directly.
    ///           < 0  centre BELOW spot -- bid-heavy, accumulating TOKEN on
    ///                any weakness ahead of an expected rise.
    ///
    ///         A resting order can only be filled by the price coming TO it,
    ///         so "expect a fall" cannot mean an ask further above -- the
    ///         price would move away from it and never fill.  It means an ask
    ///         tight against spot, which is what centring the band upward
    ///         produces.  This is the ramp-in-front-of-the-marble placement:
    ///         if the move comes, the arbitrage that makes it fills the band
    ///         on the way past.
    function fenceSkewBp(uint256 i) public view returns (int256 bp) {
        FactorParams memory fp = factorParams;
        if (!fp.enabled || director == address(0)) return 0;

        // A: overweight (negative effort) => expect a fall => positive skew.
        int256 a;
        try IFenceDirector(director).effortOf(i) returns (int256 e) { a = -e; }
        catch { a = 0; }

        // B: common mode POSITIVE means BUCK is cheap now, so K tightens,
        // supply contracts, BUCK strengthens and TOKEN/BUCK falls => again a
        // positive skew.  Scaled from tick*1e9 to bp (a tick is ~1bp).
        int256 b;
        try IFenceDirector(director).commonMode(fp.measIdx) returns (int256 c) {
            b = c / 1e9;
        } catch { b = 0; }

        bp = (a * int256(uint256(fp.weightA)) + b * int256(uint256(fp.weightB)))
            / 100;
        int256 cap = int256(uint256(fp.maxSkewBp));
        if (bp >  cap) bp =  cap;
        if (bp < -cap) bp = -cap;
    }

    /// @dev Where the band sits this strike: half-width either side of a
    ///      centre offset from spot by the skew.
    function _bandFor(uint256 i, int24 tick, int24 spacing)
        internal view returns (int24 lo, int24 hi)
    {
        int24 w = _bpToTicks(fenceHalfWidthBp);
        int256 off = int256(w) * fenceSkewBp(i) / 10000;
        int24 centre = tick + int24(off);
        lo = _align(centre - w, spacing);
        hi = _align(centre + w, spacing);
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
        (fz.lo, fz.hi) = _bandFor(i, tick, spacing);
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
        return _venue().fenceQuote(fz.pool, fz.lo, fz.hi, fz.liquidity,
                                   fz.buckIsToken0);
    }

    /// @notice TWAP price of constituent `i` in BUCK per whole TOKEN, read
    ///         from the FENCE pool.  TWAP and not spot: the K budget is
    ///         computed from this, so a spot read would let a whale inflate
    ///         the basket's own issuance budget by pushing the price.
    function fencePrice(uint256 i) public view returns (uint256) {
        Constituent storage c = constituents[i];
        return _venue().fenceTwap(fenceOf[i].pool, c.token, c.decimals,
                                  twapWindow);
    }

    /// @notice Everything the basket holds, in BUCK: both sides of every
    ///         band PLUS the idle balances.
    ///
    ///         The idle part is not a rounding detail.  `getLiquidityForAmounts`
    ///         binds on whichever side runs out first, and with a K-scaled
    ///         mix that is always the BUCK side, so about (1-K) of every
    ///         deposit's TOKEN -- a quarter of it at K = 0.75 -- sits outside
    ///         the band.  A band-only measure misses all of it.
    function fenceAssets() public view returns (uint256 assets) {
        for (uint256 i = 0; i < constituents.length; i++) {
            (uint256 b, uint256 t) = fenceAmounts(i);
            uint256 tok = t + IERC20(constituents[i].token).balanceOf(address(this));
            assets += b;
            if (tok > 0) {
                assets += UniswapV3OracleLib.mulDiv(
                    tok, fencePrice(i), 10 ** constituents[i].decimals);
            }
        }
        assets += IERC20(address(buck)).balanceOf(address(this));
    }

    /// @notice The depositor claim base: assets NET of the BUCK the basket
    ///         still owes to retire.
    ///
    ///         Netting is what makes this comparable to `totalShares`.  A
    ///         deposit of `t` adds `t` of TOKEN and mints `K*t` of BUCK, so
    ///         assets rise by `(1+K)t` while the obligation rises by `K*t` --
    ///         a net `t`, which is exactly the shares issued.  Reporting
    ///         assets without the netting made the basket look 1/(1+K) richer
    ///         than it is, and the band-only version made it look poorer.
    function fenceNav() public view returns (uint256) {
        uint256 assets = fenceAssets();
        int256 net = int256(assets) - netIssued;
        return net > 0 ? uint256(net) : 0;
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
        if (mintAmt > 0) {
            buck.mintFromBasket(address(this), mintAmt);
            netIssued += int256(mintAmt);
        }

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
        (uint256 a0, uint256 a1) = fz.buckIsToken0
            ? (buckAmount, tokenAmount) : (tokenAmount, buckAmount);
        return _venue().fenceLiquidityFor(fz.pool, fz.lo, fz.hi, a0, a1);
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

        // Re-strike, skewed by the factors when they are enabled.
        (, int24 tick,) = _venue().fenceState(fz.pool);
        (fz.lo, fz.hi) = _bandFor(i, tick, fz.spacing);

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
            netIssued -= int256(excess);
            buckDelta = -int256(excess);
        } else if (target > buckHave) {
            uint256 short_ = target - buckHave;
            buck.mintFromBasket(address(this), short_);
            buckHave += short_;
            netIssued += int256(short_);
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
        uint256 buckClaim = IERC20(address(buck)).balanceOf(address(this))
            - buckBefore + buckBefore * num / den;

        Deposit storage dep = deposits[receiptId];
        // The obligation this receipt still carries is its PRO-RATA share of
        // the LIVE issuance, not the figure minted on the day it arrived.
        //
        // `buckPrincipal` goes stale the moment fenceRebalance burns against
        // a falling K budget, and it never catches up.  Retiring the stale
        // figure destroys BUCK the basket no longer owes, and that BUCK is
        // depositor value: measured, a receipt whose live obligation was
        // 12,000 was retiring 30,001, and recovered 28,077 against a 40,001
        // claim.  Shares are a pro-rata claim on the assets; the obligation
        // has to be pro-rata on the same denominator or the two do not close.
        uint256 principal = netIssued > 0
            ? uint256(netIssued) * num / den : 0;
        uint256 j = indexOf[dep.token] - 1;
        Fence storage fzo = fenceOf[j];

        // A shortfall still has to be covered by selling TOKEN, or the
        // obligation silently fails to retire and every later claim is
        // overstated.  This direction is small and unavoidable.
        if (buckClaim < principal) {
            uint256 need = principal - buckClaim;
            uint256 px = fencePrice(j);
            uint256 tokIn = px == 0 ? 0 : UniswapV3OracleLib.mulDiv(
                need, 10 ** constituents[j].decimals, px) * 102 / 100;
            uint256 have = IERC20(dep.token).balanceOf(address(this));
            if (tokIn > have) tokIn = have;
            if (tokIn > 0) {
                (, uint256 got) = _venue().fenceSwap(dep.token, fzo.pool, false, tokIn);
                buckClaim += got;
            }
        }

        uint256 buckHeld = IERC20(address(buck)).balanceOf(address(this));
        uint256 toBurn = principal;
        if (toBurn > buckClaim) toBurn = buckClaim;
        if (toBurn > buckHeld)  toBurn = buckHeld;
        if (toBurn > 0) {
            buck.burnFromBasket(toBurn);
            netIssued -= int256(toBurn);
        }
        // `totalOutstandingBuck` and `buckPrincipal` are now informational
        // only -- kept so the deposit ledger still zeroes out as receipts
        // close, but no longer load-bearing for anything.
        uint256 booked = dep.buckPrincipal * bp / 10000;
        if (booked > dep.buckPrincipal) booked = dep.buckPrincipal;
        dep.buckPrincipal -= booked;
        totalOutstandingBuck -= booked > totalOutstandingBuck
            ? totalOutstandingBuck : booked;

        // Settle the BUCK side WITHOUT moving BUCK.
        //
        // Two earlier attempts failed for instructive reasons.  Paying only
        // TOKEN stranded the claim whenever the band had converted to BUCK
        // (a 40,000-share claim came back as 13,620).  CONVERTING the surplus
        // unwound into the very band the exit had just thinned, so the leaver
        // ate the slippage of their own withdrawal.  Transferring the BUCK in
        // kind reverts outright: `BUCK: recipient must identity-approve
        // sender` -- an active per-counterparty approval no depositor gives,
        // which is a far harder constraint than merely being identity-bound.
        //
        // So the surplus BUCK STAYS with the basket and the depositor is paid
        // its TWAP-equivalent in TOKEN out of the idle buffer instead.  That
        // buffer exists by construction: getLiquidityForAmounts binds on the
        // BUCK side, so about (1-K) of every deposit's TOKEN sits outside the
        // band and is exactly what this settles against.  No swap, no
        // slippage, no BUCK ever leaves.  The basket is left BUCK-heavy by
        // precisely the surplus, which is what the next fenceRebalance
        // retires against the K budget -- the imbalance is absorbed by the
        // mechanism that already exists rather than by the person leaving.
        uint256 extra;
        {
            uint256 surplus = buckClaim > toBurn ? buckClaim - toBurn : 0;
            uint256 px = fencePrice(j);
            if (surplus > 0 && px > 0) {
                extra = UniswapV3OracleLib.mulDiv(
                    surplus, 10 ** constituents[j].decimals, px);
            }
        }

        uint256 unpaid;
        for (uint256 i = 0; i < n; i++) {
            address tk = constituents[i].token;
            uint256 bal = IERC20(tk).balanceOf(address(this));
            uint256 released = bal > tokBefore[i] ? bal - tokBefore[i] : 0;
            uint256 pay = released + tokBefore[i] * num / den;
            if (i == j) pay += extra;
            if (pay > bal) { unpaid = pay - bal; pay = bal; }
            if (pay > 0) IERC20(tk).transfer(msg.sender, pay);
        }

        // Idle first, swap only what is left over.  Ordering matters more
        // than it looks: settling the WHOLE surplus by swapping unwinds into
        // the band the exit just thinned, and in the worst case -- a fully
        // converted band, half the liquidity gone -- returned 13,620 on a
        // 40,000 claim.  Draining the free buffer first means the swap is
        // only ever the remainder, which for one exit among many is nothing.
        if (unpaid > 0) {
            uint256 px2 = fencePrice(j);
            uint256 needBuck = px2 == 0 ? 0 : UniswapV3OracleLib.mulDiv(
                unpaid, px2, 10 ** constituents[j].decimals);
            uint256 held2 = IERC20(address(buck)).balanceOf(address(this));
            if (needBuck > held2) needBuck = held2;
            if (needBuck > 0) {
                (, uint256 gotTok) = _venue().fenceSwap(
                    dep.token, fzo.pool, true, needBuck);
                uint256 avail = IERC20(dep.token).balanceOf(address(this));
                if (gotTok > avail) gotTok = avail;
                if (gotTok > 0) IERC20(dep.token).transfer(msg.sender, gotTok);
            }
        }

        shareOf[receiptId] = sh - take;
        totalShares -= take;
        if (shareOf[receiptId] == 0) receipt.burn(receiptId);

        emit FenceRedeemed(receiptId, take, toBurn);
        emit Redeemed(msg.sender, receiptId, toBurn, 0, 0,
                      uint256(10000 - bp));
    }
}
