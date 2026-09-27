// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}          from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20}       from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math}            from "@openzeppelin/contracts/utils/math/Math.sol";
import {IUniswapV3Pool}  from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

import {WorkWheel} from "./WorkWheel.sol";

/// @title CycleMath -- the closed form a cycle of constant-product legs is sized by.
///
/// @notice A swap of x on reserves (R_in, R_out) at fee f is
///         out(x) = g R_out x / (R_in + g x), g = 1 - f: a Mobius map.  Maps of
///         that form compose to the same form, so a cycle is described by two
///         numbers: its slope at zero rho = prod(g_k R_out_k / R_in_k) and its
///         curvature kappa = sum_k (prod_{j<k} rho_j) g_k / R_in_k.  The cycle
///         pays iff rho > 1; its profit rho x / (1 + kappa x) - x peaks at
///         x* = (sqrt(rho) - 1) / kappa with profit (sqrt(rho) - 1)^2 / kappa.
///         Reserves are a V3 pool's VIRTUAL reserves at slot0 (L / sqrtP and
///         L sqrtP), valid inside the active range -- so sizes are capped well
///         inside it, and the realized-profit check is what makes a cycle safe.
library CycleMath {
    uint256 internal constant Q96 = 2 ** 96;

    struct Leg {
        address pool;
        address tokenIn;
    }

    function reserves(address pool, address tokenIn)
        internal view returns (uint256 rIn, uint256 rOut, uint24 fee)
    {
        (uint160 sqrtP,,,,,,) = IUniswapV3Pool(pool).slot0();
        uint128 L = IUniswapV3Pool(pool).liquidity();
        fee = IUniswapV3Pool(pool).fee();
        if (sqrtP == 0 || L == 0) return (0, 0, fee);
        uint256 r0 = Math.mulDiv(uint256(L), Q96, uint256(sqrtP));
        uint256 r1 = Math.mulDiv(uint256(L), uint256(sqrtP), Q96);
        (rIn, rOut) = IUniswapV3Pool(pool).token0() == tokenIn ? (r0, r1) : (r1, r0);
    }

    /// @return rho   the slope at zero, 1e18
    /// @return kappa the curvature, 1e36 per unit of the first leg's input
    /// @return rIn0  the first leg's input reserve (for the size cap)
    function compose(Leg[3] memory legs)
        internal view returns (uint256 rho, uint256 kappa, uint256 rIn0)
    {
        rho = 1e18;
        for (uint256 k = 0; k < 3; k++) {
            (uint256 rIn, uint256 rOut, uint24 fee) = reserves(legs[k].pool, legs[k].tokenIn);
            if (rIn == 0 || rOut == 0) return (0, 0, 0);
            if (k == 0) rIn0 = rIn;
            uint256 g = 1e6 - uint256(fee);
            kappa += Math.mulDiv(Math.mulDiv(g, 1e30, rIn), rho, 1e18);
            rho = Math.mulDiv(Math.mulDiv(rho, rOut, rIn), g, 1e6);
        }
    }

    function optimum(uint256 rho, uint256 kappa)
        internal pure returns (uint256 x, uint256 profit)
    {
        if (rho <= 1e18 || kappa == 0) return (0, 0);
        uint256 s = Math.sqrt(rho * 1e18);          // sqrt(rho), 1e18
        if (s <= 1e18) return (0, 0);
        x = Math.mulDiv(s - 1e18, 1e18, kappa);
        profit = Math.mulDiv(s - 1e18, s - 1e18, kappa);
    }
}

interface IWheelCreditee {
    function creditDepositors(uint256 i, uint256 tokenAmount) external returns (uint128, uint256);
    function creditTreasury(uint256 buckAmount) external;
}

