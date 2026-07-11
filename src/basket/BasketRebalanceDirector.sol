// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}             from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IUniswapV3Pool}     from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";

/// @dev The slice of BuckBasketProRata the director reads (public getters).
interface IBasketMeta {
    function constituents(uint256 i) external view returns (
        address token, uint8 decimals, uint256 basketAmount,
        uint256 initialPriceInBuck, uint24 feeTier, address pool,
        int24 tickLower, int24 tickUpper, bool buckIsToken0,
        uint256 targetWeightBp, uint128 treasuryLiquidity);
    function constituentsLength() external view returns (uint256);
    function buck() external view returns (address);
}

/// @title BasketRebalanceDirector -- amortized rebalance-signal state machine.
///
/// @notice A standalone *advisor* for a BuckBasket (`BuckBasketProRata`): it
///         watches the basket's constituent AMM pools, maintains per-pool
///         moving-average deviation signals (the `vrate` policy modeled in
///         alberta_buck/sim/rebalance_policy.py), and aggregates them into
///         O(1) advisory reads:
///
///           * `depositHint()` -- which pool incoming BUCK should be invested
///             into (`investFromBucks` pool hint);
///           * `redeemHint()`  -- which pool a redemption is best drawn from;
///           * `effortOf(i)`   -- signed per-epoch rebalance effort (bp of
///             NAV) for keepers executing standalone rebalance steps.
///
///         The director holds no funds and executes no trades.  Its hints are
///         *advisory*: the basket's venue re-verifies everything it does (the
///         spot/TWAP deviation guard, liquidity bounds), so a stale or even
///         adversarial hint can only re-order flows, never break solvency.
///
/// # The amortization pattern
///
///         Signals must advance with time, but no one pays for a clock.  The
///         standard EVM answer (Compound's `accrueInterest`, Maker's `drip`,
///         Uniswap's oracle writes) is *lazy time-indexed accrual*: state is
///         advanced on first touch as a closed-form function of elapsed time.
///         For EMAs the closed form is exact under a sample-and-hold
///         assumption: after `n` missed epochs with current deviation `x`,
///
///             m' = x + (m - x) * (1-beta)^n
///
///         computed in O(log n) by binary exponentiation -- so ONE poke after
///         a quiet week costs (almost) the same as one poke after a quiet
///         hour, and the signal lands in the same place either way.
///
///         Across constituents the work is sharded by a *round-robin wheel*:
///         `poke(maxWork)` advances at most `maxWork` stale constituents
///         starting at a persistent cursor.  Every basket activation
///         (deposit, redeem, treasury sweep, or a bare keeper trigger) can
///         carry a small work budget: the more granular the triggering, the
///         fewer constituents each caller advances and the less gas each
///         pays; infrequent activity concentrates the same total work into
///         whoever shows up next.  Work is conserved -- granularity trades
///         per-call gas against signal staleness, never total cost.
///
///         Pool observations are cached (`bv`, `s`) with running sums (`B`,
///         `S`), so a poke touches ONLY its own pool (one `balanceOf`, one
///         `slot0`) and updates the aggregates by difference -- deviations
///         are computed against slightly-stale sums, which a moving-average
///         policy is insensitive to by construction.
///
/// # The signal (the `vrate` policy)
///
///         delta_i = (bv_i/B) / (s_i/S) - 1     price-scaled share deviation
///         m_i     = EMA_X(delta_i)             the delayed view
///         regime  = sign(m)-relative velocity of m: receding | level | gaining
///         effort  = rho x observed closure rate (1-week EMA of d(delta)),
///                   floored at "half the gap in one window", capped, only
///                   when sign(m) == sign(delta) and not receding; a raw
///                   |delta| leash overrides the quench beyond leashBp.
contract BasketRebalanceDirector {

    // --- Types ------------------------------------------------------------ //

    struct Meta {                       // static per-constituent (sync'd)
        address pool;
        address token;
        uint8   decimals;
        bool    buckIsToken0;
        uint256 basketAmount;           // 18-dec
        uint256 initialPriceInBuck;     // 18-dec
    }

    struct Signal {                     // hot per-constituent state
        int128  m;                      // EMA_X of deviation (1e18)
        int128  vEma;                   // EMA_X of per-epoch d(m) (1e18)
        int128  ddotEma;                // 1-week EMA of per-epoch d(delta)
        int128  prevDelta;              // last sampled raw deviation (1e18)
        uint128 bv;                     // cached pool BUCK reserve
        uint128 s;                      // cached price-scaled target share
        int32   effortBp;               // signed advisory effort (bp of NAV)
        uint32  lastEpoch;
        uint32  firstEpoch;
        bool    leashed;
    }

    struct Params {
        uint32  epochSeconds;           // sampling cadence (86400 = daily)
        uint32  windowEpochs;           // X: MA window, in epochs
        uint64  rho1e9;                 // match ratio (1e9; 3e9 = 3.0)
        uint64  deadband1e9;            // min |delta| to act (1e9; 15e6 = 1.5%)
        uint64  epsFrac1e9;             // level-regime band as frac of |m|/X
        uint64  leash1e9;               // raw-|delta| forced-rebalance bound
        uint64  leashInner1e9;          // leash release (hysteresis)
        uint32  capBpPerEpoch;          // max effort per constituent per epoch
    }

    // --- Storage ------------------------------------------------------------ //

    IBasketMeta public immutable basket;
    address     public immutable buck;
    address     public governance;

    Params  public params;
    uint32  public genesisTime;         // epoch 0 anchor
    uint256 public constituentCount;

    mapping(uint256 => Meta)   public metaOf;
    mapping(uint256 => Signal) public signalOf;
    uint256 public runningB;            // sum of cached bv
    uint256 public runningS;            // sum of cached s
    uint256 public cursor;              // round-robin wheel position

    int256 internal constant ONE = 1e18;
    int256 internal constant LN2 = 693147180559945309;   // ln(2) * 1e18

    event Synced(uint256 constituents);
    event Poked(uint256 indexed i, uint32 epoch, int256 delta, int256 m, int32 effortBp);
    event ParamsSet(Params p);

    error NotGovernance();
    error BadParams();
    error NotSynced();

    modifier onlyGov() {
        if (msg.sender != governance) revert NotGovernance();
        _;
    }

    constructor(address _basket, address _governance, Params memory p) {
        basket     = IBasketMeta(_basket);
        buck       = IBasketMeta(_basket).buck();
        governance = _governance;
        _setParams(p);
        genesisTime = uint32(block.timestamp);
    }

    // --- Governance --------------------------------------------------------- //

    function setGovernance(address g) external onlyGov { governance = g; }

    function setParams(Params calldata p) external onlyGov { _setParams(p); }

    function _setParams(Params memory p) internal {
        if (p.epochSeconds == 0 || p.windowEpochs < 2
            || p.leashInner1e9 > p.leash1e9) revert BadParams();
        params = p;
        emit ParamsSet(p);
    }

    // --- Sync (permissionless) ----------------------------------------------- //

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

    // --- The work wheel -------------------------------------------------------- //

    function epochNow() public view returns (uint32) {
        return uint32((block.timestamp - genesisTime) / params.epochSeconds);
    }

    /// @notice How many constituents are stale (worth poking) this epoch.
    function pending() external view returns (uint256 n) {
        uint32 e = epochNow();
        for (uint256 i = 0; i < constituentCount; i++) {
            if (signalOf[i].lastEpoch < e || signalOf[i].firstEpoch == 0) n++;
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
            Signal storage sig = signalOf[i];
            if (sig.firstEpoch != 0 && sig.lastEpoch >= e) continue;
            _pokeOne(i, sig, e);
            advanced++;
            cursor = (i + 1) % n;
        }
    }

    /// @notice Advance everything (keeper convenience).
    function pokeAll() external returns (uint256) {
        return poke(constituentCount);
    }

    // --- Signal update ------------------------------------------------------ //

    function _pokeOne(uint256 i, Signal storage sig, uint32 e) internal {
        Meta storage mt = metaOf[i];

        // Observe THIS pool only; fold into the running aggregates by diff.
        uint256 bvNew = IERC20(buck).balanceOf(mt.pool);
        uint256 sNew  = _targetShare(mt);
        runningB = runningB - sig.bv + bvNew;
        runningS = runningS - sig.s + sNew;
        sig.bv = uint128(bvNew);
        sig.s  = uint128(sNew);

        // First touch: initialize and wait out the warmup.
        if (sig.firstEpoch == 0) {
            int256 d0 = _deviation(bvNew, sNew);
            sig.m = int128(d0);
            sig.prevDelta = int128(d0);
            sig.firstEpoch = e + 1;      // +1 so 0 keeps meaning "never"
            sig.lastEpoch = e;
            emit Poked(i, e, d0, d0, 0);
            return;
        }

        uint32 dn = e - sig.lastEpoch;   // caller guarantees >= 1
        if (dn > 3650) dn = 3650;        // signals fully converged past this
        int256 delta = _deviation(bvNew, sNew);

        // Closed-form catch-up over dn epochs, EXACTLY reproducing dn
        // per-epoch EMA updates under sample-and-hold on delta.  With
        // q = 1-beta, the diligent recursions collapse to:
        //   m'    = delta + (m - delta) * q^n
        //   v'    = q^n * v - (1-q)^2 * n * q^(n-1) * (m - delta)
        //   ddot' = q7^(n-1) * ((delta - prevDelta)*(1-q7) + q7 * ddot)
        // (v: each gap epoch's dm is -(1-q)q^(k-1)(m-delta); ddot: only the
        // first gap epoch sees a raw-delta change, the rest decay.)
        Params memory p = params;
        int256 q = ONE - 2 * ONE / int256(uint256(p.windowEpochs) + 1);
        int256 qn1 = _pow1e18(q, dn - 1);
        int256 qn = (qn1 * q) / ONE;
        int256 m0 = sig.m;
        int256 m1 = delta + ((m0 - delta) * qn) / ONE;
        sig.m = int128(m1);
        int256 t = ((ONE - q) * (ONE - q)) / ONE;
        t = (t * int256(uint256(dn)) * qn1) / ONE;
        sig.vEma = int128((int256(sig.vEma) * qn) / ONE - (t * (m0 - delta)) / ONE);

        int256 q7 = ONE - 2 * ONE / 8;                     // 1-week EMA
        int256 inner = ((delta - int256(sig.prevDelta)) * (ONE - q7)) / ONE
            + (q7 * int256(sig.ddotEma)) / ONE;
        sig.ddotEma = int128((_pow1e18(q7, dn - 1) * inner) / ONE);
        sig.prevDelta = int128(delta);
        sig.lastEpoch = e;

        sig.effortBp = _effort(sig, p, delta, m1, e);
        emit Poked(i, e, delta, m1, sig.effortBp);
    }

    /// @dev The vrate policy: velocity-regime gate + rate-matched sizing.
    ///      Positive effort = pool wants funds (buy/deposit side); negative =
    ///      pool should be drawn down (sell/redeem side).
    function _effort(Signal storage sig, Params memory p, int256 delta,
                     int256 m, uint32 e) internal returns (int32) {
        int256 absD = delta >= 0 ? delta : -delta;

        // Leash: beyond leash1e9 the mandate overrides the quench.
        int256 leash  = int256(uint256(p.leash1e9)) * 1e9;
        int256 inner  = int256(uint256(p.leashInner1e9)) * 1e9;
        bool wasLeashed = sig.leashed;
        sig.leashed = wasLeashed ? absD > inner : absD > leash;
        if (sig.leashed) {
            return delta > 0 ? -int32(p.capBpPerEpoch) : int32(p.capBpPerEpoch);
        }

        // Warmup, deadband, and raw-vs-MA sign agreement.
        if (e - sig.firstEpoch < p.windowEpochs) return 0;
        if (absD < int256(uint256(p.deadband1e9)) * 1e9) return 0;
        if (m * delta <= 0) return 0;

        // Regime from the MA's velocity: u > 0 = the delayed view is closing.
        int256 u = m > 0 ? -int256(sig.vEma) : int256(sig.vEma);
        int256 eps = (int256(uint256(p.epsFrac1e9)) * 1e9 * (m >= 0 ? m : -m))
            / ONE / int256(uint256(p.windowEpochs));
        if (u < -eps) return 0;                          // receding: quench

        // Rate-matched sizing: observed closure (1-week EMA), floored at the
        // "half the gap in one window" prior (which alone applies while level).
        int256 c0 = (LN2 * absD) / ONE / (2 * int256(uint256(p.windowEpochs)));
        int256 c = c0;
        if (u > eps) {                                   // gaining: complete it
            int256 observed = delta > 0 ? -int256(sig.ddotEma) : int256(sig.ddotEma);
            if (observed > c) c = observed;
        }

        // effort = rho * c * weight(s/S), in bp of NAV per epoch, capped.
        int256 w = int256(uint256(sig.s)) * ONE / int256(runningS);
        int256 bp = (int256(uint256(p.rho1e9)) * c / 1e9) * w / ONE / 1e14;
        int256 cap = int256(uint256(p.capBpPerEpoch));
        if (bp > cap) bp = cap;
        if (bp == 0) return 0;
        return delta > 0 ? -int32(int256(bp)) : int32(int256(bp));
    }

    // --- Observation helpers -------------------------------------------------- //

    /// @dev Price-scaled target share s_i = basketAmount * P0^2 / spot -- the
    ///      same fixed-quantity-index target `_allocateSellHigh` uses.
    function _targetShare(Meta storage mt) internal view returns (uint256) {
        (, int24 tick,,,,,) = IUniswapV3Pool(mt.pool).slot0();
        uint256 spot = UniswapV3OracleLib.getQuoteAtTick(
            tick, uint128(10 ** mt.decimals), mt.token, buck);
        if (spot == 0) return 0;
        uint256 base = UniswapV3OracleLib.mulDiv(
            mt.basketAmount, mt.initialPriceInBuck, 1e18);
        return UniswapV3OracleLib.mulDiv(base, mt.initialPriceInBuck, spot);
    }

    /// @dev Relative deviation of this pool's cached share vs its cached
    ///      target share, over the (slightly stale) running sums.
    function _deviation(uint256 bv, uint256 s) internal view returns (int256) {
        if (bv == 0 || s == 0 || runningB == 0 || runningS == 0) return 0;
        // (bv/B) / (s/S) - 1  ==  bv*S / (B*s) - 1
        uint256 num = UniswapV3OracleLib.mulDiv(bv, runningS, runningB);
        uint256 ratio = UniswapV3OracleLib.mulDiv(num, 1e18, s);
        return int256(ratio) - ONE;
    }

    /// @dev (1e18 fixed) base^n by binary exponentiation; O(log n).
    function _pow1e18(int256 base, uint32 n) internal pure returns (int256 r) {
        r = ONE;
        int256 b = base;
        if (b < 0) b = 0;                // window 2 edge: 1-beta could hit 0
        while (n > 0) {
            if (n & 1 == 1) r = (r * b) / ONE;
            b = (b * b) / ONE;
            n >>= 1;
        }
    }

    // --- Advisory reads -------------------------------------------------------- //

    /// @notice Signed per-epoch effort (bp of NAV) for constituent `i`.
    function effortOf(uint256 i) external view returns (int256) {
        return signalOf[i].effortBp;
    }

    /// @notice All efforts (for redemption draw weighting).
    function effortsAll() external view returns (int256[] memory out) {
        out = new int256[](constituentCount);
        for (uint256 i = 0; i < constituentCount; i++) {
            out[i] = signalOf[i].effortBp;
        }
    }

    /// @notice Pool that incoming BUCK should be invested into: the strongest
    ///         positive (buy-side) effort.  `type(uint256).max` = no signal --
    ///         fall back to the venue's default most-underweight routing.
    function depositHint() external view returns (uint256 best) {
        best = type(uint256).max;
        int32 bestE = 0;
        for (uint256 i = 0; i < constituentCount; i++) {
            int32 eBp = signalOf[i].effortBp;
            if (eBp > bestE) { bestE = eBp; best = i; }
        }
    }

    /// @notice Pool a redemption is best drawn from: the strongest negative
    ///         (sell-side) effort.  `type(uint256).max` = no signal.
    function redeemHint() external view returns (uint256 best) {
        best = type(uint256).max;
        int32 bestE = 0;
        for (uint256 i = 0; i < constituentCount; i++) {
            int32 eBp = signalOf[i].effortBp;
            if (eBp < bestE) { bestE = eBp; best = i; }
        }
    }

    /// @notice Current raw deviation of constituent `i` from its cached
    ///         observations (diagnostic; the sim/TUI reads this).
    function deviationOf(uint256 i) external view returns (int256) {
        Signal storage sig = signalOf[i];
        return _deviation(sig.bv, sig.s);
    }
}
