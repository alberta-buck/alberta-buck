// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}             from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";
import {IBuckKController}    from "../IBuckKController.sol";
import {BuckBasketReceipt}  from "./BuckBasketReceipt.sol";
import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";
import {BuckBasketStorage, IUniswapV3Factory, IBuckMintBurn} from "./BuckBasketStorage.sol";

/// @title BuckBasketProRata -- venue-agnostic pro-rata exit + treasury-split shell.
///
/// @notice The economic core of a BuckBasket: it mints BUCK against deposited
///         TOKEN, issues ERC-721 receipts, and on redemption allocates a
///         receipt's value claim `V = θ·NAV` (θ = redeemBuck / totalOutstandingBuck)
///         across pools by a single closed-form over the per-pool *depositor*
///         BUCK reserves (full-range ⇒ pool value = 2·buckReserve) -- drawing
///         from the most *overweight* pools first and degenerating to pure
///         pro-rata at equilibrium.  Value-conservation preserves the coverage
///         ratio, so the tail stays solvent regardless of which pools are drawn.
///         An optional `payoutToken` draws the whole claim from one pool.  The
///         two economic reverts are (1) the burn unsatisfiable within the
///         caller's `maxConversionLossBp` budget (underwater being its extreme)
///         and (2) a single-TOKEN payout whose pool can't source the claim.
///
/// # Shell / venue split
///
///         This shell owns the *policy* -- allocation math, treasury split, burn
///         seniority, receipt + outstanding-BUCK accounting -- and is AMM-agnostic.
///         Everything that touches the backing AMM (pool setup, liquidity in/out,
///         price/TWAP reads, conversions, and the V3 callbacks) lives in a venue
///         facet behind `IBuckBasketVenue`, reached by `delegatecall` over the
///         shared `BuckBasketStorage`.  The shell calls the facet via
///         `IBuckBasketVenue(address(this)).fn(...)` -- a self-call routed by the
///         `fallback` below into the facet -- the same dispatch a future Diamond
///         keeps.  The facet's V3 mint/swap callbacks also arrive here (the pool
///         calls the basket address) and are routed in by the same fallback.
///
/// # Two kinds of liquidity
///
///   * **Depositor liquidity** -- backs outstanding receipts; the pro-rata claim
///     base.  `depositorL = positionL - treasuryLiquidity`.
///   * **Treasury liquidity / pending BUCK** -- NAV above `totalOutstandingBuck`
///     (AMM fees, retained BUCK profit).  Funds BUCK-system R&D/ops; re-LP'd by
///     the venue facet; never part of a depositor's pro-rata claim.
///
/// # The depositor keeps the TOKEN; the BUCK stays with the basket
///
///   At redemption the depositor is paid in **TOKEN only** -- their withdrawn
///   commodity (principal + AMM fees + price change) -- and **never in BUCK**.
///   The entire BUCK profit (`Bw - R`) accrues to the treasury
///   (`treasuryBuckPending`, re-LP'd by `sweepTreasury`).  Rationale: the BUCK
///   the basket minted (and burns again at redemption) was created *on the
///   depositor's behalf* -- it provided the other half of every position's
///   liquidity, amplifying the fees and rebalancing flow the depositor's TOKEN
///   earned.  That leverage is the depositor's reward (a 2x-liquidity commodity
///   LP); the BUCK half of the position is the system's own seigniorage and
///   rightly stays with the basket.  The practical dividend: a depositor touches
///   BUCK zero times, so commodity LPs need **no BUCK identity** to participate
///   (BUCK transfers are identity-gated; paying depositors BUCK would force every
///   participant to be identity-bound).  The principal burn is senior to all of
///   this.
contract BuckBasketProRata is BuckBasketStorage {

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

        receipt = new BuckBasketReceipt(address(this));
    }

    /// @dev Type-safe handle to the venue facet, dispatched through `fallback`.
    function _venue() internal view returns (IBuckBasketVenue) {
        return IBuckBasketVenue(address(this));
    }

    // --- Governance ------------------------------------------------------- //

    function setGovernance(address _governance) external onlyGov {
        if (!(_governance != address(0))) revert Gov0();
        governance = _governance;
    }

    /// @notice Install/replace the AMM-venue facet (the delegatecall target).
    function setVenue(address _venue_) external onlyGov {
        venue = IBuckBasketVenue(_venue_);
        emit VenueSet(_venue_);
    }

    /// @notice Draw accumulated treasury BUCK profit to fund operations.
    function treasuryWithdraw(address to, uint256 amount) external onlyGov {
        if (!(to != address(0))) revert To0();
        if (!(amount <= treasuryBuckPending)) revert ExceedsPending();
        treasuryBuckPending -= amount;
        IERC20(address(buck)).transfer(to, amount);
        emit TreasuryWithdrawn(to, amount);
    }

    /// @notice Recycle accrued treasury BUCK profit into the most underweight
    ///         pool as treasury-owned liquidity -- the "buy low" leg, decoupled
    ///         from redemption.  Permissionless (any keeper); no-op below the
    ///         re-LP floor.  The venue does the swap+LP mechanics; the shell tags
    ///         the resulting liquidity as treasury and adjusts the BUCK ledger.
    function sweepTreasury() external {
        if (treasuryBuckPending >= MIN_REINVEST_BUCK) {
            (uint256 idx, uint128 liquidity, uint256 consumed) =
                _venue().investFromBucks(treasuryBuckPending, type(uint256).max);
            constituents[idx].treasuryLiquidity += liquidity;
            treasuryBuckPending -= consumed;
            emit TreasuryReinvested(idx, consumed, liquidity);
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
    ///         ratio, not re-priced at current spot).  Name + signature match the
    ///         legacy `BuckBasket.addBasketToken` for sim drop-in compat.
    function addBasketToken(
        address token,
        uint8   decimals,
        uint256 initialPriceInBuck,
        uint256 weightBp,
        uint24  feeTier
    ) external onlyGov returns (address pool) {
        if (!(token != address(0) && token != address(buck))) revert BadToken();
        if (!(weightBp <= 10000)) revert BadWeight();
        if (!(indexOf[token] == 0)) revert AlreadyPresent();
        if (!(initialPriceInBuck > 0)) revert BadPrice();

        uint256 N = constituents.length + 1;
        uint256 newW = weightBp > 0 ? weightBp : 10000 / N;
        if (!(newW <= 10000 && newW > 0)) revert BadTargetWeight();

        uint256 oldW = 10000 - newW;
        uint256 oldSumW = 0;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            c.targetWeightBp = UniswapV3OracleLib.mulDiv(c.targetWeightBp, oldW, 10000);
            if (!(c.targetWeightBp > 0 && c.targetWeightBp < 10000)) revert InvalidRescale();
            oldSumW += c.targetWeightBp;
            c.basketAmount = UniswapV3OracleLib.mulDiv(c.basketAmount, oldW, 10000);
        }
        if (constituents.length > 0) {
            if (!(oldSumW < 10000)) revert ScaledWeightsIncorrect();
            newW = 10000 - oldSumW;
        }

        uint256 weightUnit   = UniswapV3OracleLib.mulDiv(newW, 1e18, 10000);
        uint256 basketAmount = UniswapV3OracleLib.mulDiv(weightUnit, 1e18, initialPriceInBuck);

        int24 tickLower;
        int24 tickUpper;
        bool  buckIsToken0;
        (pool, tickLower, tickUpper, buckIsToken0) =
            _venue().setupPool(token, decimals, initialPriceInBuck, feeTier);

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
        emit BasketTokenAdded(token, newW, initialPriceInBuck, pool);
    }

    function constituentsLength() external view returns (uint256) {
        return constituents.length;
    }

    // --- Direct mint ------------------------------------------------------ //

    /// @notice Deposit into the basket and receive an ERC-721 receipt.
    ///         `token == constituent`: mint BUCK at the pool's spot price and LP
    ///         the (TOKEN, BUCK) pair full-range. `token == BUCK`: deploy the
    ///         BUCK into the most-underweight pool as a depositor position
    ///         (constant-mix injection, §8) -- see `_depositBuck`.
    function depositToken(address token, uint256 tokenAmount, uint256 maxDeviationBp)
        external returns (uint256 receiptId)
    {
        if (!(tokenAmount > 0)) revert Amount0();
        if (token == address(buck)) return _depositBuck(tokenAmount);

        uint256 idx = indexOf[token];
        if (!(idx > 0)) revert NotInBasket();

        // Pull the TOKEN here (msg.sender is the depositor at the shell), then
        // hand the LP mint + partner-BUCK mint to the venue.
        IERC20(token).transferFrom(msg.sender, address(this), tokenAmount);
        (uint128 liquidity, uint256 buckToMint) =
            _venue().provideForToken(idx - 1, tokenAmount, maxDeviationBp);

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

    /// @notice BUCK-side deposit: deploy the caller's BUCK into the most
    ///         underweight pool as a depositor position via `investFromBucks`
    ///         (swap-balance + LP), mint a receipt against the BUCK actually
    ///         consumed, and refund any unconsumed remainder.  The principal is
    ///         the consumed BUCK (the burn obligation); the receipt records the
    ///         routed-into constituent as its `token`.  The same `investFromBucks`
    ///         primitive `sweepTreasury` uses -- only the bookkeeping differs
    ///         (depositor receipt vs treasury slice).
    function _depositBuck(uint256 buckAmount) internal returns (uint256 receiptId) {
        IERC20(address(buck)).transferFrom(msg.sender, address(this), buckAmount);
        (uint256 idx, uint128 liquidity, uint256 consumed) =
            _venue().investFromBucks(buckAmount, type(uint256).max);
        if (!(consumed > 0)) revert Buck0();
        if (buckAmount > consumed) {
            IERC20(address(buck)).transfer(msg.sender, buckAmount - consumed);
        }

        address routed = constituents[idx].token;
        receiptId = receipt.mint(msg.sender);
        deposits[receiptId] = Deposit({
            buckPrincipal: consumed,
            tokenPrincipal: 0,
            token: routed,
            depositTime: uint64(block.timestamp)
        });
        totalOutstandingBuck += consumed;

        controller.compute();
        emit Deposited(msg.sender, receiptId, routed, buckAmount, consumed, liquidity);
    }

    // --- Redemption (sell-high value-claim exit) -------------------------- //

    /// @notice Balanced redeem with the default conversion-loss budget (1%).
    function redeem(uint256 receiptId, uint256 redeemBp) external {
        _redeem(receiptId, redeemBp, DEFAULT_CONVERSION_LOSS_BP, address(0));
    }

    /// @notice Balanced redeem with an explicit conversion-loss budget.
    function redeem(uint256 receiptId, uint256 redeemBp, uint256 maxConversionLossBp)
        external
    {
        _redeem(receiptId, redeemBp, maxConversionLossBp, address(0));
    }

    /// @notice Single-TOKEN redeem: source the whole claim from `payoutToken`'s
    ///         pool only (no cross-pool routing).  Reverts if that pool can't
    ///         supply the claim (`token too thin`) or, under deflation, if the
    ///         within-pool TOKEN->BUCK conversion needed to cover the burn
    ///         exceeds `maxConversionLossBp` (`conversion loss`).
    function redeem(uint256 receiptId, uint256 redeemBp,
                    address payoutToken, uint256 maxConversionLossBp)
        external
    {
        if (!(payoutToken != address(0))) revert PayoutToken0();
        _redeem(receiptId, redeemBp, maxConversionLossBp, payoutToken);
    }

    /// @notice Shared redeem body.  `payoutToken == 0` ⇒ balanced sell-high
    ///         allocation; otherwise the whole claim is drawn from that token's
    ///         pool.  `maxConversionLossBp` caps the value lost to forced
    ///         TOKEN->BUCK conversion under deflation (0 = unlimited); the call
    ///         reverts rather than realize a larger loss.
    function _redeem(uint256 receiptId, uint256 redeemBp,
                     uint256 maxConversionLossBp, address payoutToken)
        internal
    {
        if (!(receipt.ownerOf(receiptId) == msg.sender)) revert NotOwner();
        Deposit memory d = deposits[receiptId];
        if (!(d.buckPrincipal > 0)) revert EmptyDeposit();
        uint256 redeemShare = redeemBp == 0 ? 10000 : redeemBp;
        if (!(redeemShare <= 10000)) revert Bp10000();
        if (!(totalOutstandingBuck > 0)) revert NoOutstanding();

        uint256 R = redeemShare == 10000
            ? d.buckPrincipal
            : d.buckPrincipal * redeemShare / 10000;
        if (!(R > 0)) revert RedeemZero();

        // Phase 1: read venue value statistics, allocate (balanced sell-high or
        // all from one pool), and withdraw.
        (uint256[] memory bv, uint128[] memory depL, uint256 B) = _venue().poolBuckValues();
        (uint256[] memory burnL, uint256 V) = payoutToken == address(0)
            ? _allocateSellHigh(R, bv, depL, B)
            : _allocateSingleToken(R, payoutToken, bv, depL, B);
        uint256 N = constituents.length;
        uint256[] memory perPoolTok = new uint256[](N);
        uint256 Bw = 0;
        bool anyWithdrawn = false;
        for (uint256 i = 0; i < N; i++) {
            if (burnL[i] == 0) continue;
            (uint256 tok, uint256 b) = _venue().withdrawLiquidity(i, uint128(burnL[i]));
            perPoolTok[i] = tok;
            Bw += b;
            anyWithdrawn = true;
        }
        if (!(anyWithdrawn)) revert NoLPWithdrawn();

        // Phase 2: settle the burn.  Sell-high collects from BUCK-rich pools, so
        // `Bw >= R` is the common case (no conversion).  Under deflation, cover
        // the shortfall within the loss budget.  All BUCK profit is treasury
        // equity -- the depositor is paid in TOKEN only (below), so no BUCK ever
        // leaves to a depositor and commodity LPs need no BUCK identity.
        uint256 treasuryBuck = 0;
        uint256 burned;
        if (Bw >= R) {
            burned = R;
            treasuryBuck = Bw - R;          // all BUCK profit -> treasury
        } else {
            (uint256 gained, uint256 lossValue, uint256[] memory inv) =
                _venue().convertIntoBucks(perPoolTok, R - Bw);
            perPoolTok = inv;
            uint256 have = Bw + gained;
            burned = have >= R ? R : have;
            // Revert path 1: burn unsatisfiable within the loss budget.  A zero
            // budget means "unlimited" (skip the loss cap) so `redeem(id, bp, 0)`
            // matches the legacy basket's no-guard call.
            if (!(R - burned <= MAX_DUST_WEI)) revert Underwater();
            if (!(maxConversionLossBp == 0
                  || lossValue * 10000 <= maxConversionLossBp * V)) revert ConversionLoss();
            treasuryBuck = have - burned;   // over-swap excess
        }

        // Phase 3: burn principal, pay out.  Treasury BUCK stays in the basket
        // (accrued, re-LP'd by sweepTreasury); the depositor receives TOKEN only.
        if (burned > 0) buck.burnFromBasket(burned);
        if (treasuryBuck > 0) {
            treasuryBuckPending += treasuryBuck;
            emit TreasuryAccrued(treasuryBuck, treasuryBuckPending);
        }
        for (uint256 i = 0; i < N; i++) {
            if (perPoolTok[i] > 0) {
                IERC20(constituents[i].token).transfer(msg.sender, perPoolTok[i]);
            }
        }

        // Phase 4: finalize bookkeeping (outstanding drops by R; any dust gap
        // R-burned is a bounded supply leak, not double-counted).
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
            msg.sender, receiptId, burned, 0, treasuryBuck,
            redeemShare == 10000 ? 0 : 10000 - redeemShare);
    }

    /// @notice Closed-form sell-high allocation in BUCK value, over the venue's
    ///         per-pool *depositor* BUCK reserves (`bv`, `depL`, total `B`): draw
    ///         the value claim `V = θ·NAV` from the most overweight pools first,
    ///         degenerating to pro-rata at equilibrium.  Venue-neutral policy.
    /// @return burnL  liquidity to burn per pool (∑ value = V, each ≤ depositorL).
    /// @return V      the value claim in BUCK (the loss-budget base).
    function _allocateSellHigh(uint256 R, uint256[] memory bv, uint128[] memory depL, uint256 B)
        internal view returns (uint256[] memory burnL, uint256 V)
    {
        uint256 N = constituents.length;
        burnL = new uint256[](N);

        uint256 O = totalOutstandingBuck;
        V = UniswapV3OracleLib.mulDiv(R, 2 * B, O);             // θ·NAV
        uint256 claimBv = UniswapV3OracleLib.mulDiv(R, B, O);   // θ·B (BUCK half of V)

        // Ideal sell-high draw per pool: aᵢ = bvᵢ − wᵢ·B·(O−R)/O.  Positive ⇒
        // overweight; clamp negatives (underweight pools draw 0).
        uint256[] memory pos = new uint256[](N);
        uint256 sumPos = 0;
        for (uint256 i = 0; i < N; i++) {
            if (bv[i] == 0) continue;
            uint256 tgt = UniswapV3OracleLib.mulDiv(
                uint256(constituents[i].targetWeightBp) * B, O - R, 10000 * O);
            if (bv[i] > tgt) { pos[i] = bv[i] - tgt; sumPos += pos[i]; }
        }
        if (sumPos == 0) return (burnL, V);    // unreachable: ∑aᵢ = claimBv > 0

        for (uint256 i = 0; i < N; i++) {
            if (pos[i] == 0) continue;
            uint256 allocBv = UniswapV3OracleLib.mulDiv(pos[i], claimBv, sumPos);
            uint256 bl = UniswapV3OracleLib.mulDiv(uint256(depL[i]), allocBv, bv[i]);
            burnL[i] = bl > depL[i] ? depL[i] : bl;   // cap for rounding safety
        }
    }

    /// @notice Single-TOKEN allocation: draw the entire value claim `V = θ·NAV`
    ///         from `token`'s pool alone.  Reverts `token too thin` if that pool
    ///         can't source the claim (f > 1).  The burn is then covered from
    ///         that one pool (+ within-pool conversion if deflation), so no other
    ///         pool is ever touched.
    function _allocateSingleToken(
        uint256 R, address token, uint256[] memory bv, uint128[] memory depL, uint256 B
    ) internal view returns (uint256[] memory burnL, uint256 V) {
        uint256 ix = indexOf[token];
        if (!(ix > 0)) revert NotInBasket();
        ix -= 1;
        if (!(bv[ix] > 0)) revert EmptyPool();

        V = UniswapV3OracleLib.mulDiv(R, 2 * B, totalOutstandingBuck);   // θ·NAV
        uint256 dvX = 2 * bv[ix];                                        // pool value
        if (!(V <= dvX)) revert TokenTooThin();                            // f ≤ 1

        burnL = new uint256[](constituents.length);
        uint256 bl = UniswapV3OracleLib.mulDiv(uint256(depL[ix]), V, dvX);
        burnL[ix] = bl > depL[ix] ? depL[ix] : bl;
    }

    // --- Migration / unwind ----------------------------------------------- //

    /// @notice (Scaffold stub) Hand the receipt authority to a successor basket.
    ///         The full LP + outstanding handoff lands with the migration pass.
    function adoptReceiptTo(address successor) external onlyGov {
        receipt.adopt(successor);
    }

    // --- Venue dispatch (the degenerate Diamond) -------------------------- //

    /// @notice Route any selector the shell doesn't implement -- the venue facet's
    ///         methods *and* the V3 mint/swap callbacks the pool fires at the
    ///         basket -- into the facet via `delegatecall` over the shared
    ///         storage.  A real Diamond replaces "the one facet" with a
    ///         selector->facet map; the dispatch is otherwise identical.
    fallback() external {
        address v = address(venue);
        if (v == address(0)) revert VenueUnset();
        assembly {
            calldatacopy(0, 0, calldatasize())
            let ok := delegatecall(gas(), v, 0, calldatasize(), 0, 0)
            returndatacopy(0, 0, returndatasize())
            switch ok
            case 0 { revert(0, returndatasize()) }
            default { return(0, returndatasize()) }
        }
    }
}
