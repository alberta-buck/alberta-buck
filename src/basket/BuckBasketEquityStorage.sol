// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";
import {BuckBasketStorage}  from "./BuckBasketStorage.sol";

interface IBuckKSource {
    function currentBuckK() external view returns (uint256);   // 1e18
}

/// @notice The director the equity basket consults (doc/BASKET-EQUITY.org
///         13.6): leaning targets and the two gates.  Optional: with none set
///         the basket keeps its declared targets and a plain band.
interface IEquityDirector {
    function observe() external;
    function targetsBp() external view returns (uint256[] memory);
    function mayTrim(uint256 i) external view returns (bool);
    function mayFund(uint256 i) external view returns (bool);
}

/// @title BuckBasketEquityStorage -- the equity basket's books, appended to the
///        shared basket layout, and the valuation both of its contracts use.
///
/// @notice doc/BASKET-EQUITY.org section 13.6.  The shell (`BuckBasketEquity`)
///         and its components facet (`BuckBasketEquityWheel`) inherit this and
///         nothing else, so they share one layout; the venue facet
///         (`BuckBasketUniswapV3`) inherits only `BuckBasketStorage`, of which
///         this is a strict extension, so it sees the same slots it always did.
///
///         Books are explicit.  The wallet (`idleBuck`, `idleToken`) and each
///         pool's liquidity (`liquidityOf`) are the basket's own counters, never
///         its balances or its V3 position: the desk's inventory, stray
///         transfers and liquidity others mint to the basket's position key are
///         never counted.  Pool reads are for prices only.
///
///         Value is in BUCK (1e18).  A position's fair value is twice its BUCK
///         side at the chosen price (2 L sqrtP, full range).  The marks are:
///         TWAP (the price), HIGH (each pool at the higher of spot and TWAP:
///         the incumbents' side of an entry) and LOW (the lower: their side of
///         an exit).
abstract contract BuckBasketEquityStorage is BuckBasketStorage {

    // --- The equity books (appended) ---------------------------------------- //

    struct Holding {
        uint128 shares;                  // 1e18
        uint128 basis;                   // BUCK brought, less what has left (1e18)
    }
    mapping(uint256 => Holding) public holdings;

    uint256 public totalShares;          // receipts' + the treasury's
    uint256 public treasuryShares;       // the basket's own: the 25% cuts
    uint256 public debt;                 // BUCK the equity path minted and has not burned
    uint256 public mintedTotal;          // ghost counters: mintedTotal - burnedTotal == debt
    uint256 public burnedTotal;
    uint256 public idleBuck;             // the wallet's BUCK
    mapping(uint256 => uint256) public idleToken;     // the wallet's TOKEN_i, native decimals
    mapping(uint256 => uint128) public liquidityOf;   // the basket's own count per pool
    uint256 public owed;                 // exits' debt shares and mints the wheel must burn
    uint256 public flowMs;               // EMA of the daily net flow squared (BUCK^2, 1e36)
    int256  public dayFlow;              // today's net flow (BUCK)
    uint64  public lastDay;              // the day Daily last ran (block.timestamp / 1 days)
    address public equityWheel;          // the components facet
    address public equityDirector;       // optional IEquityDirector
    mapping(uint256 => uint64) public lastSyncDay;    // Sync: once a day per pool

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

    event EquityDeposited(address indexed who, uint256 indexed id, address asset,
                          uint256 amount, uint256 value, uint256 shares, uint256 credit);
    event EquityRedeemed(address indexed who, uint256 indexed id, uint256 shares,
                         uint256 cutShares, uint256 paid, bool proRata);
    event WalletCredited(address indexed token, uint256 amount);
    event WheelWork(uint8 indexed kind, uint256 indexed i, uint256 amount);

    error MinOut();
    error NotEquityWheel();

    // --- Valuation ----------------------------------------------------------- //

    uint8 internal constant MARK_TWAP = 0;
    uint8 internal constant MARK_HIGH = 1;
    uint8 internal constant MARK_LOW  = 2;

    /// @notice One read of every pool's marks, and K: everything a verb or a
    ///         wheel step values is computed from it, so a step reads each pool
    ///         once.
    struct Snap {
        IBuckBasketVenue.Marks[] m;
        uint256 k;
    }

    function _k() internal view returns (uint256) {
        return IBuckKSource(address(controller)).currentBuckK();
    }

    function _marks(uint256 i) internal view returns (IBuckBasketVenue.Marks memory) {
        return IBuckBasketVenue(address(this)).marks(i, liquidityOf[i]);
    }

    function _snap() internal view returns (Snap memory s) {
        uint256 n = constituents.length;
        s.m = new IBuckBasketVenue.Marks[](n);
        for (uint256 i = 0; i < n; i++) s.m[i] = _marks(i);
        s.k = _k();
    }

    /// @notice The wallet and every position (fair value) at `mark`, in BUCK.
    function _grossS(Snap memory s, uint8 mark) internal view returns (uint256 g) {
        g = idleBuck;
        for (uint256 i = 0; i < s.m.length; i++) {
            IBuckBasketVenue.Marks memory m = s.m[i];
            uint256 p = mark == MARK_HIGH ? m.pHigh : mark == MARK_LOW ? m.pLow : m.pTwap;
            g += mark == MARK_HIGH ? m.posHigh : mark == MARK_LOW ? m.posLow : m.posTwap;
            uint256 tok = idleToken[i];
            if (tok > 0) g += tok * p / (10 ** constituents[i].decimals);
        }
    }

    function _equityS(Snap memory s, uint8 mark) internal view returns (uint256) {
        uint256 g = _grossS(s, mark);
        return g > debt ? g - debt : 0;
    }

    /// @notice BUCK the basket may mint on demand: K x equity - debt (<= 0: none).
    function _headroomS(Snap memory s) internal view returns (int256) {
        return int256(s.k * _equityS(s, MARK_TWAP) / 1e18) - int256(debt);
    }

    function _headroomPosS(Snap memory s) internal view returns (uint256) {
        int256 h = _headroomS(s);
        return h > 0 ? uint256(h) : 0;
    }

    /// @notice The liquidity target: the larger of `floorBp` of the gross and
    ///         enough that the band's floor holds z sigma of the daily net flow,
    ///         x (1 + K) (an exit takes its equity and its share of the debt) --
    ///         but never more than `ceilBp` of the gross: a young basket's
    ///         first deposits are its whole size, and a flow that large is
    ///         growth, not the churn the reserve is for.
    function _targetS(Snap memory s) internal view returns (uint256 t) {
        EquityParams memory p = eq;
        uint256 g = _grossS(s, MARK_TWAP);
        t = g * p.floorBp / 10000;
        if (p.flowZx100 > 0 && flowMs > 0) {
            uint256 sigma = _sqrt(flowMs * 1e18);               // BUCK, 1e18
            uint256 fl = sigma * p.flowZx100 / 100 * (1e18 + s.k) / 1e18;
            uint256 t2 = fl * 10000 / (10000 - p.bandBp);
            uint256 ceil = g * p.ceilBp / 10000;
            if (t2 > ceil) t2 = ceil;
            if (t2 > t) t = t2;
        }
    }

    /// @notice Liquidity: the BUCK held plus the positive headroom.
    function _liquidityS(Snap memory s) internal view returns (uint256) {
        return idleBuck + _headroomPosS(s);
    }

    /// @notice BUCK held for liquidity: what the target lacks in headroom.
    function _keepS(Snap memory s) internal view returns (uint256) {
        uint256 t = _targetS(s);
        uint256 h = _headroomPosS(s);
        return t > h ? t - h : 0;
    }

    function _grainS(Snap memory s) internal view returns (uint256) {
        uint256 g = _grossS(s, MARK_TWAP) * eq.grainPpm / 1e6;
        return g > 1e12 ? g : 1e12;
    }

    // The one-shot forms, for the views.
    function _gross(uint8 mark) internal view returns (uint256) { return _grossS(_snap(), mark); }
    function _equity(uint8 mark) internal view returns (uint256) { return _equityS(_snap(), mark); }
    function _headroom() internal view returns (int256) { return _headroomS(_snap()); }
    function _target() internal view returns (uint256) { return _targetS(_snap()); }
    function _liquidity() internal view returns (uint256) { return _liquidityS(_snap()); }
    function _keep() internal view returns (uint256) { return _keepS(_snap()); }

    // --- Mint and burn (the only two ways debt moves) ------------------------ //

    function _mint(uint256 amount) internal {
        if (amount == 0) return;
        buck.mintFromBasket(address(this), amount);
        debt += amount;
        mintedTotal += amount;
        idleBuck += amount;
    }

    function _burn(uint256 amount) internal {
        if (amount == 0) return;
        buck.burnFromBasket(amount);
        debt -= amount;
        burnedTotal += amount;
        idleBuck -= amount;
        owed = owed > amount ? owed - amount : 0;
    }

    /// @notice BUCK at rest repays what exits owe (and nothing else: deposits'
    ///         BUCK and credit wait for Fund).
    function _settle() internal {
        uint256 b = idleBuck < owed ? idleBuck : owed;
        if (b > debt) b = debt;
        _burn(b);
    }

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
