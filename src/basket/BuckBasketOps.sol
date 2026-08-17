// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}              from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {BuckBasketProRata}   from "./BuckBasketProRata.sol";
import {IBuckBasketVenue}    from "./IBuckBasketVenue.sol";

interface IMonetaryDirector {
    function monetaryEffort() external view returns (int32 effortBp, bool outright);
    function epochNow() external view returns (uint32);
}

/// @title BuckBasketOps -- the two-mode basket: commodity rebalancing on the
///        DIFFERENTIAL mode, monetary operations on the COMMON mode.
///
/// @notice The `pairs` director reads a seven-scale filter bank over the pool
///         ticks.  A tick is a log price, so the DIFFERENCES between legs say
///         which commodity is rich against which -- the existing mandate --
///         and the MEAN of the legs is log(basket priced in BUCK), which is
///         `basketValueInBuck`, the K controller's own process variable.  The
///         signal was already being computed every cycle and thrown away.
///         `PairsRebalanceDirector.commonMode` now returns it and
///         `monetaryEffort()` turns it into a signed instruction.
///
/// # Why a fast desk at all, when K exists
///
///         BUCK_K is sound and slow.  It reaches the economy only through
///         `creditLimit`, and a credit book turns over in months, so an
///         attacker with real money can push BUCK off parity faster than K
///         can pull it back.  The basket is the largest pool of real assets
///         in the system and can act immediately.  K sets the standing
///         policy; the desk runs the operations.
///
///         The division of labour is the point, and so is the fact that the
///         desk does NOT have to win on its own.  A monetary operation is a
///         bet that K eventually forces the issue -- the desk buys BUCK the
///         market is dumping and holds it while K tightens credit behind it.
///         Interim mark-to-market is therefore the wrong scorecard; what
///         matters is whether the position pays off over K's timescale.  What
///         WOULD invalidate the bet is the desk damping the excursion so well
///         that K's error goes to zero and the long-term forcing never
///         arrives -- see `monetaryDeviationOffset` below.
///
/// # The four quadrants
///
///         Direction (is BUCK cheap or dear) crossed with persistence (has
///         the excursion turned, or does it keep going):
///
///             bvib > 1 BUCK CHEAP            bvib < 1 BUCK DEAR
///         Q1  ABSORB  buy BUCK, hold     Q3  SUPPLY  sell held BUCK
///         Q2  RETIRE  buy BUCK, burn     Q4  ISSUE   mint BUCK, sell
///
///         Q1/Q3 are repo: what Q1 accumulates is what Q3 releases when the
///         deviation turns, and net supply is unchanged.  Q2/Q4 are outright
///         and change the size of the balance sheet; they are reached only
///         when the deviation has stayed past the leash for `persistEpochs`.
///
/// # Why the depositor cannot be diluted by any of this
///
///         A depositor's claim is `V = theta * NAV` with
///         `theta = redeemBuck / totalOutstandingBuck`, allocated from
///         `poolBuckValues()`, which reads the basket's LP POSITION net of
///         `treasuryLiquidity`.  The monetary book is held as plain TOKEN and
///         BUCK BALANCES and is never LP'd, so it is outside `depL` by
///         construction and not one line of the redemption allocator changes.
///         `mintFromBasket` does not touch `totalOutstandingBuck` either --
///         only `depositToken` / `_depositBuck` / `redeem` do -- so monetary
///         issuance cannot enter a receipt's denominator even by accident.
///
///         That is the whole liability-class separation, and it is deliberate
///         that it is achieved by NOT writing to the payout path rather than
///         by netting something out of it.  A mistake in that path is a
///         silent transfer away from depositors, not a bad trade.
///
/// # The three bounds, and each stopped a different runaway
///
///         `maxPositionBp`   inventory ceiling, bp of NAV.  Absorbing holds
///                           the measured deviation down -- that IS absorbing
///                           -- so a persistence test built on that deviation
///                           is suppressed by the very act it polices.  In
///                           the model the desk bought until its inventory
///                           reached half the pool and never once escalated,
///                           showing a profit the whole way on a mark of a
///                           position it could not have unwound.
///         `maxOutrightBp`   cumulative balance-sheet ceiling, bp of NAV.
///                           Fixing the inventory runaway merely relocated
///                           it: escalation then fired every day and burned
///                           93% of all supply chasing an 8% gap.  A central
///                           bank announces a programme size; it does not run
///                           the desk until the number comes right.
///         `maxLegBp`        per-operation ceiling, bp of the pool's own BUCK
///                           reserve.  `poolBuckValues` enforces a spot/TWAP
///                           guard on every redemption, so an oversized
///                           monetary swap would revert every depositor exit
///                           until the window caught up.  The basket must not
///                           be able to brick its own redemption path.
contract BuckBasketOps is BuckBasketProRata {

    struct OpsParams {
        uint32 maxLegBp;        // per-leg cap, bp of that pool's BUCK reserve
        uint32 maxPositionBp;   // inventory cap, bp of NAV
        uint32 maxOutrightBp;   // cumulative |monetaryOutstanding| cap, bp of NAV
        bool   enabled;
    }

    OpsParams public opsParams;

    /// @notice Director consulted for `monetaryEffort()`.  Deliberately
    ///         SEPARATE from `director`, which gates the shell's advisory
    ///         deposit routing and rebalance step.  In the chain simulation
    ///         the rebalance director is driven externally by a keeper and
    ///         `setDirector` is never called, so pointing the monetary path
    ///         at `director` would have switched on advisory routing as a
    ///         side effect -- and the comparison this basket exists for must
    ///         differ from its baseline by monetary operations and nothing
    ///         else.  Falls back to `director` when unset.
    address public monetaryDirector;

    event OpsParamsSet(OpsParams p);
    event MonetaryDirectorSet(address indexed director);

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
    ) BuckBasketProRata(_buck, _controller, _v3Factory, _governance,
                        _defaultFeeTier, _twapWindow, _observationCardinality,
                        _defaultMaxDeviationBp, _minSeedLiquidity) {
        // Off until governance installs a policy: an ops basket with no
        // parameters must behave exactly like the baseline it is compared to.
        opsParams = OpsParams({maxLegBp: 40, maxPositionBp: 1000,
                               maxOutrightBp: 1000, enabled: false});
    }

    function setMonetaryDirector(address d_) external onlyGov {
        monetaryDirector = d_;
        emit MonetaryDirectorSet(d_);
    }

    function _monDir() internal view returns (address) {
        address md = monetaryDirector;
        return md != address(0) ? md : director;
    }

    /// @notice Capitalize the desk with TOKEN reserves.
    ///
    ///         Q1 and Q2 are TOKEN-funded bids, and the desk may not fund
    ///         them from depositor TOKEN: a depositor is paid in TOKEN only,
    ///         so converting their commodity into BUCK to defend parity would
    ///         charge them for it.  Its own issuance is the other source --
    ///         Q4 sells BUCK for TOKEN when BUCK is dear -- but that is
    ///         exactly backwards for a desk whose first task is an excursion
    ///         the WRONG way: in the reverting regime BUCK is never dear, the
    ///         desk never accumulates, and it wanted to act on 57 of 90 days
    ///         with nothing to act with.
    ///
    ///         That is the 1992 ERM lesson rather than a defect -- a currency
    ///         board can only defend with reserves it built up earlier -- so
    ///         the desk is capitalized like one.  In production the same job
    ///         is done by treasury accrual, which is already excluded from
    ///         every depositor claim; this is the founding grant.
    function capitalizeMonetary(uint256 i, uint256 amount) external onlyGov {
        if (i >= constituents.length) revert NotInBasket();
        if (amount == 0) revert Amount0();
        IERC20(constituents[i].token).transferFrom(msg.sender, address(this), amount);
        monetaryTokenHeld[i] += amount;
        emit MonetaryCapitalized(i, amount);
    }

    event MonetaryCapitalized(uint256 indexed i, uint256 amount);

    function setOpsParams(OpsParams calldata p) external onlyGov {
        if (p.maxLegBp > 500) revert Bp10000();     // 5% of a pool is already a lot
        opsParams = p;
        emit OpsParamsSet(p);
    }

    // --- Views ------------------------------------------------------------ //

    /// @notice NAV in BUCK: the depositor-claim base (2x the BUCK half of the
    ///         full-range positions), which is what every bound is sized in.
    function _navBuck() internal view returns (uint256) {
        (, , uint256 B, ) = _venue().poolBuckValues();
        return 2 * B;
    }

    /// @notice TOKEN the desk holds, valued in BUCK at pool spot.
    function monetaryTokenValue() public view returns (uint256 v) {
        (uint256[] memory bv, , , uint256[] memory prices) = _venue().poolBuckValues();
        for (uint256 i = 0; i < constituents.length; i++) {
            uint256 held = monetaryTokenHeld[i];
            if (held == 0 || prices[i] == 0) continue;
            v += held * prices[i] / (10 ** constituents[i].decimals);
            bv;
        }
    }

    /// @notice What the desk's own inventory has already taken out of the
    ///         measured deviation.
    ///
    ///         This is the number that decides whether the whole design
    ///         works.  The desk damps the excursion by absorbing it, and
    ///         `basketValueInBuck` is read from the very pools it absorbs
    ///         into -- so a successful operation SHRINKS the error the K
    ///         controller sees.  If K then stops tightening, the long-term
    ///         forcing the desk's position is a bet on never arrives, and the
    ///         desk is left holding inventory with nothing behind it.
    ///
    ///         Exposed rather than acted on, because the right response is a
    ///         controller-side decision (feed K the undamped error, or feed
    ///         the ACHIEVED action back to the integrator as anti-windup) and
    ///         it should be measured before it is chosen.
    function monetaryDeviationOffset() external view returns (int256) {
        return int256(monetaryBuckHeld) - int256(monetaryTokenValue());
    }

    // --- The keeper entry point ------------------------------------------- //

    /// @notice Run one monetary operation, permissionless and bounded.
    ///         Mirrors `rebalanceStep`: once per director epoch, advisory
    ///         signal in, bounded action out.
    /// @return quadrant 0 = nothing done, else 1..4
    function monetaryOperation() external returns (uint8 quadrant) {
        OpsParams memory op = opsParams;
        if (!op.enabled) revert MonetaryIdle();
        address dir = _monDir();
        if (dir == address(0)) revert DirectorUnset();

        uint32 e = IMonetaryDirector(dir).epochNow();
        if (lastMonetaryEpoch == e + 1) revert StepAlreadyDone();
        lastMonetaryEpoch = e + 1;

        (int32 effortBp, bool outright) = IMonetaryDirector(dir).monetaryEffort();
        if (effortBp == 0) revert NoAdvice();

        uint256 nav = _navBuck();
        uint256 size = nav * uint256(uint32(effortBp < 0 ? -effortBp : effortBp)) / 10000;
        if (size == 0) revert NoValue();

        quadrant = effortBp < 0
            ? _absorb(size, outright, op, nav, effortBp)
            : _supply(size, outright, op, nav, effortBp);
    }

    // --- BUCK cheap: buy it ----------------------------------------------- //

    function _absorb(uint256 size, bool outright, OpsParams memory op,
                     uint256 nav, int32 effortBp) internal returns (uint8) {
        bool room = -monetaryOutstanding
            < int256(nav * op.maxOutrightBp / 10000);

        // Q2 RETIRE, out of INVENTORY first.  Burning what the desk already
        // holds turns a temporary position into a permanent one, which is
        // precisely the escalation the article describes: the temporary
        // operation has been consumed AND the price stayed away.
        //
        // It has to come first, and the reason is a design error this
        // ordering fixes rather than a preference.  The position limit used
        // to be tested at the top and reverted before Q2 was ever reached --
        // so a desk pinned at its inventory ceiling could never escalate,
        // even though being pinned there IS the signal that the move is
        // real.  That is the model's first runaway wearing different
        // clothes: the bound meant to TRIGGER escalation was preventing it,
        // and Q2 fired zero times in 730 agent-days across two seeds.
        //
        // Burning inventory also needs no TOKEN at all, which matters
        // because by the time persistence is established the desk has
        // usually spent its reserves absorbing, and it relieves the ceiling
        // that was blocking further operations.
        if (outright && room && monetaryBuckHeld > 0) {
            uint256 burnAmt = size < monetaryBuckHeld ? size : monetaryBuckHeld;
            buck.burnFromBasket(burnAmt);
            monetaryBuckHeld -= burnAmt;
            monetaryOutstanding -= int256(burnAmt);
            emit MonetaryOperation(2, effortBp, true, burnAmt,
                                   monetaryOutstanding, monetaryBuckHeld);
            return 2;
        }

        // Position limit.  Inventory is the one signal the desk's own action
        // cannot suppress, because it IS the desk's own action.
        if (monetaryBuckHeld >= nav * op.maxPositionBp / 10000) revert MonetaryBound();

        // Nothing left to spend is an ordinary state, not an error in the
        // signal: the desk funds Q1/Q2 from the TOKEN its own issuance
        // bought, and a desk that has spent that book simply cannot absorb
        // any more until it issues again.  Distinguished from NoValue (an
        // empty basket) so a keeper can count it.
        uint256 bought = _swapAcross(false, size, op);
        if (bought == 0) revert MonetaryIdle();

        // Q2 on a FRESH purchase.  Unlike an agent -- which can only retire
        // float it issued itself, because supply is
        // sum_a max(0, signedRaw(a)) and a drawn credit line contributes
        // nothing -- the basket burns BUCK it bought from ANYONE.  That is
        // the whole reason this lives in the contract.
        if (outright && room) {
            buck.burnFromBasket(bought);
            monetaryOutstanding -= int256(bought);
            emit MonetaryOperation(2, effortBp, true, bought,
                                   monetaryOutstanding, monetaryBuckHeld);
            return 2;
        }
        // Q1 ABSORB: hold it; Q3 releases it when the deviation turns.
        monetaryBuckHeld += bought;
        emit MonetaryOperation(1, effortBp, false, bought,
                               monetaryOutstanding, monetaryBuckHeld);
        return 1;
    }

    // --- BUCK dear: sell it ------------------------------------------------ //

    function _supply(uint256 size, bool outright, OpsParams memory op,
                     uint256 nav, int32 effortBp) internal returns (uint8) {
        // Q3 SUPPLY first, from inventory previously absorbed: a temporary
        // operation should be unwound before a permanent one is opened.
        if (monetaryBuckHeld > 0) {
            uint256 sell = size < monetaryBuckHeld ? size : monetaryBuckHeld;
            uint256 sold = _swapAcross(true, sell, op);
            if (sold > 0) {
                monetaryBuckHeld -= sold;
                emit MonetaryOperation(3, effortBp, false, sold,
                                       monetaryOutstanding, monetaryBuckHeld);
                return 3;
            }
        }
        if (!outright) revert MonetaryIdle();
        if (monetaryOutstanding >= int256(nav * op.maxOutrightBp / 10000)) {
            revert MonetaryBound();
        }
        // Q4 ISSUE: mint against the basket's own TOKEN reserves and sell into
        // the bid, acquiring real assets with money the market has over-valued.
        // mintFromBasket bypasses creditLimit entirely -- direct-mint BUCK is
        // backed by basket TOKEN, not by insured-asset credit -- which is the
        // second thing no agent can do.
        buck.mintFromBasket(address(this), size);
        uint256 issued = _swapAcross(true, size, op);
        if (issued == 0) revert NoValue();
        // Anything the pools could not absorb goes straight back out; leaving
        // it minted would overstate the liability against real assets.
        if (issued < size) buck.burnFromBasket(size - issued);
        monetaryOutstanding += int256(issued);
        emit MonetaryOperation(4, effortBp, true, issued,
                               monetaryOutstanding, monetaryBuckHeld);
        return 4;
    }

    // --- Execution --------------------------------------------------------- //

    /// @dev Spread `sizeBuck` of BUCK notional evenly over every constituent.
    ///
    ///      Evenly, and that is a decision rather than a convenience.  The
    ///      common mode is by definition the part of the signal that is the
    ///      same in every pool, so concentrating the operation in one would
    ///      inject a DIFFERENTIAL disturbance -- the exact thing the pairs
    ///      engine would then spend real money undoing.  The two mandates
    ///      share one actuator, and this is how they are kept from fighting.
    function _swapAcross(bool sellBuck, uint256 sizeBuck, OpsParams memory op)
        internal returns (uint256 moved)
    {
        uint256 n = constituents.length;
        if (n == 0) return 0;
        uint256 share = sizeBuck / n;
        if (share == 0) return 0;
        for (uint256 i = 0; i < n; i++) {
            Constituent storage c = constituents[i];
            uint256 poolBuck = IERC20(address(buck)).balanceOf(c.pool);
            uint256 cap = poolBuck * op.maxLegBp / 10000;
            uint256 want = share < cap ? share : cap;
            if (want == 0) continue;
            if (sellBuck) {
                (uint256 spent, uint256 recv) = _venue().monetaryLeg(i, true, want);
                monetaryTokenHeld[i] += recv;
                moved += spent;
            } else {
                // Sized in BUCK, spent in TOKEN: convert at the pool's own
                // ratio, then bound by what the desk actually holds.
                uint256 tokRes = IERC20(c.token).balanceOf(c.pool);
                if (poolBuck == 0 || tokRes == 0) continue;
                uint256 tokWant = want * tokRes / poolBuck;
                uint256 have = monetaryTokenHeld[i];
                if (tokWant > have) tokWant = have;
                if (tokWant == 0) continue;
                (uint256 spent, uint256 recv) = _venue().monetaryLeg(i, false, tokWant);
                monetaryTokenHeld[i] = have - spent;
                moved += recv;
            }
        }
    }
}
