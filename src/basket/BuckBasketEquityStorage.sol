// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";
import {BuckBasketStorage}  from "./BuckBasketStorage.sol";

interface IBuckKSource {
    function currentBuckK() external view returns (uint256);   // 1e18
}

/// @notice The director the equity basket consults: leaning targets and the
///         two gates (EquityTurnDirector).  Optional: with none set the basket
///         keeps its declared targets and a plain band.
interface IEquityDirector {
    function observe() external;
    function targetsBp() external view returns (uint256[] memory);
    function mayTrim(uint256 i) external view returns (bool);
    function mayFund(uint256 i) external view returns (bool);
}

/// @notice Buck, as the basket sees it: an ordinary credit holder's account.
interface IBuckHolder {
    function signedBalanceOf(address a) external view returns (int256);
    function balanceOf(address a) external view returns (uint256);
    function reliefOf(address a) external view returns (uint256);
    function settleRelief() external;
    function mint(uint256 amount, uint256[] calldata tokenIds) external;
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
///         The basket is an ordinary BUCK credit holder.  It holds one
///         self-issued MARKED BuckCredit, marked at its equity, so Buck
///         enforces its limit: creditLimit = K x equity.  Its debt is its
///         lien -- its negative balance -- and it earns Jubilee relief on it
///         like any issuer.  It never mints or burns: spending past its held
///         BUCK issues, and BUCK it receives repay the lien.
///
///         The TOKEN books are explicit: the wallet (`idleToken`) and each
///         pool's liquidity (`liquidityOf`) are the basket's own counters,
///         never its balances or its V3 position.  Its BUCK is one signed
///         account at Buck, the depositors' alone: equity's BUCK is that
///         balance plus the relief accrued on it.  Nothing else spends from
///         it -- a monetary desk is its own credit holder (`EquityDesk`),
///         with its own account, mark and wheel -- so its limit is the
///         depositors' K x equity and no one else's collateral.
///
///         Invariants (the tests check E1, E2, E4, E5 and E6 directly):
///           E1  equity(mark) = gross(mark) + signedBalance + reliefAccrued,
///               clamped at zero (`_equityS`)
///           E2  every spend is preceded by a mark at equity(LOW) and nothing
///               else (`_markS` / `_markAt`), so Buck holds every issuance
///               within K x the depositors' equity
///           E3  the lien moves only by the basket's own spends, BUCK it
///               receives, and relief collected: a K cut calls nothing back
///           E4  an exit takes at most its fraction: in BUCK, its value at
///               LOW less the charge; in kind, exactly its fraction of the
///               positions and wallet TOKEN, its lien share repaid
///           E5  token.balanceOf(basket) >= idleToken[i]
///           E6  after an in-kind exit the mark is read afresh, positions gone
///
///         Value is in BUCK.  A position's fair value is twice its BUCK side
///         at the chosen price (2 L sqrtP, full range).  The marks are: TWAP
///         (the price), HIGH (each pool at the higher of spot and TWAP: the
///         incumbents' side of an entry) and LOW (the lower: their side of an
///         exit).
abstract contract BuckBasketEquityStorage is BuckBasketStorage {

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
    address public equityDirector;       // optional IEquityDirector
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

    // --- Buck and the credit --------------------------------------------------- //

    function _bk() internal view returns (IBuckHolder) {
        return IBuckHolder(address(buck));
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
        int256  buckEq;                  // equity's BUCK: the account + relief
        uint256 lien;                    // the basket's lien (0 when it holds BUCK)
        uint256 relief;                  // relief accrued on the lien, unpaid
        uint256 spend;                   // what the account can spend: held + headroom
    }

    function _k() internal view returns (uint256) {
        return IBuckKSource(address(controller)).currentBuckK();
    }

    function _marks(uint256 i) internal view returns (IBuckBasketVenue.Marks memory) {
        return IBuckBasketVenue(address(this)).marks(i, liquidityOf[i]);
    }

    /// @dev Read the basket's account at Buck into `s`.
    function _account(Snap memory s) internal view {
        IBuckHolder b = _bk();
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

    /// @notice Liquidity: what the account can spend -- the BUCK held plus the
    ///         headroom under K x equity.  Buck computes it.
    function _liquidityS(Snap memory s) internal pure returns (uint256) {
        return s.spend;
    }

    /// @notice K x the credit's mark, less the lien (< 0: under water).  The
    ///         mark is the one the step just wrote (`_markS`).
    function _headroomS(Snap memory s) internal view returns (int256) {
        uint256 m = address(credit) == address(0) ? 0 : credit.markOf(creditId);
        return int256(s.k * m / 1e18) - int256(s.lien);
    }

    /// @notice What the wheel may place: the liquidity beyond its target.
    function _usableS(Snap memory s) internal view returns (uint256) {
        uint256 t = _targetS(s);
        return s.spend > t ? s.spend - t : 0;
    }

    function _grainS(Snap memory s) internal view returns (uint256) {
        uint256 g = _grossS(s, MARK_TWAP) * eq.grainPpm / 1e6;
        return g > 1e3 ? g : 1e3;                       // a floor tiny at any BUCK decimals
    }

    /// @notice Mark the credit at the basket's equity (the exiters' LOW
    ///         marks), activating it the first time the mark is above zero,
    ///         then re-read what the account can spend.  Every verb and wheel
    ///         step marks before it spends (E2).
    function _markS(Snap memory s) internal {
        _markAt(_equityS(s, MARK_LOW));
        s.spend = _bk().balanceOf(address(this));
    }

    /// @dev Mark the credit at `value` (activating it the first time it is
    ///      above zero).
    function _markAt(uint256 value) internal {
        IMarkedCredit c = credit;
        if (address(c) == address(0)) return;
        if (c.markOf(creditId) != value) c.mark(creditId, value);
        if (!creditLive && value > 0) {
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
