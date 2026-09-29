// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";
import {BuckBasketEquityStorage, IEquityDirector} from "./BuckBasketEquityStorage.sol";

/// @title BuckBasketEquityWheel -- the equity basket's components, a facet.
///
/// @notice doc/BASKET-EQUITY.org 13.6.  Reached by the shell's fallback
///         (delegatecall over the shared storage) for `wheelDue` and
///         `wheelStep`; only the basket's work wheel may step it.  Each kind is
///         a state-machine step that does at most one bounded thing:
///
///           0 Daily    fold the day's net flow into the liquidity sizing;
///                      the director's sample
///           1 Sync(i)  collect pool i's fees into the wallet (once a day)
///           2 Deploy(i) pair the wallet's TOKEN_i with the BUCK held beyond
///                      what liquidity needs, selling part of the TOKEN if
///                      short; add liquidity
///           3 Fund     buy the director's pick (or, when the wallet's spare
///                      BUCK passes the parking room, the neediest pool's)
///                      TOKEN with half of what it places; Deploy pairs it
///           4 Trim     repay what exits owe, refill liquidity below its
///                      floor, or trim the pool the director names: unwind at
///                      most `stepBp` of a position, sell its TOKEN
///
///         The arbitrage stays in the wheel (ArbKind): it needs no float and
///         credits its captures to the wallet (creditDepositors /
///         creditTreasury).
contract BuckBasketEquityWheel is BuckBasketEquityStorage {

    uint8 internal constant DAILY  = 0;
    uint8 internal constant SYNC   = 1;
    uint8 internal constant DEPLOY = 2;
    uint8 internal constant FUND   = 3;
    uint8 internal constant TRIM   = 4;

    function _v() internal view returns (IBuckBasketVenue) {
        return IBuckBasketVenue(address(this));
    }

    function wheelDue(uint8 kind, uint256 i) external view returns (bool) {
        if (kind == DAILY)  return block.timestamp / 1 days > lastDay;
        if (kind == SYNC)   return liquidityOf[i] > 0 && block.timestamp / 1 days > lastSyncDay[i];
        if (kind > TRIM)    return false;
        Snap memory s = _snap();
        if (kind == DEPLOY) return _tokenValue(s, i) > _grainS(s);
        if (kind == FUND)   { (uint256 j,) = _fundPlan(s); return j != NONE; }
        (uint256 t,) = _trimPlan(s);
        return t != NONE;
    }

    function wheelStep(uint8 kind, uint256 i) external returns (uint256 work) {
        if (msg.sender != wheel || wheel == address(0)) revert NotWheel();
        if (kind == DAILY)  return _daily();
        if (kind == SYNC)   return _sync(i);
        if (kind > TRIM)    return 0;
        Snap memory s = _snap();
        if (kind == DEPLOY) return _deploy(s, i);
        if (kind == FUND)   return _fund(s);
        return _trim(s);
    }

    uint256 internal constant NONE = type(uint256).max;

    // --- Daily ------------------------------------------------------------------- //

    function _daily() internal returns (uint256) {
        uint64 today = uint64(block.timestamp / 1 days);
        if (today <= lastDay) return 0;
        uint256 f = uint256(dayFlow >= 0 ? dayFlow : -dayFlow);
        uint256 sq = f * f / 1e18;                       // BUCK^2 at 1e18 scale
        uint256 span = uint256(eq.flowDays) + 1;
        uint256 ms = flowMs;
        flowMs = sq >= ms ? ms + (sq - ms) * 2 / span : ms - (ms - sq) * 2 / span;
        dayFlow = 0;
        lastDay = today;
        address d = equityDirector;
        if (d != address(0)) { try IEquityDirector(d).observe() {} catch {} }
        emit WheelWork(DAILY, 0, f);
        return 1;
    }

    // --- Sync ---------------------------------------------------------------------- //

    function _sync(uint256 i) internal returns (uint256) {
        lastSyncDay[i] = uint64(block.timestamp / 1 days);
        (uint256 t, uint256 b) = _v().positionSync(i);
        idleToken[i] += t;
        idleBuck += b;
        _settle();
        emit WheelWork(SYNC, i, b);
        return 1;
    }

    // --- Deploy ---------------------------------------------------------------------- //

    function _tokenValue(Snap memory s, uint256 i) internal view returns (uint256) {
        uint256 tok = idleToken[i];
        if (tok == 0) return 0;
        return tok * s.m[i].pTwap / (10 ** constituents[i].decimals);
    }

    function _usable(Snap memory s) internal view returns (uint256) {
        uint256 k = _keepS(s);
        return idleBuck > k ? idleBuck - k : 0;
    }

    function _deploy(Snap memory s, uint256 i) internal returns (uint256) {
        uint256 one = 10 ** constituents[i].decimals;
        IBuckBasketVenue.Marks memory m = s.m[i];
        uint256 p = m.pTwap;
        if (p == 0) return 0;
        uint256 v = idleToken[i] * p / one;
        uint256 have = _usable(s);
        if (have > v) have = v;
        if (have < v && m.depth > 0) {                   // balance the pair: sell TOKEN
            uint256 sell = (v - have) / 2;
            uint256 cap = _swapCap(m);
            if (sell > cap) sell = cap;
            uint256 dx = sell * one / p;
            if (dx > 0) {
                (uint256 spent, uint256 got) = _v().monetaryLeg(i, false, dx);
                idleToken[i] -= spent;
                idleBuck += got;
                have += got;
            }
        }
        (uint128 l, uint256 tokUsed, uint256 buckUsed) =
            _v().positionMint(i, idleToken[i], have);
        if (l == 0) return 0;
        liquidityOf[i] += l;
        idleToken[i] -= tokUsed;
        idleBuck -= buckUsed;
        emit WheelWork(DEPLOY, i, buckUsed);
        return 1;
    }

    // --- Fund ------------------------------------------------------------------------ //

    function _targetsBp() internal view returns (uint256[] memory tg) {
        uint256 n = constituents.length;
        address d = equityDirector;
        if (d != address(0)) {
            try IEquityDirector(d).targetsBp() returns (uint256[] memory t) {
                if (t.length == n) return t;
            } catch {}
        }
        tg = new uint256[](n);
        for (uint256 i = 0; i < n; i++) tg[i] = constituents[i].targetWeightBp;
    }

    function _positions(Snap memory s) internal pure returns (uint256[] memory pos, uint256 tot) {
        pos = new uint256[](s.m.length);
        for (uint256 i = 0; i < s.m.length; i++) {
            pos[i] = s.m[i].posTwap;
            tot += pos[i];
        }
    }

    function _mayFund(uint256 i) internal view returns (bool) {
        address d = equityDirector;
        if (d == address(0)) return false;
        try IEquityDirector(d).mayFund(i) returns (bool ok) { return ok; } catch { return false; }
    }

    function _mayTrim(uint256 i) internal view returns (bool) {
        address d = equityDirector;
        if (d == address(0)) return false;
        try IEquityDirector(d).mayTrim(i) returns (bool ok) { return ok; } catch { return false; }
    }

    function _anyTokenWaiting(Snap memory s, uint256 g) internal view returns (bool) {
        for (uint256 i = 0; i < s.m.length; i++) {
            if (_tokenValue(s, i) > g) return true;
        }
        return false;
    }

    /// @notice (pool, BUCK) Fund would place now.  The spare is the BUCK held
    ///         beyond what liquidity needs.  It goes to the director's pick
    ///         (the largest gap beyond the band it may fund), or -- once the
    ///         spare passes the parking room (with no director, the band) --
    ///         to the largest gap.
    function _fundPlan(Snap memory s) internal view returns (uint256 j, uint256 amount) {
        j = NONE;
        uint256 g = _grainS(s);
        if (_anyTokenWaiting(s, g)) return (NONE, 0);
        uint256 spare = _usable(s);
        if (spare <= g) return (NONE, 0);
        (uint256[] memory pos, uint256 tot) = _positions(s);
        uint256[] memory tg = _targetsBp();
        uint256 total = tot + spare;
        uint256 band = total * eq.weightBandBp / 10000;
        uint256 best;
        uint256 bestAny;
        uint256 jAny = NONE;
        for (uint256 i = 0; i < pos.length; i++) {
            uint256 want = total * tg[i] / 10000;
            if (want <= pos[i] || s.m[i].depth == 0) continue;
            uint256 gap = want - pos[i];
            if (gap > bestAny) { bestAny = gap; jAny = i; }
            if (gap > band && gap > best && _mayFund(i)) { best = gap; j = i; }
        }
        if (j == NONE) {
            uint256 room = equityDirector == address(0) ? eq.bandBp : eq.parkBp;
            if (spare * 10000 <= _targetS(s) * room + g * 10000) return (NONE, 0);
            if (jAny == NONE) return (NONE, 0);
            (j, best) = (jAny, bestAny);
        }
        amount = spare < best ? spare : best;
        if (amount <= g) return (NONE, 0);
    }

    function _fund(Snap memory s) internal returns (uint256) {
        (uint256 j, uint256 amount) = _fundPlan(s);
        if (j == NONE) return 0;
        uint256 half = amount / 2;
        uint256 cap = _swapCap(s.m[j]);
        if (half > cap) half = cap;
        if (half == 0) return 0;
        (uint256 spent, uint256 got) = _v().monetaryLeg(j, true, half);
        idleBuck -= spent;
        idleToken[j] += got;
        emit WheelWork(FUND, j, spent);
        return 1;
    }

    // --- Trim --------------------------------------------------------------------------- //

    /// @notice (pool, BUCK value) Trim would unwind now: only a settled basket
    ///         trims (no TOKEN waiting, nothing to fund).  It repays what exits
    ///         owe, refills liquidity below its floor, or trims the pool the
    ///         director names (with none: the pool furthest over its target
    ///         beyond the band).
    function _trimPlan(Snap memory s) internal view returns (uint256 t, uint256 value) {
        t = NONE;
        uint256 g = _grainS(s);
        if (_anyTokenWaiting(s, g)) return (NONE, 0);
        (uint256 jf,) = _fundPlan(s);
        if (jf != NONE) return (NONE, 0);
        uint256 short = owed > g ? owed : 0;
        uint256 target = _targetS(s);
        uint256 liq = _liquidityS(s);
        if (liq + g < target * (10000 - eq.bandBp) / 10000 && target - liq > short) {
            short = target - liq;
        }
        (uint256[] memory pos, uint256 tot) = _positions(s);
        if (tot == 0) return (NONE, 0);
        uint256[] memory tg = _targetsBp();
        uint256 band = tot * eq.weightBandBp / 10000;
        bool directed = equityDirector != address(0);
        uint256 need = 0;
        for (uint256 i = 0; i < pos.length; i++) {
            uint256 want = tot * tg[i] / 10000;
            if (pos[i] <= want || s.m[i].depth == 0) continue;
            uint256 x = pos[i] - want;
            if (x > band && x > need && (directed ? _mayTrim(i) : true)) { need = x; t = i; }
        }
        if (t == NONE) {
            if (short == 0) return (NONE, 0);
            uint256 over = 0;
            for (uint256 i = 0; i < pos.length; i++) {       // the most overweight
                if (pos[i] == 0) continue;
                uint256 want = tot * tg[i] / 10000;
                uint256 x = pos[i] > want ? pos[i] - want : 0;
                if (t == NONE || x > over) { over = x; t = i; }
            }
            if (t == NONE) return (NONE, 0);
            need = short;
        } else if (short > need) {
            need = short;
        }
        uint256 step = pos[t] * eq.stepBp / 10000;
        uint256 cap = 2 * _swapCap(s.m[t]);               // its TOKEN half is sold
        if (cap < step) step = cap;
        value = need < step ? need : step;
        if (value <= g) return (NONE, 0);
    }

    function _trim(Snap memory s) internal returns (uint256) {
        (uint256 t, uint256 value) = _trimPlan(s);
        if (t == NONE) return 0;
        uint256 pos = s.m[t].posTwap;
        uint128 l = uint128(uint256(liquidityOf[t]) * value / pos);
        if (l == 0) return 0;
        (uint256 tok, uint256 b) = _v().positionBurn(t, l);
        liquidityOf[t] -= l;
        idleBuck += b;
        if (tok > 0) {
            (uint256 spent, uint256 got) = _v().monetaryLeg(t, false, tok);
            idleBuck += got;
            idleToken[t] += tok - spent;
        }
        _settle();
        emit WheelWork(TRIM, t, value);
        return 1;
    }
}
