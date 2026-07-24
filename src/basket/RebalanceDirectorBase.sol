// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}             from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IUniswapV3Pool}     from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";
import {IRebalanceDirector, IBasketMeta} from "./IRebalanceDirector.sol";

/// @title RebalanceDirectorBase -- the shared chassis of a rebalance advisor.
///
/// @notice Everything policy-agnostic lives here, because this is where the
///         audited amortization invariants live: the epoch clock, the
///         round-robin work wheel, constituent metadata sync, the cached
///         per-pool observations with running sums (a poke reads only its
///         own pool), the O(log n) fixed-point power for closed-form EMA
///         catch-up, and the advisory-read surface built over the derived
///         policy's `effortOf`.  A signal engine (the vrate
///         `BasketRebalanceDirector`, the differential-mode
///         `PairsRebalanceDirector`) supplies only `_isStale` / `_pokeOne` /
///         `effortOf` -- the seam sits exactly at the tested boundary.
abstract contract RebalanceDirectorBase is IRebalanceDirector {

    struct Meta {                       // static per-constituent (sync'd)
        address pool;
        address token;
        uint8   decimals;
        bool    buckIsToken0;
        uint256 basketAmount;           // 18-dec
        uint256 initialPriceInBuck;     // 18-dec
    }

    IBasketMeta public immutable basket;
    address     public immutable buck;
    address     public governance;

    uint32  public genesisTime;         // epoch 0 anchor
    uint32  public epochSeconds;        // sampling cadence
    uint256 public constituentCount;
    uint256 public cursor;              // round-robin wheel position

    mapping(uint256 => Meta) public metaOf;

    // Cached per-pool observations + running sums: deviations are computed
    // against slightly-stale totals, which a moving-average policy is
    // insensitive to by construction.
    mapping(uint256 => uint128) public bvOf;   // pool BUCK reserve
    mapping(uint256 => uint128) public sOf;    // price-scaled target share
    uint256 public runningB;
    uint256 public runningS;

    int256 internal constant ONE = 1e18;

    event Synced(uint256 constituents);

    error NotGovernance();
    error BadParams();
    error NotSynced();

    modifier onlyGov() {
        if (msg.sender != governance) revert NotGovernance();
        _;
    }

    constructor(address _basket, address _governance, uint32 _epochSeconds) {
        if (_epochSeconds == 0) revert BadParams();
        basket       = IBasketMeta(_basket);
        buck         = IBasketMeta(_basket).buck();
        governance   = _governance;
        epochSeconds = _epochSeconds;
        genesisTime  = uint32(block.timestamp);
    }

    function setGovernance(address g) external onlyGov { governance = g; }

    // --- Sync (permissionless) --------------------------------------------- //

    /// @notice Pull/refresh constituent metadata from the basket.  Re-callable
    ///         any time: `addBasketToken` renormalizes existing weights and
    ///         amounts, and new constituents start with fresh signals.
    function syncConstituents() external {
        uint256 n = basket.constituentsLength();
        for (uint256 i = 0; i < n; i++) {
            (address token, uint8 decimals, uint256 amount, uint256 p0,,
             address pool,,, bool buckIs0,,) = basket.constituents(i);
            metaOf[i] = Meta({
                pool: pool, token: token, decimals: decimals,
                buckIsToken0: buckIs0, basketAmount: amount,
                initialPriceInBuck: p0
            });
        }
        constituentCount = n;
        emit Synced(n);
    }

    // --- The work wheel ------------------------------------------------------ //

    function epochNow() public view returns (uint32) {
        return uint32((block.timestamp - genesisTime) / epochSeconds);
    }

    /// @notice How many constituents are stale (worth poking) this epoch.
    function pending() external view returns (uint256 n) {
        uint32 e = epochNow();
        for (uint256 i = 0; i < constituentCount; i++) {
            if (_isStale(i, e)) n++;
        }
    }

    /// @notice Advance up to `maxWork` stale constituents' signals, starting
    ///         at the wheel cursor.  Anyone may call; basket activations are
    ///         expected to carry a small budget (1-2), keepers may pass N.
    ///         Already-fresh constituents cost ~2 warm SLOADs to skip.
    function poke(uint256 maxWork) public returns (uint256 advanced) {
        uint256 n = constituentCount;
        if (n == 0) revert NotSynced();
        uint32 e = epochNow();
        uint256 c = cursor;
        for (uint256 step = 0; step < n && advanced < maxWork; step++) {
            uint256 i = (c + step) % n;
            if (!_isStale(i, e)) continue;
            _pokeOne(i, e);
            advanced++;
            cursor = (i + 1) % n;
        }
    }

    /// @notice Advance everything (keeper convenience).
    function pokeAll() external returns (uint256) {
        return poke(constituentCount);
    }

    /// @dev Policy hook: does constituent `i` need advancing at epoch `e`?
    function _isStale(uint256 i, uint32 e) internal view virtual returns (bool);

    /// @dev Policy hook: advance constituent `i` to epoch `e` (observe, fold,
    ///      closed-form catch-up, effort update).
    function _pokeOne(uint256 i, uint32 e) internal virtual;

    // --- Observation helpers --------------------------------------------------- //

    /// @dev Observe THIS pool only: its BUCK reserve (the value sufficient
    ///      statistic; full-range pool value = 2*bv), its price-scaled target
    ///      share s = basketAmount * P0^2 / spot (the same fixed-quantity-
    ///      index target `_allocateSellHigh` uses), and the pool tick
    ///      normalized so + always means TOKEN appreciating in BUCK (a tick
    ///      IS a log price -- signal ladders can run on it directly).
    function _observe(uint256 i)
        internal view returns (uint256 bvNew, uint256 sNew, int24 normTick)
    {
        Meta storage mt = metaOf[i];
        bvNew = IERC20(buck).balanceOf(mt.pool);
        (, int24 tick,,,,,) = IUniswapV3Pool(mt.pool).slot0();
        uint256 spot = UniswapV3OracleLib.getQuoteAtTick(
            tick, uint128(10 ** mt.decimals), mt.token, buck);
        if (spot != 0) {
            uint256 base_ = UniswapV3OracleLib.mulDiv(
                mt.basketAmount, mt.initialPriceInBuck, 1e18);
            sNew = UniswapV3OracleLib.mulDiv(base_, mt.initialPriceInBuck, spot);
        }
        normTick = mt.buckIsToken0 ? -tick : tick;
    }

    /// @dev Fold a fresh observation into the caches by difference.
    function _fold(uint256 i, uint256 bvNew, uint256 sNew) internal {
        runningB = runningB - bvOf[i] + bvNew;
        runningS = runningS - sOf[i] + sNew;
        bvOf[i] = uint128(bvNew);
        sOf[i]  = uint128(sNew);
    }

    /// @dev Weight ratio (w_i / w*_i) in 1e18: cached share over cached
    ///      target share, against the (slightly stale) running sums.
    function _ratio1e18(uint256 i) internal view returns (uint256) {
        uint256 bv = bvOf[i];
        uint256 s = sOf[i];
        if (bv == 0 || s == 0 || runningB == 0 || runningS == 0) return 1e18;
        uint256 num = UniswapV3OracleLib.mulDiv(bv, runningS, runningB);
        return UniswapV3OracleLib.mulDiv(num, 1e18, s);
    }

    /// @notice Current raw deviation of constituent `i` from its cached
    ///         observations (diagnostic; the sim/TUI reads this).
    function deviationOf(uint256 i) public view returns (int256) {
        return int256(_ratio1e18(i)) - ONE;
    }

    /// @dev (1e18 fixed) base^n by binary exponentiation; O(log n).
    function _pow1e18(int256 base_, uint32 n) internal pure returns (int256 r) {
        r = ONE;
        int256 b = base_;
        if (b < 0) b = 0;
        while (n > 0) {
            if (n & 1 == 1) r = (r * b) / ONE;
            b = (b * b) / ONE;
            n >>= 1;
        }
    }

    // --- Advisory reads (over the policy's effortOf) ---------------------------- //

    function effortOf(uint256 i) public view virtual returns (int256);

    function effortsAll() external view returns (int256[] memory out) {
        out = new int256[](constituentCount);
        for (uint256 i = 0; i < constituentCount; i++) {
            out[i] = effortOf(i);
        }
    }

    /// @notice Pool that incoming BUCK should be invested into: the strongest
    ///         positive (buy-side) effort.  `type(uint256).max` = no signal --
    ///         fall back to the venue's default most-underweight routing.
    function depositHint() external view returns (uint256 best) {
        best = type(uint256).max;
        int256 bestE = 0;
        for (uint256 i = 0; i < constituentCount; i++) {
            int256 e = effortOf(i);
            if (e > bestE) { bestE = e; best = i; }
        }
    }

    /// @notice Pool a redemption is best drawn from: the strongest negative
    ///         (sell-side) effort.  `type(uint256).max` = no signal.
    function redeemHint() external view returns (uint256 best) {
        best = type(uint256).max;
        int256 bestE = 0;
        for (uint256 i = 0; i < constituentCount; i++) {
            int256 e = effortOf(i);
            if (e < bestE) { bestE = e; best = i; }
        }
    }
}
