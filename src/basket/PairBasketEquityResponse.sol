// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityResponse} from "./BuckBasketEquityResponse.sol";
import {PairRebalancePolicy} from "./PairRebalancePolicy.sol";
import {IBasketSignals} from "./BasketSignals.sol";
import {PortfolioMath as PM} from "./PortfolioMath.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

/// @notice Experimental response facet: one donor and proportional eligible
/// recipients per response. Reuses the established LP, cash and credit books.
/// Risk/initialization work retains the legacy response; discretionary work
/// uses declared allocation gaps and graded pair intent, with no target lean.
contract PairBasketEquityResponse is BuckBasketEquityResponse {
    uint256 private constant NONE = type(uint256).max;
    PairRebalancePolicy public immutable pairPolicy;
    struct Allocation {
        uint256 donor;
        uint256 total;
        uint256[] recipients;
        bool fallbackRequired;
    }
    event PairResponse(uint256 indexed donor, uint256 planned, uint256 proceeds, uint256 bought);

    constructor(address policy) { pairPolicy = PairRebalancePolicy(policy); }

    function _allocation(Snap memory s) internal view virtual returns (Allocation memory a) {
        uint256 n = s.m.length;
        require(n == pairPolicy.count() && address(this) == pairPolicy.basket(), "pair basket");
        require(response.tiltWad == 0, "pair targets are declared weights");
        a.donor = NONE; a.recipients = new uint256[](n);
        uint256[] memory value = new uint256[](n);
        uint256 total;
        for (uint256 i; i < n; ++i) { value[i] = s.m[i].posTwap + _tokenValue(s, i); total += value[i]; }
        uint256 grain = _grainS(s);
        // Existing liquidity/credit safeguards are an explicit override,
        // including cash-only and balanced waiting-TOKEN LP initialization.
        if (total == 0 || s.lien > s.k * _equityS(s, MARK_LOW) / 1e18
            || s.spend + grain < _targetS(s) * (10000 - eq.bandBp) / 10000) {
            a.fallbackRequired = true; return a;
        }
        uint256[] memory excess = new uint256[](n);
        uint256[] memory deficit = new uint256[](n);
        bool[] memory material = new bool[](n);
        uint256 band = total * eq.weightBandBp / 10000;
        bool outside;
        for (uint256 i; i < n; ++i) {
            uint256 weight = constituents[i].targetWeightBp;
            uint256 target = total * weight / 10000;
            uint256 gap = value[i] > target ? value[i] - target : target - value[i];
            if (gap * 10000 > total * weight * response.leashBp / 10000) {
                a.fallbackRequired = true; return a;
            }
            if (s.m[i].depth == 0) continue;
            material[i] = gap > band;
            if (material[i]) outside = true;
            // The band triggers consideration; capacity is the safe distance
            // to target. Requiring two independent band excursions starves
            // useful counter-legs, and subtracting both bands starves size.
            if (value[i] > target) excess[i] = gap;
            else deficit[i] = gap;
        }
        if (!outside) return a;
        // A nominal request barely above grain can fall below it after the
        // sale, reverting a predictable dust transfer. Reserve room for the
        // venue's allowed price band on input and output; execution still
        // rechecks actual proceeds, credit and each recipient's grain.
        uint256 tolerance = defaultMaxDeviationBp;
        if (tolerance >= 10000) return a;
        uint256 minimum = grain * (10000 + tolerance) / (10000 - tolerance) + 1;
        uint256 elapsed = block.timestamp - pairPolicy.lastExecutionAt();
        for (uint256 i; i < n; ++i) {
            if (excess[i] == 0) continue;
            uint256 eligible;
            uint256 weightedRate;
            for (uint256 j; j < n; ++j) {
                if (deficit[j] == 0 || (!material[i] && !material[j])) continue;
                uint256 rate = pairPolicy.desire(i, j);
                if (rate == 0) continue;
                eligible += deficit[j]; weightedRate += deficit[j] * rate;
            }
            if (eligible == 0) continue;
            uint256 amount = PM.min(excess[i], eligible) * pairPolicy.fraction(weightedRate / eligible, elapsed) / 1e18;
            amount = PM.min(amount, PM.min(value[i] * eq.stepBp / 10000, _swapCap(s.m[i])));
            if (amount <= minimum || amount <= a.total) continue;
            uint256[] memory recipients = new uint256[](n);
            uint256 executable;
            for (uint256 j; j < n; ++j) {
                if (deficit[j] == 0 || (!material[i] && !material[j]) || pairPolicy.desire(i, j) == 0) continue;
                uint256 part = amount * deficit[j] / eligible;
                // One common scale is used below for depth/step constraints;
                // do not separately overfill a small recipient.
                recipients[j] = part; executable += part;
            }
            uint256 scale = 1e18;
            for (uint256 j; j < n; ++j) {
                if (recipients[j] == 0) continue;
                uint256 target = total * constituents[j].targetWeightBp / 10000;
                uint256 step = (value[j] > target ? value[j] : target) * eq.stepBp / 10000;
                uint256 cap = PM.min(_swapCap(s.m[j]), PM.min(deficit[j], step));
                if (cap < recipients[j]) scale = PM.min(scale, cap * 1e18 / recipients[j]);
            }
            executable = 0;
            for (uint256 j; j < n; ++j) {
                recipients[j] = recipients[j] * scale / 1e18;
                // Leave uneconomic dust unrequested rather than forcing a leg.
                if (recipients[j] <= minimum) recipients[j] = 0;
                executable += recipients[j];
            }
            if (executable > a.total) { a.donor = i; a.total = executable; a.recipients = recipients; }
        }
    }

    /// @dev Pair inventory already owned by the basket. Adding BUCK expands
    /// the book, so rank shortages in the funded allocation, not capped step
    /// sizes (which repeatedly favour the largest leg). No TOKEN is swapped;
    /// a price-turn veto has no role in this independent placement operation.
    function _idlePlacement(Snap memory s) internal view returns (Plan memory p) {
        p.sell = NONE; p.buy = NONE; p.pairOnly = true;
        uint256 usable = _usableS(s);
        uint256 grain = _grainS(s);
        if (usable <= grain) return p;
        uint256 expanded = _grossS(s, MARK_TWAP) + usable;
        uint256 best;
        for (uint256 i; i < s.m.length; ++i) {
            uint256 idle = _tokenValue(s, i);
            uint256 value = s.m[i].posTwap + idle;
            uint256 target = expanded * constituents[i].targetWeightBp / 10000;
            if (target <= value) continue;
            uint256 gap = target - value;
            uint256 cap = PM.min(idle, PM.min(gap, usable));
            cap = PM.min(cap, PM.min(value * eq.stepBp / 10000, _swapCap(s.m[i])));
            if (gap > best && cap > grain) { best = gap; p.buy = i; p.buyCap = cap; }
        }
    }

    function wheelDue(uint8 kind, uint256 i) public view override returns (bool) {
        if (kind != 2) return super.wheelDue(kind, i);
        if (block.timestamp < uint256(lastResponseAt) + response.interval) return false;
        if (pairPolicy.signals().isDue()) return true;
        Snap memory s = _snap();
        Allocation memory a = _allocation(s);
        if (a.fallbackRequired) return super.wheelDue(kind, i);
        return a.donor != NONE || _idlePlacement(s).buy != NONE || _staleMark(s);
    }

    function _spotTicks() private view returns (int24[] memory ticks) {
        ticks = new int24[](constituents.length);
        for (uint256 i; i < ticks.length; ++i) (, ticks[i],,,,,) = IUniswapV3Pool(constituents[i].pool).slot0();
    }

    function _record(int24[] memory beforeTicks) private {
        int24[] memory afterTicks = _spotTicks();
        for (uint256 i; i < beforeTicks.length; ++i)
            if (beforeTicks[i] != afterTicks[i]) pairPolicy.recordImpact(i, beforeTicks[i], afterTicks[i]);
    }

    function _respond() internal virtual override returns (uint256) {
        pairPolicy.observe();
        if (responseSignals != address(0)) { try IBasketSignals(responseSignals).observe() {} catch {} }
        Snap memory s = _snap();
        Allocation memory a = _allocation(s);
        int24[] memory beforeTicks = _spotTicks();
        if (a.fallbackRequired) {
            uint256 work = super._respond();
            _record(beforeTicks);
            return work;
        }
        bool stale = _staleMark(s); _markS(s);
        if (a.donor == NONE) {
            Plan memory p = _idlePlacement(s);
            if (p.buy != NONE && _headroomS(s) >= 0) {
                uint256 placed = _buy(p.buy, PM.min(p.buyCap, s.spend), true);
                if (placed > 0) {
                    _markS(_snap());
                    lastResponseAt = uint64(block.timestamp);
                    emit Responded(4, NONE, p.buy, 0, placed);
                    emit WheelWork(2, 0, placed);
                    return 1;
                }
            }
            if (!stale) return 0;
            lastResponseAt = uint64(block.timestamp);
            emit Responded(0, NONE, NONE, 0, 0);
            emit WheelWork(2, 0, 0);
            return 1;
        }
        uint256 spendBefore = s.spend;
        require(_sell(s, a.donor, a.total), "pair sell refused");
        uint256 spendAfter = _bk().balanceOf(address(this));
        uint256 proceeds = spendAfter > spendBefore ? spendAfter - spendBefore : 0;
        s = _snap(); _markS(s);
        // This operation transfers donor value; it does not add a fresh draw
        // merely because the credit account has unused headroom.
        uint256 budget = PM.min(a.total, PM.min(proceeds, s.spend));
        require(_headroomS(s) >= 0 && budget > _grainS(s), "pair cash unavailable");
        uint256 used;
        for (uint256 j; j < a.recipients.length; ++j) {
            if (a.recipients[j] == 0) continue;
            uint256 part = budget * a.recipients[j] / a.total;
            // Every leg rechecks current venue bounds; any refused planned
            // recipient rolls the entire response back, including the sale.
            require(part > _grainS(s) && part <= _bk().balanceOf(address(this)), "pair recipient budget");
            uint256 filled = _buy(j, part, false);
            require(filled > 0, "pair buy refused");
            used += filled; s = _snap(); _markS(s);
            require(_headroomS(s) >= 0, "pair credit");
        }
        _record(beforeTicks);
        pairPolicy.recordExecution();
        lastResponseAt = uint64(block.timestamp);
        emit Responded(3, a.donor, NONE, proceeds, used);
        emit PairResponse(a.donor, a.total, proceeds, used);
        emit WheelWork(2, 0, proceeds + used);
        return 1;
    }
}