/// @title ArbKind -- the consistency arbitrage, one slot per triangle.
///
/// @notice doc/BASKET-WHEEL.org 3-5.  A triangle through constituent TOKEN:
///         the basket's own TOKEN/BUCK pool, TOKEN/USDC and BUCK/USDC.  A run
///         goes round it from TOKEN to TOKEN in whichever direction pays,
///         with no inventory: the first swap pays out before it is paid, the
///         other two run inside its callback, and the TOKEN they return pays
///         the first.  USDC exists only between two swaps; the supply does
///         not change.  The realized profit (TOKEN) pays the caller `shareBp`
///         and the rest is credited to the basket's DEPOSITORS
///         (=creditDepositors=: re-LP'd as depositor liquidity, the stress
///         fee's mechanics), so the harvest reaches the receipts.
///
///         START MODES.  The same three swaps in the same rotation earn the
///         same, whichever vertex they start from; the start decides where
///         the profit lands.  TOKEN (startMode 0): in the constituent, credited
///         to the depositors.  BUCK (startMode 1): in BUCK, the reserve's own
///         asset -- it funds the callers' gas offset up to the reserve's cap
///         and the rest goes to the basket's treasury, so the wheel pays for
///         its own calls without touching the basket's yield.  No flash mint
///         is needed for either: V3 pays a swap's output before it collects
///         the input, so the first leg funds the cycle.  (The basket's mint
///         authority is what the NAV leg -- issuing at a premium, absorbing at
///         a discount -- uses; that leg is the undertakings', not the wheel's.)
///
///         v1 takes cycles with a positive DIRECT profit only (gaps beyond all
///         three pool fees).  The band only the basket can take -- a direct
///         loss on the cycle recouped by the fee its own pool earns -- needs a
///         small TOKEN float or the wheel inside the shell (BASKET-WHEEL 8.4);
///         it is v2.
abstract contract ArbKind is WorkWheel {
    using SafeERC20 for IERC20;

    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342;

    struct Triangle {
        address token;          // the constituent
        address poolOwn;        // TOKEN/BUCK, the basket's
        address poolUsdc;       // TOKEN/USDC
        uint256 basketIndex;    // the constituent's index in the basket
    }

    address public immutable arbBuck;
    address public immutable arbUsdc;
    address public poolUb;                  // BUCK/USDC
    address public creditee;                // the basket (0: keep the profit here)
    uint16  public shareBp = 1000;          // the caller's cut of a cycle's profit
    uint16  public capBp = 100;             // max input, bp of the first leg's input reserve
    uint16  public minEdgeBp = 1;           // min profit, bp of the input
    uint8   public startMode;               // 0: TOKEN -> TOKEN, 1: BUCK -> BUCK
    Triangle[] public triangles;

    address private transient _cbPool;      // the only pool whose callback is accepted

    event Cycled(uint256 indexed k, uint8 dir, uint256 amountIn, uint256 profit,
                 uint256 callerShare, uint256 credited);

    error NotSelf();
    error BadCallback();
    error NoEdge();

    constructor(address buck_, address usdc_) {
        arbBuck = buck_;
        arbUsdc = usdc_;
    }

    function setArb(address poolUb_, address creditee_, uint16 shareBp_, uint16 capBp_,
                    uint16 minEdgeBp_) external onlyGov {
        poolUb = poolUb_;
        creditee = creditee_;
        shareBp = shareBp_;
        capBp = capBp_;
        minEdgeBp = minEdgeBp_;
    }

    function setStartMode(uint8 m) external onlyGov { startMode = m; }

    function setTriangle(uint256 k, Triangle calldata t) external onlyGov {
        if (k == triangles.length) triangles.push(t);
        else triangles[k] = t;
    }

    function triangleCount() external view returns (uint256) { return triangles.length; }

    // --- the seam --------------------------------------------------------------- //

    function _slotCount() internal view virtual override returns (uint256) {
        return super._slotCount() + (poolUb == address(0) ? 0 : triangles.length);
    }

    function _due(uint256 s) internal view virtual override returns (bool) {
        uint256 b = super._slotCount();
        if (s < b) return super._due(s);
        (uint256 x,,) = plan(s - b);
        return x > 0;
    }

    function _run(uint256 s) internal virtual override returns (uint256) {
        uint256 b = super._slotCount();
        if (s < b) return super._run(s);
        uint256 k = s - b;
        (uint256 x, uint8 dir,) = plan(k);
        if (x == 0) return 0;
        try this.arbCycle(k, dir, x) returns (uint256 profit) {
            _distribute(k, dir, x, profit);
            return 1;
        } catch {
            return 0;
        }
    }

    // --- planning ------------------------------------------------------------------ //

    /// @dev The two rotations of the triangle, from the start token:
    ///      TOKEN start: 0 = TOKEN -> BUCK (own) -> USDC -> TOKEN,
    ///                   1 = TOKEN -> USDC -> BUCK -> TOKEN (own);
    ///      BUCK start:  0 = BUCK -> TOKEN (own) -> USDC -> BUCK,
    ///                   1 = BUCK -> USDC -> TOKEN -> BUCK (own).
    function _legs(Triangle memory t, uint8 dir) internal view returns (CycleMath.Leg[3] memory l) {
        if (startMode == 0) {
            if (dir == 0) {
                l[0] = CycleMath.Leg(t.poolOwn, t.token);
                l[1] = CycleMath.Leg(poolUb, arbBuck);
                l[2] = CycleMath.Leg(t.poolUsdc, arbUsdc);
            } else {
                l[0] = CycleMath.Leg(t.poolUsdc, t.token);
                l[1] = CycleMath.Leg(poolUb, arbUsdc);
                l[2] = CycleMath.Leg(t.poolOwn, arbBuck);
            }
        } else {
            if (dir == 0) {
                l[0] = CycleMath.Leg(t.poolOwn, arbBuck);
                l[1] = CycleMath.Leg(t.poolUsdc, t.token);
                l[2] = CycleMath.Leg(poolUb, arbUsdc);
            } else {
                l[0] = CycleMath.Leg(poolUb, arbBuck);
                l[1] = CycleMath.Leg(t.poolUsdc, arbUsdc);
                l[2] = CycleMath.Leg(t.poolOwn, t.token);
            }
        }
    }

    function _startToken(Triangle memory t) internal view returns (address) {
        return startMode == 0 ? t.token : arbBuck;
    }

    /// @notice The best cycle on triangle k now: (input TOKEN, direction,
    ///         expected profit TOKEN); x = 0 when none clears minEdgeBp.
    function plan(uint256 k) public view returns (uint256 x, uint8 dir, uint256 profit) {
        Triangle memory t = triangles[k];
        for (uint8 d = 0; d < 2; d++) {
            (uint256 rho, uint256 kappa, uint256 rIn0) = CycleMath.compose(_legs(t, d));
            (uint256 xd, uint256 pd) = CycleMath.optimum(rho, kappa);
            if (xd == 0) continue;
            uint256 cap = rIn0 * capBp / 10000;
            if (xd > cap) {
                xd = cap;
                // profit at the capped size, on the composed map
                uint256 out = Math.mulDiv(Math.mulDiv(rho, xd, 1e18), 1e36,
                                          1e36 + Math.mulDiv(kappa, xd, 1e18));
                pd = out > xd ? out - xd : 0;
            }
            if (pd * 10000 < xd * minEdgeBp || pd <= profit) continue;
            (x, dir, profit) = (xd, d, pd);
        }
    }

    // --- execution: a cycle with no inventory ---------------------------------------- //

    /// @notice Go round triangle k from TOKEN to TOKEN.  Self-call only, so a
    ///         cycle that misses its edge reverts alone and the tick goes on.
    function arbCycle(uint256 k, uint8 dir, uint256 x) external returns (uint256 profit) {
        if (msg.sender != address(this)) revert NotSelf();
        Triangle memory t = triangles[k];
        CycleMath.Leg[3] memory l = _legs(t, dir);
        address start = _startToken(t);
        uint256 before = IERC20(start).balanceOf(address(this));
        _swap(l[0], x, abi.encode(uint8(1), l[1], l[2]));
        uint256 afterBal = IERC20(start).balanceOf(address(this));
        if (afterBal <= before) revert NoEdge();
        profit = afterBal - before;
        if (profit * 10000 < x * minEdgeBp) revert NoEdge();
    }

    function _swap(CycleMath.Leg memory leg, uint256 amountIn, bytes memory data)
        internal returns (uint256 out)
    {
        bool zeroForOne = IUniswapV3Pool(leg.pool).token0() == leg.tokenIn;
        _cbPool = leg.pool;
        (int256 a0, int256 a1) = IUniswapV3Pool(leg.pool).swap(
            address(this), zeroForOne, int256(amountIn),
            zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1, data);
        out = uint256(-(zeroForOne ? a1 : a0));
    }

    /// @notice Leg callbacks.  The first leg's (tag 1) runs legs two and three
    ///         on what it paid out, then pays its own input from the TOKEN
    ///         they return; the others (tag 0) pay their input.
    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata data) external {
        if (msg.sender != _cbPool || _cbPool == address(0)) revert BadCallback();
        address pool = msg.sender;
        uint8 tag = abi.decode(data[:32], (uint8));
        if (tag == 1) {
            (, CycleMath.Leg memory l1, CycleMath.Leg memory l2) =
                abi.decode(data, (uint8, CycleMath.Leg, CycleMath.Leg));
            uint256 got = uint256(-(a0 < 0 ? a0 : a1));
            uint256 got2 = _swap(l1, got, abi.encode(uint8(0)));
            _swap(l2, got2, abi.encode(uint8(0)));
        }
        if (a0 > 0) IERC20(IUniswapV3Pool(pool).token0()).safeTransfer(pool, uint256(a0));
        if (a1 > 0) IERC20(IUniswapV3Pool(pool).token1()).safeTransfer(pool, uint256(a1));
    }

    function _distribute(uint256 k, uint8 dir, uint256 x, uint256 profit) internal {
        Triangle memory t = triangles[k];
        IERC20 tok = IERC20(_startToken(t));
        uint256 share = profit * shareBp / 10000;
        if (share > 0) tok.safeTransfer(msg.sender, share);
        uint256 rest = profit - share;
        uint256 credited = 0;
        if (startMode == 1 && rest > 0) {
            // BUCK: fund the callers' reserve first, the treasury with the rest
            uint256 room = reserveCap > reserve ? reserveCap - reserve : 0;
            uint256 toReserve = rest < room ? rest : room;
            reserve += toReserve;
            rest -= toReserve;
            credited = toReserve;
        }
        if (rest > 0 && creditee != address(0)) {
            tok.forceApprove(creditee, rest);
            if (startMode == 0) {
                try IWheelCreditee(creditee).creditDepositors(t.basketIndex, rest) {
                    credited += rest;
                } catch { tok.forceApprove(creditee, 0); }
            } else {
                try IWheelCreditee(creditee).creditTreasury(rest) {
                    credited += rest;
                } catch { tok.forceApprove(creditee, 0); }
            }
        }
        emit Cycled(k, dir, x, profit, share, credited);
    }
}
