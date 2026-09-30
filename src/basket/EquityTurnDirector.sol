// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IEquityDirector} from "./BuckBasketEquityStorage.sol";

interface IEquityBasketView {
    function constituentsLength() external view returns (uint256);
    function constituents(uint256 i) external view returns (
        address token, uint8 decimals, uint256 basketAmount, uint256 initialPriceInBuck,
        uint24 feeTier, address pool, int24 tickLower, int24 tickUpper, bool buckIsToken0,
        uint256 targetWeightBp, uint128 treasuryLiquidity);
    function weightsBp() external view returns (uint256[] memory);
    function twapWindow() external view returns (uint32);
}

interface IV3Observe {
    function observe(uint32[] calldata secondsAgos)
        external view returns (int56[] memory tickCumulatives, uint160[] memory);
    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, uint8, bool);
}

/// @title EquityTurnDirector -- the equity basket's director: the multi-scale
///        turn detector, per leg, and the lean (alberta-buck-ethereum-wheel.org, "The
///        director"; alberta-buck-ethereum.org, "The Director").
///
/// @notice Once a day (`observe`, the basket's Daily step, or anyone) it reads
///         each constituent's TWAP tick -- a tick is a log price, so no ln() --
///         oriented as BUCK per TOKEN, takes it against the basket's mean (the
///         common numeraire cancels), and updates a ladder of EMAs of that
///         deviation: six windows (5-160 days) and the ANCHOR (1280 days).
///
///         A leg's EXCURSION is its deviation against its anchor: RICH above,
///         CHEAP below.  Each of the six shorter EMAs votes when it is moving
///         back toward the anchor; `quorum` votes is a TURN.  A leg not turned
///         is RUNNING.
///
///           targetsBp()  the declared weights x exp(-tilt x excursion),
///                        renormalized: the lean against the excursion
///           mayTrim(i)   not a leg still running up -- or past the leash
///                        (its weight over its DECLARED target by leashBp of it)
///           mayFund(i)   not a leg still running down -- or past the leash
///
///         The lean pays where prices revert, the gate where excursions have
///         momentum.  The leash keeps the mandate; the lean only times it.
contract EquityTurnDirector is IEquityDirector {
    uint256 internal constant W = 7;                      // the ladder's windows
    int256  internal constant TICK_LN_WAD = 99995000333297;   // ln(1.0001), 1e18
    int256  internal constant ONE = 1e18;

    IEquityBasketView public immutable basket;
    address public governance;

    uint16[7] public windows = [uint16(5), 10, 20, 40, 80, 160, 1280];   // days; the last is the anchor
    uint8   public quorum = 4;
    int256  public tiltWad = 1e18;
    uint16  public leashBp = 3000;
    uint64  public lastDay;
    bool    public primed;

    mapping(uint256 => int256[7]) internal _ema;           // per leg, ticks x 1e18
    mapping(uint256 => int256[7]) internal _vel;
    mapping(uint256 => int256) public deviation;           // the leg against the mean, ticks x 1e18

    error NotGovernance();

    constructor(address basket_, address governance_) {
        basket = IEquityBasketView(basket_);
        governance = governance_;
    }

    function setParams(uint8 quorum_, int256 tiltWad_, uint16 leashBp_) external {
        if (msg.sender != governance) revert NotGovernance();
        quorum = quorum_;
        tiltWad = tiltWad_;
        leashBp = leashBp_;
    }

    // --- the daily sample ------------------------------------------------------- //

    /// @notice The daily sample.  A sample after a gap of `n` days advances
    ///         each EMA as if today's reading had held through all of them
    ///         (sample-and-hold), in closed form: a quiet week costs one
    ///         sample and lands where seven daily samples would have.
    ///
    ///           e' = x + (e - x) q^n        v' = a q^(n-1) (x - e)
    ///
    ///         with a = 2/(w+1) and q = 1 - a; v' is the last day's step, whose
    ///         sign the turn votes read.  One day apart it is the plain step.
    function observe() external {
        uint64 today = uint64(block.timestamp / 1 days);
        if (primed && today <= lastDay) return;
        uint256 gap = primed ? uint256(today - lastDay) : 1;
        if (gap > 3650) gap = 3650;
        lastDay = today;
        uint256 n = basket.constituentsLength();
        if (n == 0) return;
        int256[] memory lp = new int256[](n);
        int256 mean = 0;
        uint32 window = basket.twapWindow();
        for (uint256 i = 0; i < n; i++) {
            (,,,,, address pool,,, bool b0,,) = basket.constituents(i);
            int256 t = int256(_tick(pool, window));
            lp[i] = b0 ? -t : t;                          // BUCK per TOKEN
            mean += lp[i];
        }
        mean /= int256(n);
        for (uint256 i = 0; i < n; i++) {
            int256 x = (lp[i] - mean) * 1e18;
            deviation[i] = x;
            int256[7] storage e = _ema[i];
            int256[7] storage v = _vel[i];
            for (uint256 k = 0; k < W; k++) {
                if (!primed) { e[k] = x; v[k] = 0; continue; }
                int256 w1 = int256(uint256(windows[k])) + 1;
                if (gap == 1) {
                    int256 step = (x - e[k]) * 2 / w1;
                    e[k] += step;
                    v[k] = step;
                } else {
                    int256 q = ONE - 2 * ONE / w1;
                    int256 qn1 = _powWad(q, gap - 1);
                    int256 d0 = x - e[k];
                    e[k] = x - d0 * (qn1 * q / ONE) / ONE;
                    v[k] = d0 * 2 / w1 * qn1 / ONE;
                }
            }
        }
        primed = true;
    }

    /// @dev b^n in 1e18 fixed point, 0 <= b < 1e18, by squaring: O(log n).
    function _powWad(int256 b, uint256 n) internal pure returns (int256 r) {
        r = ONE;
        while (n > 0) {
            if (n & 1 == 1) r = r * b / ONE;
            b = b * b / ONE;
            n >>= 1;
        }
    }

    function _tick(address pool, uint32 window) internal view returns (int24) {
        uint32[] memory ago = new uint32[](2);
        ago[0] = window;
        try IV3Observe(pool).observe(ago) returns (int56[] memory c, uint160[] memory) {
            int56 d = c[1] - c[0];
            int24 t = int24(d / int56(uint56(window)));
            if (d < 0 && (d % int56(uint56(window)) != 0)) t--;
            return t;
        } catch {
            (, int24 t,,,,,) = IV3Observe(pool).slot0();
            return t;
        }
    }

    // --- the reading ---------------------------------------------------------- //

    /// @notice Leg i's ladder: each window's EMA and its last day's step, in
    ///         ticks x 1e18 (the anchor last).
    function ladder(uint256 i) external view returns (int256[7] memory ema, int256[7] memory vel) {
        return (_ema[i], _vel[i]);
    }

    /// @notice The leg against its anchor, in ticks x 1e18 (0 before a sample).
    function excursion(uint256 i) public view returns (int256) {
        if (!primed) return 0;
        return deviation[i] - _ema[i][W - 1];
    }

    function turned(uint256 i) public view returns (bool) {
        int256 e = excursion(i);
        if (e == 0) return false;
        uint256 votes = 0;
        int256[7] storage v = _vel[i];
        for (uint256 k = 0; k + 1 < W; k++) {
            if ((v[k] < 0 && e > 0) || (v[k] > 0 && e < 0)) votes++;
        }
        return votes >= quorum;
    }

    function running(uint256 i, int256 side) public view returns (bool) {
        int256 e = excursion(i);
        return (side > 0 ? e > 0 : e < 0) && !turned(i);
    }

    function targetsBp() external view returns (uint256[] memory tg) {
        uint256 n = basket.constituentsLength();
        tg = new uint256[](n);
        uint256[] memory raw = new uint256[](n);
        uint256 z = 0;
        for (uint256 i = 0; i < n; i++) {
            (,,,,,,,,, uint256 w,) = basket.constituents(i);
            int256 y = -(excursion(i) * TICK_LN_WAD / 1e18) * tiltWad / 1e18;   // ln units
            raw[i] = w * _expWad(y) / 1e18;
            z += raw[i];
        }
        for (uint256 i = 0; i < n; i++) tg[i] = z == 0 ? 0 : raw[i] * 10000 / z;
    }

    function mayTrim(uint256 i) external view returns (bool) {
        return !running(i, 1) || _leashed(i, true);
    }

    function mayFund(uint256 i) external view returns (bool) {
        return !running(i, -1) || _leashed(i, false);
    }

    function _leashed(uint256 i, bool over) internal view returns (bool) {
        (,,,,,,,,, uint256 declared,) = basket.constituents(i);
        uint256 w = basket.weightsBp()[i];
        uint256 lim = declared * leashBp / 10000;
        return over ? w > declared + lim : w + lim < declared;
    }

    /// @notice exp(y) for y in 1e18, clamped to [-4, 4]: exp(y/16)^16, the
    ///         inner by its Taylor series.
    function _expWad(int256 y) internal pure returns (uint256) {
        if (y > 4e18) y = 4e18;
        if (y < -4e18) y = -4e18;
        int256 x = y / 16;
        int256 term = 1e18;
        int256 sum = 1e18;
        for (int256 j = 1; j <= 6; j++) {
            term = term * x / (j * 1e18);
            sum += term;
        }
        uint256 r = uint256(sum);
        for (uint256 j = 0; j < 4; j++) r = r * r / 1e18;
        return r;
    }
}
