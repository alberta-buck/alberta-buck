// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityStorage} from "./BuckBasketEquityStorage.sol";
import {IBuckBasketVenue} from "./IBuckBasketVenue.sol";
import {IBasketSignals} from "./IBasketSignals.sol";
import {IBasketUpkeep} from "./IBasketUpkeep.sol";
import {PortfolioMath as PM} from "./PortfolioMath.sol";

/// @notice Replacement components facet: Daily, Sync(i), Respond. One response
///         sells at most one leg and buys at most one, with a fresh credit mark
///         between the two. Idle TOKEN participates in the same allocation as
///         LP; waiting inventory never vetoes work in another leg.
contract BuckBasketEquityResponse is BuckBasketEquityStorage, IBasketUpkeep {
    uint256 private constant NONE = type(uint256).max;
    int256 private constant TICK_LN_WAD = 99995000333297;
    struct Plan { uint256 sell; uint256 buy; uint256 sellCap; uint256 buyCap; bool pairOnly; }
    // action: 0 mark-only, 1 sell, 2 buy, 3 transfer, 4 pair existing inventory.
    event Responded(uint8 indexed action, uint256 sell, uint256 buy, uint256 proceeds, uint256 buySpent);

    function _v() private view returns (IBuckBasketVenue) { return IBuckBasketVenue(address(this)); }

    function wheelDue(uint8 kind, uint256 i) public view virtual returns (bool) {
        if (kind == 0) return block.timestamp / 1 days > lastDay;
        if (kind == 1) return i < constituents.length && liquidityOf[i] > 0 && block.timestamp / 1 days > lastSyncDay[i];
        if (kind != 2 || block.timestamp < uint256(lastResponseAt) + response.interval) return false;
        if (responseSignals != address(0) && IBasketSignals(responseSignals).isDue()) return true;
        Snap memory s = _snap();
        Plan memory p = _plan(s);
        return p.sell != NONE || (p.buy != NONE && _usableS(s) > _grainS(s)) || _staleMark(s);
    }

    function wheelStep(uint8 kind, uint256 i) external returns (uint256) {
        if (msg.sender != wheel || wheel == address(0)) revert NotWheel();
        if (kind == 0) return _daily();
        if (kind == 1) {
            if (i >= constituents.length || liquidityOf[i] == 0 || block.timestamp / 1 days <= lastSyncDay[i]) return 0;
            lastSyncDay[i] = uint64(block.timestamp / 1 days);
            (uint256 t, uint256 b) = _v().positionSync(i);
            idleToken[i] += t;
            _markS(_snap());
            emit WheelWork(1, i, b);
            return 1;
        }
        if (kind != 2 || block.timestamp < uint256(lastResponseAt) + response.interval) return 0;
        return _respond();
    }

    function _daily() private returns (uint256) {
        uint64 day = uint64(block.timestamp / 1 days);
        if (day <= lastDay) return 0;
        uint256 flow = uint256(dayFlow < 0 ? -dayFlow : dayFlow);
        uint256 square = flow * flow / 1e18;
        uint256 span = uint256(eq.flowDays) + 1;
        flowMs = square >= flowMs ? flowMs + (square - flowMs) * 2 / span : flowMs - (flowMs - square) * 2 / span;
        dayFlow = 0;
        lastDay = day;
        if (_bk().signedBalanceOf(address(this)) < 0 && _bk().reliefOf(address(this)) > 0) _bk().settleRelief();
        _markS(_snap());
        emit WheelWork(0, 0, flow);
        return 1;
    }

    function _tokenValue(Snap memory s, uint256 i) internal view returns (uint256) {
        return idleToken[i] * s.m[i].pTwap / (10 ** constituents[i].decimals);
    }

    function _staleMark(Snap memory s) internal view returns (bool) {
        if (address(credit) == address(0)) return false;
        uint256 value = _equityS(s, MARK_LOW);
        uint256 mark = credit.markOf(creditId);
        return (value > mark ? value - mark : mark - value) > _grainS(s);
    }

    function _plan(Snap memory s) internal view returns (Plan memory p) {
        p.sell = NONE; p.buy = NONE;
        IBasketSignals signal = IBasketSignals(responseSignals);
        bool directed = address(signal) != address(0);
        if (directed && !signal.fresh()) return p;
        uint256 n = s.m.length;
        uint256[] memory value = new uint256[](n);
        uint256[] memory rawTarget = new uint256[](n);
        uint256 total;
        for (uint256 i; i < n; ++i) {
            value[i] = s.m[i].posTwap + _tokenValue(s, i);
            total += value[i];
            int256 excursion = directed ? signal.excursion(i) : int256(0);
            int256 lean = -(excursion * TICK_LN_WAD / 1e18) * response.tiltWad / 1e18;
            rawTarget[i] = constituents[i].targetWeightBp * PM.expWad(lean) / 1e18;
        }
        // A BUCK-only deposit has no TOKEN weights yet. Allocate the usable
        // cash against the declared basket to bootstrap its first position.
        if (total == 0) total = _usableS(s);
        if (total == 0) return p;
        uint256[] memory weights = PM.weights(value);
        uint256[] memory targets = PM.weights(rawTarget);
        uint256 band = total * eq.weightBandBp / 10000;
        uint256 grain = _grainS(s);
        uint256 bestSell; uint256 bestBuy;
        // Refill liquidity/deleverage even when all weights are balanced.
        uint256 limit = s.k * _equityS(s, MARK_LOW) / 1e18;
        bool underwater = s.lien > limit;
        uint256 targetLiquidity = _targetS(s);
        uint256 shortfall = s.spend + grain < targetLiquidity * (10000 - eq.bandBp) / 10000
            ? targetLiquidity - s.spend : 0;
        if (underwater) shortfall += s.lien - limit;
        for (uint256 i; i < n; ++i) {
            if (s.m[i].depth == 0) continue;
            uint256 declared = constituents[i].targetWeightBp;
            uint256 declaredValue = total * declared / 10000;
            uint256 targetValue = total * targets[i] / 10000;
            uint256 leash = declared * response.leashBp / 10000;
            bool over = weights[i] > declared + leash;
            bool under = weights[i] + leash < declared;
            uint256 sellAim;
            uint256 buyAim;
            if (over) sellAim = value[i] > declaredValue ? value[i] - declaredValue : 0;
            else if (value[i] > targetValue + band && (!directed || !signal.running(i, 1)))
                sellAim = value[i] - targetValue - band;
            if (under) buyAim = declaredValue > value[i] ? declaredValue - value[i] : 0;
            else if (targetValue > value[i] + band && (!directed || !signal.running(i, -1)))
                buyAim = targetValue - value[i] - band;
            // Cash needs take priority over trend gating. They reduce risk;
            // the liquidity target must not force a simultaneous new draw.
            if (shortfall > sellAim) sellAim = shortfall;
            if (shortfall > 0) buyAim = 0;
            uint256 step = value[i] * eq.stepBp / 10000;
            // A zero holding must be able to receive its first allocation.
            uint256 buyStep = (value[i] > declaredValue ? value[i] : declaredValue) * eq.stepBp / 10000;
            if (directed && signal.turned(i) && !over && shortfall == 0) step = step * signal.votes(i) / 6;
            if (directed && signal.turned(i) && !under) buyStep = buyStep * signal.votes(i) / 6;
            uint256 sellCap = PM.min(sellAim, PM.min(step, 2 * _swapCap(s.m[i])));
            uint256 buyCap = PM.min(buyAim, PM.min(buyStep, _swapCap(s.m[i])));
            if (sellCap > grain && sellAim > bestSell) {
                p.sell = i; p.sellCap = sellCap; bestSell = sellAim;
            }
            if (buyCap > grain && buyAim > bestBuy) {
                p.buy = i; p.buyCap = buyCap; bestBuy = buyAim;
            }
        }
        // Balanced waiting TOKEN can be paired without another swap. Size
        // against the expanded book so pairing cannot favour an overweight
        // arrival leg. This also bootstraps LP after balanced initial deposits.
        if (p.sell == NONE && p.buy == NONE && !underwater && shortfall == 0) {
            uint256 usable = _usableS(s);
            for (uint256 i; i < n; ++i) {
                if (directed && signal.running(i, -1)) continue;
                uint256 expandedTarget = (total + usable) * targets[i] / 10000;
                if (expandedTarget <= value[i]) continue;
                uint256 cap = PM.min(_tokenValue(s, i), PM.min(expandedTarget - value[i], usable));
                cap = PM.min(cap, PM.min(value[i] * eq.stepBp / 10000, _swapCap(s.m[i])));
                if (cap > grain && cap > p.buyCap) { p.buy = i; p.buyCap = cap; p.pairOnly = true; }
            }
        }
    }

    function _respond() internal virtual returns (uint256) {
        if (responseSignals != address(0)) { try IBasketSignals(responseSignals).observe() {} catch {} }
        Snap memory s = _snap();
        bool stale = _staleMark(s);
        _markS(s);
        Plan memory p = _plan(s);
        int256 headroom0 = _headroomS(s);
        uint256 usable0 = _usableS(s);
        uint256 proceeds;
        bool sold;
        if (p.sell != NONE) {
            uint256 beforeSpend = s.spend;
            sold = _sell(s, p.sell, p.sellCap);
            uint256 afterSpend = _bk().balanceOf(address(this));
            proceeds = afterSpend > beforeSpend ? afterSpend - beforeSpend : 0;
            // Never re-mark the opening snapshot after changing the book.
            s = _snap();
            _markS(s);
        }
        uint256 budget;
        uint256 buySpent;
        if (p.buy != NONE && headroom0 >= 0) {
            budget = PM.min(proceeds, p.buyCap);
            if (_headroomS(s) >= 0) budget += PM.min(usable0, p.buyCap - budget);
            budget = PM.min(budget, s.spend);
            if (budget > _grainS(s)) buySpent = _buy(p.buy, budget, p.pairOnly);
            if (buySpent > 0) _markS(_snap());
        }
        if (!sold && buySpent == 0 && !stale) return 0;
        lastResponseAt = uint64(block.timestamp);
        uint8 action = sold ? (buySpent > 0 ? 3 : 1) : (buySpent > 0 ? (p.pairOnly ? 4 : 2) : 0);
        emit Responded(action, p.sell, p.buy, proceeds, buySpent);
        emit WheelWork(2, 0, buySpent + proceeds);
        return 1;
    }

    function _sell(Snap memory s, uint256 i, uint256 cap) internal returns (bool) {
        uint256 swapCap = _swapCap(s.m[i]);
        uint256 idleValue = PM.min(_tokenValue(s, i), PM.min(cap, swapCap));
        uint256 dxIdle = idleValue * (10 ** constituents[i].decimals) / s.m[i].pTwap;
        uint256 still = PM.min(cap, swapCap) - idleValue;
        uint256 burnValue = PM.min(2 * still, PM.min(s.m[i].posTwap, cap - idleValue));
        uint128 burn = s.m[i].posTwap == 0 ? 0
            : uint128(uint256(liquidityOf[i]) * burnValue / s.m[i].posTwap);
        uint256 tokenOut;
        if (burn > 0) {
            (tokenOut,) = _v().positionBurn(i, burn);
            liquidityOf[i] -= burn;
        }
        idleToken[i] += tokenOut;
        uint256 dx = dxIdle + tokenOut;
        // Burning at spot can release more TOKEN than its TWAP half. Bound
        // the actual swap input, leave any remainder in the wallet.
        dx = PM.min(dx, swapCap * (10 ** constituents[i].decimals) / s.m[i].pHigh);
        uint256 spent;
        if (dx > 0) (spent,) = _v().monetaryLeg(i, false, dx);
        idleToken[i] -= spent;
        return burn > 0 || spent > 0;
    }

    function _buy(uint256 i, uint256 budget, bool pairOnly) internal returns (uint256 used) {
        uint256 half = pairOnly ? 0 : budget / 2;
        uint256 spent;
        if (half > 0) {
            uint256 got;
            (spent, got) = _v().monetaryLeg(i, true, half);
            idleToken[i] += got;
        }
        (uint128 l, uint256 tokens, uint256 bucks) = _v().positionMint(i, idleToken[i], budget - half);
        liquidityOf[i] += l;
        idleToken[i] -= tokens;
        used = spent + bucks;
    }
}
