// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}              from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {BuckBasketStorage}   from "./BuckBasketStorage.sol";
import {IBuckBasketVenue}    from "./IBuckBasketVenue.sol";
import {IStabilizer}         from "./IStabilizer.sol";

interface IMonetaryDirector {
    function monetaryEffort() external view returns (int32 effortBp, bool outright);
    function epochNow() external view returns (uint32);
}

/// @title MonetaryDesk -- the basket's monetary-operations desk, a mixin:
///        commodity rebalancing on the DIFFERENTIAL mode, monetary operations
///        on the COMMON mode.
///
/// @notice Shared by BuckBasketOps (the pro-rata basket plus the desk) and
///         BuckBasketEquityOps (the equity basket plus the desk).  A shell
///         supplies the three things the desk reads from it: its NAV
///         (`_deskNav`, and `_deskNavSafe` that never reverts) and each
///         constituent's price (`_deskPrices`).  Everything else -- the
///         quadrants, the bounds, the book, the stabilizer seam -- is the
///         desk's own, and its book (`monetaryBuckHeld`, `monetaryTokenHeld`)
///         is plain balances: outside the pro-rata claim (never LP'd) and
///         outside the equity basket's explicit books (never counted).
///
/// @notice The `pairs` director reads a seven-scale filter bank over the pool
///         ticks.  A tick is a log price, so the DIFFERENCES between legs say
///         which commodity is rich against which -- the existing mandate.
///         The COMMON mode is log(basket priced in BUCK), `basketValueInBuck`,
///         the K controller's own process variable, which the director keeps
///         as a ladder of its own against par (`commonMode`; it was once the
///         legs' mean, which drifted from the basket -- see there), and
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
///
/// # The stabilizer seam
///
///         The desk is the first level-1 stabilizer: it implements
///         `IStabilizer` from its own book (`netInventory`, `capacity`,
///         `saturation`) and publishes the reference depth D
///         (`shadowDepth`).  The OBSERVER that registers stabilizers with
///         their lambdas and assembles K's process variable from them is
///         `ShadowObserver` -- its own contract, for the bytecode budget and
///         because that is the shape of the observer facet of the monetary
///         Diamond (WP-11).  What `BuckKControllerShadow` integrates is the
///         observer's `shadowValueInBuck()`, not anything on this shell.
abstract contract MonetaryDesk is BuckBasketStorage, IStabilizer {

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

    constructor() {
        // Off until governance installs a policy: an ops basket with no
        // parameters must behave exactly like the baseline it is compared to.
        opsParams = OpsParams({maxLegBp: 40, maxPositionBp: 1000,
                               maxOutrightBp: 1000, enabled: false});
    }

    /// @dev The shell's NAV in BUCK: what every bound is sized in.  May revert
    ///      when unreadable (`monetaryOperation` does too).
    function _deskNav() internal view virtual returns (uint256);

    /// @dev `_deskNav` without the revert (0 when unreadable): a VIEW on the
    ///      controller's path must not take K's compute() down with it.
    function _deskNavSafe() internal view virtual returns (uint256);

    /// @dev Each constituent's price in BUCK per whole TOKEN.
    function _deskPrices() internal view virtual returns (uint256[] memory);

    function _dv() internal view returns (IBuckBasketVenue) {
        return IBuckBasketVenue(address(this));
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

    /// @notice TOKEN the desk holds, valued in BUCK at the shell's prices.
    function monetaryTokenValue() public view returns (uint256 v) {
        uint256[] memory prices = _deskPrices();
        for (uint256 i = 0; i < constituents.length; i++) {
            uint256 held = monetaryTokenHeld[i];
            if (held == 0 || prices[i] == 0) continue;
            v += held * prices[i] / (10 ** constituents[i].decimals);
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

        uint256 nav = _deskNav();
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
            _deskRetire(burnAmt);
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
            _deskRetire(bought);
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
        // Q4 ISSUE: issue against the basket's own TOKEN reserves and sell into
        // the bid, acquiring real assets with money the market has over-valued.
        size = _deskIssue(size);
        if (size == 0) revert MonetaryBound();
        uint256 issued = _swapAcross(true, size, op);
        if (issued == 0) revert NoValue();
        // Anything the pools could not absorb goes straight back out; leaving
        // it issued would overstate the liability against real assets.
        if (issued < size) _deskRetire(size - issued);
        monetaryOutstanding += int256(issued);
        emit MonetaryOperation(4, effortBp, true, issued,
                               monetaryOutstanding, monetaryBuckHeld);
        return 4;
    }

    // --- Issue and retire: the host's hooks ---------------------------------- //

    /// @dev Make up to `size` BUCK available to sell, returning how much.
    ///      The pro-rata host mints it through Buck's basket hooks (the sims'
    ///      BuckWithBasketHooks): direct-mint BUCK backed by basket TOKEN.  A
    ///      credit-holding host (BuckBasketEquityOps) mints nothing -- selling
    ///      spends its credit -- and returns what its limit leaves room for.
    function _deskIssue(uint256 size) internal virtual returns (uint256) {
        buck.mintFromBasket(address(this), size);
        return size;
    }

    /// @dev Take `amount` BUCK the desk bought (or could not sell) out of
    ///      circulation.  The pro-rata host burns it; a credit-holding host
    ///      has nothing to do -- BUCK it receives repay its lien on arrival.
    function _deskRetire(uint256 amount) internal virtual {
        buck.burnFromBasket(amount);
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
        internal virtual returns (uint256 moved)
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
                (uint256 spent, uint256 recv) = _dv().monetaryLeg(i, true, want);
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
                (uint256 spent, uint256 recv) = _dv().monetaryLeg(i, false, tokWant);
                monetaryTokenHeld[i] = have - spent;
                moved += recv;
            }
        }
    }

    // --- IStabilizer: the desk's own book ----------------------------------- //

    /// @notice absorbed - issued, BUCK native units.
    ///
    /// @dev    DERIVATION from the book as `_absorb` / `_supply` keep it:
    ///
    ///           monetaryBuckHeld     BUCK bought under Q1 and still held
    ///                                (released by Q3, burned by Q2).  Every
    ///                                unit of it is BUCK the market sold and
    ///                                the desk took out of circulation
    ///                                TEMPORARILY -- absorbed, positive.
    ///           monetaryOutstanding  signed net OUTRIGHT change to supply:
    ///                                +issued (Q4 mint-and-sell), -retired
    ///                                (Q2 burn).  While it is positive the
    ///                                desk has BUCK in circulation it minted
    ///                                and has not retired -- issued, negative.
    ///
    ///         So netInventory = monetaryBuckHeld - max(monetaryOutstanding, 0).
    ///
    ///         The negative side of monetaryOutstanding is deliberately NOT
    ///         inventory.  A Q2 burn is the persistence escalation: it moves
    ///         BUCK out of level 1 and into K's stock (level 2), so the shadow term must DROP when it fires
    ///         (D4: "burned inventory leaves the book and the shadow term
    ///         drops to zero").  Counting the retired amount as absorbed would
    ///         leave the term where it was; counting it against the issued
    ///         side (signed) would make a burn of held inventory a no-op on
    ///         the shadow value.  Walk the quadrants: Q4 issue 100 -> -100;
    ///         Q1 absorb 100 -> 0 (its own issuance re-absorbed, circulation
    ///         unchanged); Q2 burn the 100 held -> held 0, outstanding 0 ->
    ///         0; Q2 burn a further 50 bought fresh -> outstanding -50 -> 0:
    ///         permanent, K sees the real bvib, no double count.
    function netInventory() public view override returns (int256) {
        int256 issued = monetaryOutstanding > 0 ? monetaryOutstanding : int256(0);
        return int256(monetaryBuckHeld) - issued;
    }

    /// @notice Remaining room under the desk's bounds, 1e18 = all: the
    ///         SMALLER of the fraction left under `maxPositionBp` (inventory
    ///         vs NAV) and under `maxOutrightBp` (|monetaryOutstanding| vs
    ///         NAV) -- the two bounds `_absorb` / `_supply` revert on.
    ///
    /// @dev    A disabled desk, and a desk whose NAV cannot be read (empty
    ///         basket, or the spot/TWAP guard tripped -- `monetaryOperation`
    ///         would revert on the same read), report 0: they cannot act,
    ///         which for the gain scheduling is the same thing as pinned.
    ///         `maxLegBp` is a per-operation pacing limit, not a book bound,
    ///         and TOKEN-reserve exhaustion is an ordinary state the desk
    ///         refills by issuing; neither enters capacity.
    function capacity() public view override returns (uint256) {
        OpsParams memory op = opsParams;
        if (!op.enabled) return 0;
        uint256 nav = _deskNavSafe();
        uint256 posRoom = _room(nav * op.maxPositionBp / 10000, monetaryBuckHeld);
        int256  o = monetaryOutstanding;
        uint256 outRoom = _room(nav * op.maxOutrightBp / 10000,
                                o < 0 ? uint256(-o) : uint256(o));
        return posRoom < outRoom ? posRoom : outRoom;
    }

    /// @notice 1e18 - capacity(): 1e18 once either bound has been hit.
    function saturation() public view override returns (uint256) {
        return 1e18 - capacity();
    }

    /// @notice WP-13: the desk's inventory bound in BUCK, `maxPositionBp` x
    ///         NAV -- the bound `_absorb` reverts on, and the cap the
    ///         observer normalizes `netInventory` by (decision 11).  The
    ///         outright bound is a balance-sheet programme size, not an
    ///         inventory bound, so it does not enter.
    ///
    /// @dev    Three outcomes (IStabilizer): 0 when the desk is disabled
    ///         (excluded from the aggregate); REVERTS `NavUnreadable` when
    ///         the desk is enabled and NAV cannot be read (empty basket, or
    ///         the spot/TWAP guard tripped in one pool -- the observer holds
    ///         its last good cap and flags the desk stale, so a raid day
    ///         does not read as a change of position); the bound otherwise.
    ///         The holding logic lives in the OBSERVER: the shell adds
    ///         nothing but this view (EIP-170 headroom).
    function positionCap() external view override returns (uint256) {
        OpsParams memory op = opsParams;
        if (!op.enabled) return 0;
        uint256 nav = _deskNavSafe();
        if (nav == 0) revert NavUnreadable();
        return nav * op.maxPositionBp / 10000;
    }

    /// @dev The desk is enabled but its NAV cannot be read this block.
    error NavUnreadable();

    /// @dev Fraction of `cap` still unused by `used`, 1e18-scaled; 0 at or
    ///      past the bound (the same `>=` the operations revert on), and 0
    ///      for a zero cap (any book at all is over it).
    function _room(uint256 cap, uint256 used) internal pure returns (uint256) {
        if (cap == 0 || used >= cap) return 0;
        return (cap - used) * 1e18 / cap;
    }

    /// @notice The reference depth D the observer normalizes inventory by:
    ///         the BUCK reserve of the basket pools (native units) -- the
    ///         whole pool balance, exactly what `_swapAcross` sizes its legs
    ///         against, rather than the guarded depositor slice from
    ///         `poolBuckValues`, which reverts under a spot/TWAP deviation
    ///         and would take K's compute() with it.
    function shadowDepth() external view returns (uint256 depth) {
        uint256 n = constituents.length;
        for (uint256 i = 0; i < n; i++) {
            depth += IERC20(address(buck)).balanceOf(constituents[i].pool);
        }
    }
}
