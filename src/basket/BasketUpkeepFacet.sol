// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityStorage} from "./BuckBasketEquityStorage.sol";
import {IBuckBasketVenue} from "./IBuckBasketVenue.sol";
import {IBasketUpkeep} from "./IBasketUpkeep.sol";
import {IRebalancePolicy} from "./IRebalancePolicy.sol";
import {PortfolioMath as PM} from "./PortfolioMath.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

/// @notice The basket's upkeep facet: Daily, Sync(i) and Respond. A response
/// does one of three things. Where the book needs it, a safeguard: cash for
/// short liquidity or a lien above K, a constituent brought back inside its
/// leash, a first cash deposit placed, all with no view on prices. Otherwise
/// one paired transfer, from one donor to proportional eligible recipients,
/// on the gaps from its targets and the policy's graded intent. Failing
/// both, it pairs TOKEN the basket already holds. Idle TOKEN counts in the
/// same allocation as LP; waiting inventory never vetoes work elsewhere.
///
/// A target is the declared weight, leaned against price when the shell's
/// tilt is set: lowered for a constituent above its reference price against
/// the others, raised for one below. A liquidity position sells as its price
/// rises, so its share moves half as far as a wallet's; a tilt of one half
/// asks for the other half. The leash and the safeguard keep the declared
/// weights.
contract BasketUpkeepFacet is BuckBasketEquityStorage, IBasketUpkeep {
    uint256 private constant NONE = type(uint256).max;
    IRebalancePolicy public immutable pairPolicy;
    // sells: the value each constituent is to sell; sell: the one selling most, NONE when none does.
    struct Plan { uint256 sell; uint256 buy; uint256[] sells; uint256 buyCap; bool pairOnly; }
    struct Allocation {
        uint256 donor;
        uint256 total;
        uint256[] recipients;
        bool needsSafeguard;
        int256[] gaps; // each constituent over (+) or under (-) its target, as a share of the book, 1e18
        uint256[] rates; // the rate each donor is asked at toward its eligible recipients, per day, 1e18
    }
    // action: 0 mark-only, 1 sell, 2 buy, 3 transfer, 4 pair existing inventory.
    event Responded(uint8 indexed action, uint256 sell, uint256 buy, uint256 proceeds, uint256 buySpent);
    event PairResponse(uint256 indexed donor, uint256 planned, uint256 proceeds, uint256 bought);

    constructor(address policy) { pairPolicy = IRebalancePolicy(policy); }

    function _v() private view returns (IBuckBasketVenue) { return IBuckBasketVenue(address(this)); }

    function wheelDue(uint8 kind, uint256 i) public view returns (bool) {
        if (kind == 0) return block.timestamp / 1 days > lastDay;
        if (kind == 1) return i < constituents.length && liquidityOf[i] > 0 && block.timestamp / 1 days > lastSyncDay[i];
        if (kind != 2 || block.timestamp < uint256(lastResponseAt) + response.interval) return false;
        if (pairPolicy.signals().isDue()) return true;
        Snap memory s = _snap();
        if (_staleMark(s)) return true;
        Allocation memory a = _allocation(s);
        if (!a.needsSafeguard) return a.donor != NONE || _idlePlacement(s, 0).buy != NONE;
        Plan memory p = _safeguard(s);
        return p.sell != NONE || (p.buy != NONE && _usableS(s) > _grainS(s));
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

    function _beyondLeash(uint256 gap, uint256 total, uint256 weight) private view returns (bool) {
        return gap * 10000 > total * weight * response.leashBp / 10000;
    }

    /// @dev Each constituent's target value. With no tilt, its declared
    /// weight of the book. With one, that weight times (reference / price) to
    /// the tilt, shared out over the book and held within the lean cap. Only
    /// one price against another matters: a move in the BUCK itself cancels.
    function _targets(Snap memory s, uint256 total) internal view returns (uint256[] memory target) {
        uint256 n = s.m.length;
        target = new uint256[](n);
        uint256 tilt = response.tiltBp;
        if (tilt == 0) {
            for (uint256 i; i < n; ++i) target[i] = total * constituents[i].targetWeightBp / 10000;
            return target;
        }
        uint256 sum;
        for (uint256 i; i < n; ++i) {
            uint256 price = s.m[i].pTwap;
            uint256 anchor = referencePrice[i] == 0 ? constituents[i].initialPriceInBuck : referencePrice[i];
            int256 above = price == 0 || anchor == 0 ? int256(0) : PM.lnWad(price * 1e18 / anchor);
            target[i] = constituents[i].targetWeightBp * PM.expWad(-above * int256(tilt) / 10000);
            sum += target[i];
        }
        for (uint256 i; i < n; ++i) {
            uint256 declared = total * constituents[i].targetWeightBp / 10000;
            uint256 most = declared * response.leanCapBp / 10000;
            uint256 leaned = total * target[i] / sum;
            target[i] = leaned > declared + most ? declared + most : leaned + most < declared ? declared - most : leaned;
        }
    }

    function _allocation(Snap memory s) internal view virtual returns (Allocation memory a) {
        uint256 n = s.m.length;
        require(n == pairPolicy.count() && address(this) == pairPolicy.basket(), "pair basket");
        a.donor = NONE; a.recipients = new uint256[](n);
        uint256[] memory value = new uint256[](n);
        uint256 total;
        for (uint256 i; i < n; ++i) { value[i] = s.m[i].posTwap + _tokenValue(s, i); total += value[i]; }
        uint256 grain = _grainS(s);
        // The book comes first: a cash-only start, a lien above K, short
        // liquidity or a constituent beyond its leash is safeguard work.
        if (total == 0 || s.lien > s.k * _equityS(s, MARK_LOW) / 1e18
            || s.spend + grain < _targetS(s) * (10000 - eq.bandBp) / 10000) {
            a.needsSafeguard = true; return a;
        }
        uint256[] memory excess = new uint256[](n);
        uint256[] memory deficit = new uint256[](n);
        uint256[] memory targets = _targets(s, total);
        a.gaps = new int256[](n); a.rates = new uint256[](n);
        for (uint256 i; i < n; ++i) {
            uint256 weight = constituents[i].targetWeightBp;
            uint256 target = total * weight / 10000;
            uint256 gap = value[i] > target ? value[i] - target : target - value[i];
            // The leash is the mandate: it is kept on the declared weight.
            if (_beyondLeash(gap, total, weight)) { a.needsSafeguard = true; return a; }
            target = targets[i];
            gap = value[i] > target ? value[i] - target : target - value[i];
            a.gaps[i] = value[i] > target ? int256(gap * 1e18 / total) : -int256(gap * 1e18 / total);
            if (s.m[i].depth == 0) continue;
            if (value[i] > target) excess[i] = gap;
            else deficit[i] = gap;
        }
        // Which gaps may be worked at all. With a patient policy, those that
        // have built their full urgency; with none, those beyond the fixed
        // band. Either triggers consideration only; capacity is the safe
        // distance to target. Requiring both legs to qualify starves useful
        // counter-legs, and subtracting both bands starves size.
        bool[] memory material = new bool[](n);
        {
            bool patient = pairPolicy.patience() != 0;
            uint256 mask = patient ? pairPolicy.urgent(a.gaps) : 0;
            uint256 band = total * eq.weightBandBp / 10000;
            bool outside;
            for (uint256 i; i < n; ++i) {
                if (s.m[i].depth == 0) continue;
                material[i] = patient ? (mask >> i) & 1 == 1 : excess[i] + deficit[i] > band;
                if (material[i]) outside = true;
            }
            if (!outside) return a;
        }
        // A nominal request barely above grain can fall below it after the
        // sale, reverting a predictable dust transfer. Reserve room for the
        // venue's allowed price band on input and output; execution still
        // rechecks actual proceeds, credit and each recipient's grain.
        uint256 tolerance = defaultMaxDeviationBp;
        if (tolerance >= 10000) return a;
        uint256 minimum = grain * (10000 + tolerance) / (10000 - tolerance) + 1;
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
            a.rates[i] = weightedRate / eligible;
            uint256 amount = PM.min(excess[i], eligible) * pairPolicy.share(i, a.rates[i]) / 1e18;
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
                uint256 step = (value[j] > targets[j] ? value[j] : targets[j]) * eq.stepBp / 10000;
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

    /// @dev What the book needs before any view on prices. A constituent
    /// beyond its leash is sold or bought back toward its declared weight. A
    /// need for cash beyond that is spread over every constituent, each
    /// giving in proportion to what it may give in one turn, and while cash
    /// is needed nothing is bought. When none is, TOKEN already held may be
    /// placed in the same turn: that swaps nothing, so a leash sale need not
    /// hold it back.
    function _safeguard(Snap memory s) internal view returns (Plan memory p) {
        p.sell = NONE; p.buy = NONE;
        uint256 n = s.m.length;
        p.sells = new uint256[](n);
        uint256[] memory value = new uint256[](n);
        uint256 total;
        for (uint256 i; i < n; ++i) { value[i] = s.m[i].posTwap + _tokenValue(s, i); total += value[i]; }
        // A cash-only book has no weights yet: aim its usable cash at the declared ones.
        if (total == 0) total = _usableS(s);
        if (total == 0) return p;
        uint256 grain = _grainS(s);
        uint256 limit = s.k * _equityS(s, MARK_LOW) / 1e18;
        uint256 liquidity = _targetS(s);
        uint256 need = s.spend + grain < liquidity * (10000 - eq.bandBp) / 10000 ? liquidity - s.spend : 0;
        if (s.lien > limit) need += s.lien - limit;
        uint256[] memory room = new uint256[](n);
        uint256 rooms;
        uint256 raised;
        uint256 mostUnder;
        for (uint256 i; i < n; ++i) {
            if (s.m[i].depth == 0) continue;
            uint256 weight = constituents[i].targetWeightBp;
            uint256 target = total * weight / 10000;
            bool over = value[i] > target;
            uint256 gap = over ? value[i] - target : target - value[i];
            bool leashed = _beyondLeash(gap, total, weight);
            room[i] = PM.min(value[i] * eq.stepBp / 10000, 2 * _swapCap(s.m[i]));
            if (over && leashed) { p.sells[i] = PM.min(gap, room[i]); raised += p.sells[i]; }
            rooms += room[i] - p.sells[i];
            if (need > 0 || over || !leashed) continue;
            // A zero holding must be able to receive its first allocation.
            uint256 buyCap = PM.min(gap, PM.min(target * eq.stepBp / 10000, _swapCap(s.m[i])));
            if (buyCap > grain && gap > mostUnder) { p.buy = i; p.buyCap = buyCap; mostUnder = gap; }
        }
        if (need > raised && rooms > 0) {
            uint256 more = need - raised;
            for (uint256 i; i < n; ++i) {
                uint256 rest = room[i] - p.sells[i];
                p.sells[i] += more >= rooms ? rest : more * rest / rooms;
            }
        }
        uint256 most;
        raised = 0;
        for (uint256 i; i < n; ++i) {
            if (p.sells[i] <= grain) { p.sells[i] = 0; continue; }
            raised += p.sells[i];
            if (p.sells[i] > most) { most = p.sells[i]; p.sell = i; }
        }
        if (need == 0 && p.buy == NONE) {
            // What a leash sale raises can pair waiting TOKEN in the same turn.
            Plan memory idle = _idlePlacement(s, raised);
            p.buy = idle.buy; p.buyCap = idle.buyCap; p.pairOnly = true;
        }
    }

    /// @dev Pair inventory already owned by the basket, with the cash it can
    /// spare and any more this turn is about to raise. Adding BUCK expands
    /// the book, so rank shortages in the funded allocation, not capped step
    /// sizes (which repeatedly favour the largest leg). No TOKEN is swapped,
    /// so no policy is asked.
    function _idlePlacement(Snap memory s, uint256 raised) internal view returns (Plan memory p) {
        p.sell = NONE; p.buy = NONE; p.pairOnly = true;
        uint256 usable = _usableS(s) + raised;
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

    function _spotTicks() private view returns (int24[] memory ticks) {
        ticks = new int24[](constituents.length);
        for (uint256 i; i < ticks.length; ++i) (, ticks[i],,,,,) = IUniswapV3Pool(constituents[i].pool).slot0();
    }

    function _record(int24[] memory beforeTicks) private {
        int24[] memory afterTicks = _spotTicks();
        for (uint256 i; i < beforeTicks.length; ++i)
            if (beforeTicks[i] != afterTicks[i]) pairPolicy.recordImpact(i, beforeTicks[i], afterTicks[i]);
    }

    function _respond() internal virtual returns (uint256) {
        pairPolicy.observe();
        Snap memory s = _snap();
        Allocation memory a = _allocation(s);
        int24[] memory beforeTicks = _spotTicks();
        if (a.needsSafeguard) {
            uint256 work = _protect(s);
            _record(beforeTicks);
            return work;
        }
        pairPolicy.recordGaps(a.gaps);
        pairPolicy.recordRates(a.rates);
        bool stale = _staleMark(s); _markS(s);
        if (a.donor == NONE) {
            Plan memory p = _idlePlacement(s, 0);
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
        pairPolicy.recordExecution(a.donor);
        lastResponseAt = uint64(block.timestamp);
        emit Responded(3, a.donor, NONE, proceeds, used);
        emit PairResponse(a.donor, a.total, proceeds, used);
        emit WheelWork(2, 0, proceeds + used);
        return 1;
    }

    /// @dev Carry out the safeguard plan, with a fresh credit mark between
    /// its sale and its purchase.
    function _protect(Snap memory s) private returns (uint256) {
        bool stale = _staleMark(s);
        _markS(s);
        Plan memory p = _safeguard(s);
        int256 headroom0 = _headroomS(s);
        uint256 usable0 = _usableS(s);
        uint256 proceeds;
        bool sold;
        if (p.sell != NONE) {
            uint256 beforeSpend = s.spend;
            // Each sale is in its own pool, so the opening snapshot prices them all.
            for (uint256 i; i < p.sells.length; ++i)
                if (p.sells[i] > 0 && _sell(s, i, p.sells[i])) sold = true;
            uint256 afterSpend = _bk().balanceOf(address(this));
            proceeds = afterSpend > beforeSpend ? afterSpend - beforeSpend : 0;
            // Never re-mark the opening snapshot after changing the book.
            s = _snap();
            _markS(s);
        }
        uint256 buySpent;
        if (p.buy != NONE && headroom0 >= 0) {
            uint256 budget = PM.min(proceeds, p.buyCap);
            if (_headroomS(s) >= 0) budget += PM.min(usable0, p.buyCap - budget);
            budget = PM.min(budget, s.spend);
            if (budget > _grainS(s)) buySpent = _buy(p.buy, budget, p.pairOnly);
            if (buySpent > 0) _markS(_snap());
        }
        if (!sold && buySpent == 0 && !stale) return 0;
        lastResponseAt = uint64(block.timestamp);
        // 0 a mark alone, 1 sold, 2 bought, 3 sold and bought, 4 placed, 5 sold and placed.
        uint8 action = sold ? (buySpent > 0 ? (p.pairOnly ? 5 : 3) : 1) : (buySpent > 0 ? (p.pairOnly ? 4 : 2) : 0);
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
