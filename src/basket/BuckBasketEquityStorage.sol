// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBuckNetView} from "../IBuckNetView.sol";
import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";
import {BuckBasketStorage}  from "./BuckBasketStorage.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {BasketJubileeMath} from "./BasketJubileeMath.sol";
import {BasketMarketOps} from "./BasketMarketOps.sol";
import {BuckTypes} from "../BuckTypes.sol";

interface IBuckKSource {
    function currentBuckK() external view returns (uint256);   // 1e18
}

/// @notice Buck, as the basket sees it: an ordinary credit holder's account.
interface IBuckHolder {
    function signedBalanceOf(address a) external view returns (int256);
    function balanceOf(address a) external view returns (uint256);
    function reliefOf(address a) external view returns (uint256);
    function settleRelief() external;
    function mint(uint256 amount, uint256[] calldata tokenIds) external;
}

/// @notice Canonical cash and issuance-age views. No signed wallet or holder relief.
/// @notice Buck's net view (IBuckNetView) and the issuance calls the basket makes.
interface IBuckNetAccount is IBuckNetView {
    function approvedCarryingIssuer(address owner) external view returns (bool);
    function totalIssued() external view returns (uint256);
    function maturitiesPending() external view returns (bool);
    function jubilee() external view returns (address);
    function authorizeReduction(uint256 id, uint256 principal, uint256 minimumReliefScaled, uint64 deadline) external;
    function issue(uint256 id, uint256 coverage, uint256 spendable, uint256 maxPremium, address expectedPool)
        external returns (uint256 principal, uint256 premium);
}

interface IBasketCreditFund {
    function reduce(uint256 id, uint256 principal) external returns (uint256 reliefScaled, uint256 nominalRefund);
}

/// @notice BuckCredit, as the basket sees it: the issuer of its own MARKED
///         credit (DepreciationType 3).
interface IMarkedCredit {
    function setCreditIssuer(address insurer, bool accepted) external;
    function createCredit(address client, uint8 assetClass, uint256 faceValue,
                          uint256 depreciationFloor, uint8 depType, uint32 depRate,
                          uint48 depStartAt, uint32 premiumRate) external returns (uint256);
    function mark(uint256 tokenId, uint256 value) external;
    function markOf(uint256 tokenId) external view returns (uint256);
    function issuanceOf(uint256 tokenId) external view returns (uint256 principal, uint256 seconds_);
    function creditInfo(uint256 tokenId) external view returns (uint256 face, uint256 active, uint32 premiumRate);
    function currentValue(uint256 tokenId) external view returns (uint256);
}

/// @title BuckBasketEquityStorage -- the equity basket's books, appended to the
///        shared basket layout, and the valuation both of its contracts use.
///
/// @notice (alberta-buck-ethereum.org, "BuckBasketEquity: the Basket as a
///         Credit Holder".)  The shell (`BuckBasketEquity`) and its components facet
///         (`BuckBasketEquityWheel`) inherit this and nothing else, so they
///         share one layout; the venue facet (`BuckBasketUniswapV3`) inherits
///         only `BuckBasketStorage`, of which this is a strict extension.
///
///         The canonical basket is an approved Carrying service with one self-issued
///         MARKED BuckCredit. In canonical accounting its actual net cash and
///         that NFT's issued principal are separate. Transfers change cash;
///         only explicit issue/reduce operations change principal. Marking
///         collateral does not issue BUCK or repay a facility.
///
///         The TOKEN books are explicit: the wallet (`idleToken`) and each
///         pool's liquidity (`liquidityOf`) are the basket's own counters,
///         never its balances or its V3 position. The depositors' BUCK equity
///         is cash minus principal plus the marked unpaid Jubilee claim.
///         Nothing else spends from
///         it -- a monetary desk is its own credit holder (`BasketDesk`),
///         with its own account, mark and wheel -- so its limit is the
///         depositors' K x equity and no one else's collateral.
///
///         Canonical valuation pre/post invariants:
///           E1  equity(mark) = max(0, gross(mark) + cash - principal + claim).
///               One fee-aware TWAP claim serves all entry/exit price bands;
///               it is solved without recursive fund -> basket valuation.
///           E2  the collateral mark excludes unpaid claims. The optional
///               held-BUCK offset is separate; the NFT face still caps value.
///           E3  cash receipts and K cuts cannot change actual NFT principal;
///               a marked claim cannot be spent or guarantee a fund payout.
///           E4  an exit takes at most its fraction: in BUCK, its value at
///               LOW less the charge; in kind, exactly its fraction of the
///               positions and wallet TOKEN, less retained debt backing.
///               Canonical principal is unchanged: both opening and final
///               activated collateral must cover it under current K. Only
///               the legacy path implicitly repays its lien share.
///           E5  token.balanceOf(basket) >= idleToken[i]
///           E6  after an exit the mark is read afresh with final positions,
///               shares and basis, including any changed fund-owned receipt
///
///         The temporary legacy branch below is for existing signed-token
///         fixtures. Its signed balance/holder relief and implicit draw/repay
///         behavior are not valid semantics of canonical Buck. Canonical
///         opening validates the model, public service identity and matching
///         credit binding through shared setup in the existing upkeep facets.
///
///         Value is in BUCK.  A position's fair value is twice its BUCK side
///         at the chosen price (2 L sqrtP, full range).  The marks are: TWAP
///         (the price), HIGH (each pool at the higher of spot and TWAP: the
///         incumbents' side of an entry) and LOW (the lower: their side of an
///         exit).
abstract contract BuckBasketEquityStorage is BuckBasketStorage {

    // Shared transient locks: the shell and delegated upkeep operations use
    // the same slots. These do not add or reorder persistent storage.
    bool internal transient _positionEntered;
    bool internal transient _creditEntered;

    // --- The equity books (appended) ---------------------------------------- //

    struct Holding {
        uint128 shares;                  // 1e18
        uint128 basis;                   // BUCK brought, less what has left
    }
    mapping(uint256 => Holding) public holdings;

    uint256 public totalShares;          // receipts' + the treasury's
    uint256 public treasuryShares;       // the basket's own: the 25% cuts
    mapping(uint256 => uint256) public idleToken;     // the wallet's TOKEN_i, native decimals
    mapping(uint256 => uint128) public liquidityOf;   // the basket's own count per pool
    uint256 public flowMs;               // EMA of the daily net flow squared (BUCK^2, 1e36)
    int256  public dayFlow;              // today's net flow (BUCK)
    uint64  public lastDay;              // the day Daily last ran (block.timestamp / 1 days)
    address public equityWheel;          // the components facet
    mapping(uint256 => uint64) public lastSyncDay;    // Sync: once a day per pool

    IMarkedCredit public credit;         // the BuckCredit its credit lives in
    uint256 public creditId;             // its MARKED credit
    bool    public creditLive;           // activated (the first mark above zero)

    struct EquityParams {
        uint16 floorBp;                  // liquidity target at least this of the gross (100)
        uint16 flowZx100;                // z x 100 standard deviations at the band's floor (200)
        uint16 bandBp;                   // liquidity floats within +-this of its target (5000)
        uint32 parkBp;                   // parking room: Fund places beyond (1 + this) x target (30000)
        uint16 flowDays;                 // the flow EMA's span, days (30)
        uint16 stepBp;                   // Trim's slice, bp of a position (500)
        uint16 weightBandBp;             // the band director's weight band (200)
        uint16 grainPpm;                 // the least work, ppm of the gross (100)
        uint16 exitFeeBp;                // the stress fee's hook (0: off)
        uint16 swapCapBp;                // a wheel swap: at most this of the pool's BUCK depth (100)
        uint16 ceilBp;                   // the liquidity target at most this of the gross (2000)
    }
    EquityParams public eq;

    // The upkeep facet's response configuration; append-only. Legacy facets ignore it.
    struct ResponseParams {
        uint16 leashBp;                  // relative to the leg's declared weight
        uint32 interval;                 // minimum seconds between responses
        uint16 tiltBp;                   // how far a target leans against price: 0 not at all, 10000 the most
        uint16 leanCapBp;                // the most a target may lean, relative to its declared weight
    }
    ResponseParams public response;
    /// @notice The price a constituent is taken to return to, in BUCK units
    ///         per whole TOKEN as the venue quotes it. Its target leans
    ///         against its distance from this. Zero: its anchor price, the
    ///         one its reference quantity was struck at.
    mapping(uint256 => uint256) public referencePrice;
    uint64 public lastResponseAt;
    // Optional separate arb wheel: income crediting only, never upkeep or spending.
    address public arbitrageCreditor;
    /// @notice How the credit is marked. Off: at the basket's equity, so K
    ///         bounds every BUCK drawn. On: so that K bounds the BUCKs drawn
    ///         net of those still held beside the TOKEN in its own positions,
    ///         which carry no price risk against the lien they came from.
    bool public netMark;

    /// @notice Canonical compounded cash and separate facility debt. Public
    ///         credit opening validates the model, identity and matching NFT;
    ///         issuance, repayment and low-K exit solvency remain explicit.
    ///         Legacy fixtures retain the earlier signed-account branch.
    bool public netAccounting;
    uint16 internal refreshFloorBp;
    uint16 internal refreshTopBp;

    /// @notice The basket's share of a receipt's gain over its cost basis.
    uint256 public constant LAMBDA_BP = 2500;

    uint8 internal constant DEP_MARKED = 3;    // BuckCredit.DepreciationType.MARKED

    // Deposits and redemptions emit the pro-rata shells' Deposited and
    // Redeemed (BuckBasketStorage), which the simulation already reads.
    event WalletCredited(address indexed token, uint256 amount);
    event WheelWork(uint8 indexed kind, uint256 indexed i, uint256 amount);
    event CreditOpened(address indexed credit, uint256 indexed tokenId, uint256 face);
    /// @notice A pro-rata exit's TOKEN, paid in kind.
    event PaidInKind(address indexed to, address indexed token, uint256 amount);

    error MinOut();
    error NonzeroBasketPremium();
    error CreditShortfallIncreased();

    // --- Buck and the credit --------------------------------------------------- //

    function _bk() internal view returns (IBuckHolder) {
        return IBuckHolder(address(buck));
    }

    /// @notice Every policy budget/receipt is net-valued. Only the venue and
    /// ERC-20 boundary use nominal Carrying units. Legacy fixtures keep their
    /// original accounting; canonical Ordinary comparisons use the same path.
    function _marketLeg(uint256 i, bool sellBuck, uint256 amount)
        internal returns (uint256 spent, uint256 received)
    {
        if (netAccounting) return BasketMarketOps.monetaryLeg(address(buck), i, sellBuck, amount);
        return IBuckBasketVenue(address(this)).monetaryLeg(i, sellBuck, amount);
    }

    function _mintPosition(uint256 i, uint256 tokens, uint256 netBuck)
        internal returns (uint128 liquidity, uint256 tokenUsed, uint256 buckUsed)
    {
        if (netAccounting) return BasketMarketOps.positionMint(address(buck), i, tokens, netBuck);
        return IBuckBasketVenue(address(this)).positionMint(i, tokens, netBuck);
    }

    function _payCash(address to, uint256 value) internal {
        if (netAccounting) BasketMarketOps.payNet(address(buck), to, value);
        else IERC20(address(buck)).transfer(to, value);
    }

    function _cashAvailable() internal view returns (uint256) {
        return netAccounting ? BasketMarketOps.spendableNet(address(buck)) : _bk().balanceOf(address(this));
    }

    // --- Valuation ----------------------------------------------------------- //

    uint8 internal constant MARK_TWAP = 0;
    uint8 internal constant MARK_HIGH = 1;
    uint8 internal constant MARK_LOW  = 2;

    /// @notice One read of every pool's marks, K, and the basket's account at
    ///         Buck: everything a verb or a wheel step values is computed from
    ///         it, so a step reads each once.
    struct Snap {
        IBuckBasketVenue.Marks[] m;
        uint256 k;
        int256  buckEq;                  // canonical cash - principal + claim
        uint256 lien;                    // actual NFT principal in canonical mode
        uint256 relief;                  // marked unpaid claim, not credit collateral
        uint256 spend;                   // canonical cash; legacy held + headroom
        uint256 cash;                    // canonical actual net cash, separate from principal
    }

    function _k() internal view returns (uint256) {
        return IBuckKSource(address(controller)).currentBuckK();
    }

    /// @dev Pool i's marks, in net BUCK under net accounting: the venue nets
    ///      them (BuckBasketUniswapV3.marks), the one place they are netted.
    function _marks(uint256 i) internal view returns (IBuckBasketVenue.Marks memory) {
        return IBuckBasketVenue(address(this)).marks(i, liquidityOf[i]);
    }

    /// @dev Pre: s.m contains this snapshot's pool marks. Post: canonical cash,
    ///      principal and claim come from their own authoritative books; cash
    ///      changes never masquerade as principal repayment. Legacy fixtures
    ///      alone retain the old signed account calculation.
    function _account(Snap memory s) internal view {
        IBuckHolder b = _bk();
        if (netAccounting) {
            (s.cash, s.lien, s.relief, s.spend) = BasketJubileeMath.snapshot(
                address(buck), address(credit), creditId, _grossS(s, MARK_TWAP));
            s.buckEq = int256(s.cash) - int256(s.lien) + int256(s.relief);
            return;
        }
        int256 signed = b.signedBalanceOf(address(this));
        s.lien   = signed < 0 ? uint256(-signed) : 0;
        s.relief = b.reliefOf(address(this));
        s.buckEq = signed + int256(s.relief);
        s.spend  = b.balanceOf(address(this));
    }

    function _snap() internal view returns (Snap memory s) {
        uint256 n = constituents.length;
        s.m = new IBuckBasketVenue.Marks[](n);
        for (uint256 i = 0; i < n; i++) s.m[i] = _marks(i);
        s.k = _k();
        _account(s);
    }

    /// @notice Every position (fair value) and the wallet's TOKEN at `mark`.
    function _grossS(Snap memory s, uint8 mark) internal view returns (uint256 g) {
        for (uint256 i = 0; i < s.m.length; i++) {
            IBuckBasketVenue.Marks memory m = s.m[i];
            uint256 p = mark == MARK_HIGH ? m.pHigh : mark == MARK_LOW ? m.pLow : m.pTwap;
            g += mark == MARK_HIGH ? m.posHigh : mark == MARK_LOW ? m.posLow : m.posTwap;
            uint256 tok = idleToken[i];
            if (tok > 0) g += tok * p / (10 ** constituents[i].decimals);
        }
    }

    /// @notice The gross plus equity's BUCK (negative: the lien, net of relief).
    function _equityS(Snap memory s, uint8 mark) internal view returns (uint256) {
        int256 e = int256(_grossS(s, mark)) + s.buckEq;
        return e > 0 ? uint256(e) : 0;
    }

    /// @notice The liquidity target: the larger of `floorBp` of the gross and
    ///         enough that the band's floor holds z sigma of the daily net flow,
    ///         x (1 + K) (an exit takes its equity and its share of the lien) --
    ///         but never more than `ceilBp` of the gross: a young basket's
    ///         first deposits are its whole size, and a flow that large is
    ///         growth, not the churn the reserve is for.
    function _targetS(Snap memory s) internal view returns (uint256 t) {
        EquityParams memory p = eq;
        uint256 g = _grossS(s, MARK_TWAP);
        t = g * p.floorBp / 10000;
        if (p.flowZx100 > 0 && flowMs > 0) {
            uint256 sigma = _sqrt(flowMs * 1e18);
            uint256 fl = sigma * p.flowZx100 / 100 * (1e18 + s.k) / 1e18;
            uint256 t2 = fl * 10000 / (10000 - p.bandBp);
            uint256 ceil = g * p.ceilBp / 10000;
            if (t2 > ceil) t2 = ceil;
            if (t2 > t) t = t2;
        }
    }

    /// @notice Planning liquidity, separate from actual wallet cash (s.spend).
    ///         Canonical credit is only potential until _fundCash mints it.
    function _liquidityS(Snap memory s) internal view returns (uint256) {
        return s.spend + (netAccounting ? _issuableS(s) : 0);
    }

    /// @notice K x the credit's mark, less the lien (< 0: under water).  The
    ///         mark is the one the step just wrote (`_markS`).
    function _headroomS(Snap memory s) internal view returns (int256) {
        uint256 m = address(credit) == address(0) ? 0 : credit.markOf(creditId);
        if (netAccounting && address(credit) != address(0)) {
            m = _activatedValue(m);
        }
        return int256(s.k * m / 1e18) - int256(s.lien);
    }

    /// @notice What the wheel may place: the liquidity beyond its target.
    function _usableS(Snap memory s) internal view returns (uint256) {
        uint256 t = _targetS(s);
        uint256 liquid = _liquidityS(s);
        return liquid > t ? liquid - t : 0;
    }

    /// @dev Pre: canonical service basket, one self-insured zero-premium
    ///      MARKED NFT. Post: no state change; additional principal fits its
    ///      face/current mark, K, principal, account and supply bounds. A
    ///      matured facility or pending maturity book has no issuance room.
    ///      The unpaid Jubilee claim never enters this collateral calculation.
    function _issuableS(Snap memory s) internal view returns (uint256 room) {
        if (address(credit) == address(0)) return 0;
        IBuckNetAccount token = IBuckNetAccount(address(buck));
        if (token.maturitiesPending()) return 0;
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(creditId);
        if (principal != 0 && seconds_ == principal * (50 * (365 days + 6 hours))) return 0;
        (uint256 face,, uint32 rate) = credit.creditInfo(creditId);
        if (rate != 0) revert NonzeroBasketPremium();
        uint256 mark = Math.min(face, _creditValueS(s, _equityS(s, MARK_LOW)));
        uint256 limit = Math.mulDiv(mark, s.k, 1e18);
        if (principal >= limit) return 0;
        room = Math.min(limit - principal, BuckTypes.MAX_BALANCE - principal);
        (uint256 gross,) = token.accountBalancesScaled(address(this));
        room = Math.min(room, (BuckTypes.MAX_BALANCE * 1e36 - gross) / 1e36);
        room = Math.min(room, type(uint256).max / 1e36 - token.totalIssued());
    }

    /// @dev Pre: authorized upkeep, no position mutation in progress. Refresh
    ///      K and collateral before exercising credit. Post: return at most
    ///      desired, backed by actual cash; mint precisely its cash shortfall.
    ///      Activate the basket's whole free self-insured backing independently
    ///      of that draw, including when existing cash funds the purchase.
    ///      The returned actual coverage shortfall is checked after execution.
    ///      Failure of backing/mint/trade/postcheck reverts together.
    function _fundCash(uint256 desired, bool fresh) internal returns (uint256 funded, uint256 shortfall) {
        if (!netAccounting) return (Math.min(desired, _bk().balanceOf(address(this))), 0);
        controller.compute();
        Snap memory s = _snap();
        uint256 spend = s.spend;    // net: _markS leaves the nominal balance, which the legacy branch wants
        _markS(s);
        s.spend = spend;
        shortfall = _shortfallS(s);
        funded = Math.min(desired, fresh ? _issuableS(s) : _liquidityS(s));
        if (funded != 0 && address(credit) != address(0)) {
            (uint256 face, uint256 active, uint32 rate) = credit.creditInfo(creditId);
            if (rate != 0) revert NonzeroBasketPremium();
            uint256 coverage = face - active;
            uint256 draw = !fresh && funded > s.spend ? funded - s.spend : 0;
            (uint256 principal, uint256 age) = credit.issuanceOf(creditId);
            // A matured lien needs release; existing cash does not renew it.
            if (draw == 0 && principal != 0 && age == principal * (50 * (365 days + 6 hours))) coverage = 0;
            if (draw != 0 || coverage != 0) {
                IBuckNetAccount(address(buck)).issue(creditId, coverage, draw, 0, address(0));
            }
            if (fresh || draw != 0) creditLive = true;
        }
        if (!fresh) funded = Math.min(funded, _cashAvailable());
    }

    /// @dev Policy solvency using fresh bare collateral and the independently
    ///      configured held-LP-BUCK offset. Unpaid claims are not collateral.
    function _shortfallS(Snap memory s) internal view returns (uint256) {
        if (!netAccounting || address(credit) == address(0)) return 0;
        uint256 value = _activatedValue(_creditValueS(s, _equityS(s, MARK_LOW)));
        uint256 limit = Math.mulDiv(value, s.k, 1e18);
        return s.lien > limit ? s.lien - limit : 0;
    }

    /// @dev The exact marked/activated value enforced by BuckCredit. Shared
    ///      by cached headroom and prospective purchase/exit solvency checks.
    function _activatedValue(uint256 mark) internal view returns (uint256) {
        (uint256 face, uint256 active,) = credit.creditInfo(creditId);
        return face == 0 ? 0 : Math.mulDiv(Math.min(mark, face), active, face);
    }

    /// @dev Postcondition of a canonical purchase/pair: no greater credit
    ///      shortfall after actual fees, carried pool value and fresh marks.
    function _finishBuy(uint256 openingShortfall) internal {
        if (!netAccounting) return;
        Snap memory s = _snap();
        if (_shortfallS(s) > openingShortfall) revert CreditShortfallIncreased();
        _markS(s);
    }

    function _grainS(Snap memory s) internal view returns (uint256) {
        uint256 g = _grossS(s, MARK_TWAP) * eq.grainPpm / 1e6;
        return g > 1e3 ? g : 1e3;                       // a floor tiny at any BUCK decimals
    }

    /// @notice Mark collateral at LOW equity excluding the unpaid claim in
    ///         canonical mode, with the optional held-BUCK offset. Post: only
    ///         the appraised credit value changes; no cash/principal is issued.
    ///         The legacy fixture branch alone activates at its first mark.
    function _markS(Snap memory s) internal {
        _markAt(_creditValueS(s, _equityS(s, MARK_LOW)));
        s.spend = _bk().balanceOf(address(this));
    }

    /// @notice The BUCK side of every position, at the LOW marks: BUCKs the
    ///         basket drew and still holds.
    function _heldBuckS(Snap memory s) internal pure returns (uint256 held) {
        for (uint256 i = 0; i < s.m.length; i++) held += s.m[i].posLow / 2;
    }

    /// @notice What the credit is marked at for `equity`. Buck allows a lien
    ///         of K x the mark, so adding the held BUCKs over K makes that
    ///         K x equity + held: the lien net of them is what K x equity bounds.
    function _creditValueS(Snap memory s, uint256 equity) internal view returns (uint256) {
        // Approved policy: unpaid claims affect LP prices, never issuance
        // collateral. Preserve the independent held-BUCK offset below.
        if (netAccounting) equity = equity > s.relief ? equity - s.relief : 0;
        if (!netMark || s.k == 0) return equity;
        return equity + _heldBuckS(s) * 1e18 / s.k;
    }

    /// @dev Write the appraised value, leaving canonical issuance untouched.
    function _markAt(uint256 value) internal {
        IMarkedCredit c = credit;
        if (address(c) == address(0)) return;
        if (c.markOf(creditId) != value) c.mark(creditId, value);
        if (!netAccounting && !creditLive && value > 0) {
            // One mint for all the credit can give activates its whole face:
            // zero premium, so no deposit.
            uint256[] memory ids = new uint256[](1);
            ids[0] = creditId;
            _bk().mint(type(uint256).max, ids);
            creditLive = true;
        }
    }

    // The one-shot forms, for the views.
    function _gross(uint8 mark) internal view returns (uint256) { return _grossS(_snap(), mark); }
    function _equity(uint8 mark) internal view returns (uint256) { return _equityS(_snap(), mark); }
    function _target() internal view returns (uint256) { return _targetS(_snap()); }

    /// @notice The most BUCK (value) one wheel swap may move in pool i: a
    ///         large deposit is placed over many ticks, and the arbitrage
    ///         re-pins the pool between them.
    function _swapCap(IBuckBasketVenue.Marks memory m) internal view returns (uint256) {
        return m.depth * eq.swapCapBp / 10000;
    }

    function _sqrt(uint256 x) internal pure returns (uint256 y) {
        if (x == 0) return 0;
        uint256 z = (x + 1) / 2;
        y = x;
        while (z < y) { y = z; z = (x / z + z) / 2; }
    }
}
