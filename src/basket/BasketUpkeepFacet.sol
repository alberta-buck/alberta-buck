// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityStorage} from "./BuckBasketEquityStorage.sol";
import {IBuckBasketVenue} from "./IBuckBasketVenue.sol";
import {IBasketUpkeep} from "./IBasketUpkeep.sol";
import {IBasketReference} from "./IBasketReference.sol";
import {IRebalancePolicy} from "./IRebalancePolicy.sol";
import {PortfolioMath as PM} from "./PortfolioMath.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

/// @notice The basket's upkeep facet: Daily, Sync(i) and Respond, for a shell
/// whose credit is marked net (netMark): K bounds the BUCKs the basket has
/// let out, not those it still holds beside its TOKEN. Nothing is sold to
/// place a deposit or to pay an exit, so a response is three things.
///
/// 1. Pair every waiting TOKEN with issued BUCKs, at once. The BUCKs stay in
///    the position, so this uses none of the line.
/// 2. Size the book. The TOKEN it should hold is its equity plus the BUCKs it
///    aims to have issued. That aim leans against parity: where the basket
///    costs more than one BUCK the BUCK is cheap and the aim is below zero
///    (hold BUCKs); where it costs less, above (issue them). Once the TOKEN
///    held is further from that size than the band, whatever moved it, the
///    difference is bought or sold with no word from the policy: into the
///    constituents under their targets, or out of those over, each in
///    proportion to its gap, so the book lands as near its weights as it can.
/// 3. Work each constituent toward its target share of that book on its own:
///    sell one that is over, buy one that is under, each when the policy
///    sees it closing against the rest and at the policy's release. The two
///    legs need not meet in one turn: the line carries the difference.
///
/// Two things act with no view on prices. A constituent beyond its leash is
/// brought back at once. BUCKs issued beyond the ceiling, half of K x equity,
/// are sold down at once, every constituent giving its share; the other half
/// of the line is what exits are paid from.
///
/// A target is the declared weight, leaned against price when the shell's
/// tilt is set: lowered for a constituent above its reference price against
/// the others, raised for one below. A liquidity position sells as its price
/// rises, so its share moves half as far as a wallet's; a tilt of one half
/// asks for the other half. The leash keeps the declared weights.
contract BasketUpkeepFacet is BuckBasketEquityStorage, IBasketUpkeep {
    uint256 private constant NONE = type(uint256).max;
    /// @notice The most BUCKs to have issued in normal running, of K x equity.
    uint256 public constant CEILING_BP = 5000;
    IRebalancePolicy public immutable pairPolicy;
    /// @notice The parity reference: the basket's cost in BUCK, as last sampled.
    IBasketReference public immutable parity;
    /// @notice BUCKs to hold per unit of parity error while the BUCK is cheap, as a share of equity, 1e18.
    uint256 public immutable parityGain;
    /// @notice BUCKs to have let out per unit of parity error while the BUCK is dear, likewise. Deployed
    /// equal to parityGain, so the book leans against parity on both sides; a deployment may set either
    /// to zero. What is let out is bounded by the ceiling whatever the gain.
    uint256 public immutable parityGainDear;

    struct Book {
        uint256[] token;   // each constituent's TOKEN, in its position and the wallet, in BUCK at the TWAP
        uint256[] target;  // what the book asks of it
        uint256 total;     // the TOKEN the book should hold
        int256 issued;     // BUCKs let out: the lien net of relief and of the BUCKs held in the positions
        uint256 ceiling;   // the most to have issued in normal running
        uint256 grain;
    }
    struct Plan {
        int256[] gaps;     // each constituent over (+) or under (-) its target, as a share of the book, 1e18
        uint256[] rates;   // the rate each is worked at, per day, 1e18
        uint256[] sells;   // TOKEN to sell, in BUCK
        uint256[] buys;    // TOKEN to buy, in BUCK
    }
    // action: 0 a mark alone, 1 sold, 2 bought, 4 placed. proceeds: the TOKEN sold; buySpent: BUCKs paired or TOKEN bought.
    event Responded(uint8 indexed action, uint256 sell, uint256 buy, uint256 proceeds, uint256 buySpent);

    constructor(address policy, address parity_, uint256 parityGain_, uint256 parityGainDear_) {
        pairPolicy = IRebalancePolicy(policy);
        parity = IBasketReference(parity_);
        parityGain = parityGain_;
        parityGainDear = parityGainDear_;
    }

    function _v() private view returns (IBuckBasketVenue) { return IBuckBasketVenue(address(this)); }
    function _spendable() private view returns (uint256) { return _bk().balanceOf(address(this)); }

    function wheelDue(uint8 kind, uint256 i) public view returns (bool) {
        if (kind == 0) return block.timestamp / 1 days > lastDay;
        if (kind == 1) return i < constituents.length && liquidityOf[i] > 0 && block.timestamp / 1 days > lastSyncDay[i];
        if (kind != 2 || block.timestamp < uint256(lastResponseAt) + response.interval) return false;
        if (pairPolicy.signals().isDue()) return true;
        Snap memory s = _snap();
        if (_staleMark(s)) return true;
        uint256 grain = _grainS(s);
        for (uint256 j; j < s.m.length; ++j) if (s.m[j].depth != 0 && _tokenValue(s, j) > grain) return true;
        Plan memory p = _plan(s, _book(s));
        for (uint256 j; j < s.m.length; ++j) if (p.sells[j] > 0 || p.buys[j] > 0) return true;
        return false;
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

    /// @dev Settle the relief on the lien and mark. The day's flow is reported
    /// and cleared; nothing here rations room by it.
    function _daily() private returns (uint256) {
        uint64 day = uint64(block.timestamp / 1 days);
        if (day <= lastDay) return 0;
        uint256 flow = uint256(dayFlow < 0 ? -dayFlow : dayFlow);
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
        uint256 value = _creditValueS(s, _equityS(s, MARK_LOW));
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

    /// @dev Pair every waiting TOKEN with issued BUCKs. The BUCKs are still
    /// held afterwards, so each marking gives the room back: a deposit larger
    /// than the room is paired in a few passes.
    function _placeAll(Snap memory s) private returns (uint256 moved) {
        uint256 grain = _grainS(s);
        for (uint256 i; i < s.m.length; ++i) {
            if (s.m[i].depth == 0) continue;
            for (uint256 pass; pass < 4; ++pass) {
                uint256 budget = PM.min(_tokenValue(s, i), _spendable());
                if (budget <= grain) break;
                uint256 placed = _buy(i, budget, true);
                if (placed == 0) break;
                _markS(_snap());
                emit Responded(4, NONE, i, 0, placed);
                moved += placed;
            }
        }
    }

    /// @dev The book the equity asks for, and where each constituent stands in it.
    function _book(Snap memory s) internal view returns (Book memory b) {
        uint256 n = s.m.length;
        b.token = new uint256[](n);
        uint256 held;
        uint256 token;
        for (uint256 i; i < n; ++i) {
            held += s.m[i].posTwap / 2;
            b.token[i] = s.m[i].posTwap / 2 + _tokenValue(s, i);
            token += b.token[i];
        }
        b.issued = -s.buckEq - int256(held);
        int256 equity = int256(token) - b.issued;
        if (equity < 0) equity = 0;
        b.ceiling = s.k * uint256(equity) / 1e18 * CEILING_BP / 10000;
        // With no fresh reading the book stays the size it is.
        int256 aim = b.issued;
        if (parityGain == 0 && parityGainDear == 0) aim = 0;
        else if (parity.fresh()) {
            // Above zero the basket costs more than a BUCK: the BUCK is cheap, and the book holds
            // some. Below, it is dear, and the book lets some out. Each side has its own gain.
            int256 error = parity.rawValue() - 1e18;
            aim = -error * int256(error > 0 ? parityGain : parityGainDear) / 1e18 * equity / 1e18;
        }
        if (aim > int256(b.ceiling)) aim = int256(b.ceiling);
        if (aim < -int256(b.ceiling)) aim = -int256(b.ceiling);
        b.total = equity + aim > 0 ? uint256(equity + aim) : 0;
        b.target = _targets(s, b.total);
        b.grain = _grainS(s);
    }

    /// @dev How fast constituent i may be worked: the policy's rate for it
    /// against each of the others, weighed by their declared weights. Sold,
    /// it is the pair's seller; bought, its buyer.
    function _rate(uint256 i, bool sold, uint256 n) internal view virtual returns (uint256) {
        uint256 sum;
        uint256 weights;
        for (uint256 j; j < n; ++j) {
            if (j == i) continue;
            uint256 weight = constituents[j].targetWeightBp;
            sum += weight * (sold ? pairPolicy.desire(i, j) : pairPolicy.desire(j, i));
            weights += weight;
        }
        return weights == 0 ? 0 : sum / weights;
    }

    /// @dev Which gaps may be worked at all: with a patient policy, those
    /// that have built their full urgency; with none, those beyond the fixed
    /// band.
    function _eligible(Book memory b, int256[] memory gaps) private view returns (uint256 mask) {
        if (pairPolicy.patience() != 0) return pairPolicy.urgent(gaps);
        uint256 band = b.total * eq.weightBandBp / 10000;
        for (uint256 i; i < gaps.length; ++i) {
            uint256 gap = b.token[i] > b.target[i] ? b.token[i] - b.target[i] : b.target[i] - b.token[i];
            if (gap > band) mask |= uint256(1) << i;
        }
    }

    /// @dev The book's own move. Once the TOKEN held and the book's size differ by more than the band --
    /// whatever moved them: a BUCK deposit, an exit, the lean against parity, a K that frees the ceiling
    /// -- the difference is bought into the constituents under their targets, or sold from those over,
    /// each in proportion to its gap to the target at the book's new size. With none on the other side
    /// the book lands on its targets; otherwise each closes the same share of its gap. A sale leaves out
    /// what the ceiling already asks of every constituent.
    function _move(Book memory b, uint256 tokens, uint256 excess) private view returns (uint256[] memory move) {
        uint256 n = b.token.length;
        move = new uint256[](n);
        bool grow = b.total > tokens;
        uint256 amount = grow ? b.total - tokens : tokens - b.total;
        if (amount * 10000 <= b.total * eq.weightBandBp) return move;
        if (!grow) amount = amount > excess ? amount - excess : 0;
        uint256[] memory gap = new uint256[](n);
        uint256 sum;
        for (uint256 i; i < n; ++i) {
            if (grow ? b.target[i] > b.token[i] : b.token[i] > b.target[i])
                gap[i] = grow ? b.target[i] - b.token[i] : b.token[i] - b.target[i];
            sum += gap[i];
        }
        if (sum == 0) return move;
        for (uint256 i; i < n; ++i) move[i] = amount * gap[i] / sum;
    }

    function _plan(Snap memory s, Book memory b) internal view returns (Plan memory p) {
        uint256 n = s.m.length;
        require(n == pairPolicy.count() && address(this) == pairPolicy.basket(), "pair basket");
        p.gaps = new int256[](n); p.rates = new uint256[](n);
        p.sells = new uint256[](n); p.buys = new uint256[](n);
        uint256 tokens;
        for (uint256 i; i < n; ++i) {
            tokens += b.token[i];
            if (b.total > 0) p.gaps[i] = (int256(b.token[i]) - int256(b.target[i])) * 1e18 / int256(b.total);
        }
        // Beyond the ceiling every constituent gives its share, whatever the policy sees.
        uint256 excess = b.issued > int256(b.ceiling) ? uint256(b.issued - int256(b.ceiling)) : 0;
        if (excess > 0 && tokens > 0)
            for (uint256 i; i < n; ++i) p.sells[i] = excess * b.token[i] / tokens;
        uint256[] memory move = _move(b, tokens, excess);
        uint256 mask = _eligible(b, p.gaps);
        for (uint256 i; i < n; ++i) {
            if (s.m[i].depth == 0 || b.token[i] == b.target[i]) continue;
            bool over = b.token[i] > b.target[i];
            uint256 gap = over ? b.token[i] - b.target[i] : b.target[i] - b.token[i];
            uint256 amount;
            uint256 declared = b.total * constituents[i].targetWeightBp / 10000;
            if (_beyondLeash(b.token[i] > declared ? b.token[i] - declared : declared - b.token[i], b.total,
                             constituents[i].targetWeightBp)) {
                // The leash is the mandate: it is kept on the declared weight, and at once.
                amount = gap;
            } else if ((mask >> i) & 1 == 1) {
                p.rates[i] = _rate(i, over, n);
                amount = gap * pairPolicy.share(i, p.rates[i]) / 1e18;
            }
            // The book's own move needs no word from the policy.
            if (move[i] > amount) amount = move[i];
            if (over) p.sells[i] += amount; else p.buys[i] = amount;
        }
        uint256 selling;
        uint256 buying;
        for (uint256 i; i < n; ++i) {
            uint256 cap = _swapCap(s.m[i]);
            p.sells[i] = PM.min(p.sells[i], PM.min(b.token[i] * eq.stepBp / 10000, cap));
            p.buys[i] = PM.min(p.buys[i], PM.min(b.target[i] * eq.stepBp / 10000, cap));
            // Leave uneconomic dust unrequested rather than forcing a leg.
            if (p.sells[i] <= 2 * b.grain) p.sells[i] = 0;
            if (p.buys[i] <= 2 * b.grain) p.buys[i] = 0;
            selling += p.sells[i]; buying += p.buys[i];
        }
        // Purchases may not take the issue past the ceiling.
        int256 line = int256(b.ceiling) - b.issued + int256(selling);
        if (line <= 0) { for (uint256 i; i < n; ++i) p.buys[i] = 0; }
        else if (buying > uint256(line)) { for (uint256 i; i < n; ++i) p.buys[i] = p.buys[i] * uint256(line) / buying; }
    }

    function _execute(Snap memory s, Plan memory p, uint256 grain) private returns (uint256 moved) {
        // Each sale is in its own pool, so the opening snapshot prices them all. Wallet TOKEN goes
        // first; a position gives up TOKEN and BUCKs in equal value, so twice the rest is burned.
        bool sold;
        for (uint256 i; i < p.sells.length; ++i) {
            if (p.sells[i] == 0) continue;
            uint256 waiting = _tokenValue(s, i);
            if (!_sell(s, i, waiting >= p.sells[i] ? p.sells[i] : waiting + 2 * (p.sells[i] - waiting))) continue;
            pairPolicy.recordExecution(i);
            emit Responded(1, i, NONE, p.sells[i], 0);
            moved += p.sells[i]; sold = true;
        }
        if (sold) _markS(_snap());
        for (uint256 j; j < p.buys.length; ++j) {
            // Half buys the TOKEN, half is paired with it.
            uint256 budget = PM.min(2 * p.buys[j], _spendable());
            if (budget <= 2 * grain) continue;
            uint256 used = _buy(j, budget, false);
            if (used == 0) continue;
            _markS(_snap());
            pairPolicy.recordExecution(j);
            emit Responded(2, NONE, j, 0, used / 2);
            moved += used / 2;
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
        require(netMark, "net mark");
        pairPolicy.observe();
        Snap memory s = _snap();
        bool stale = _staleMark(s);
        _markS(s);
        int24[] memory beforeTicks = _spotTicks();
        uint256 moved = _placeAll(s);
        if (moved > 0) { s = _snap(); _markS(s); }
        Book memory b = _book(s);
        Plan memory p = _plan(s, b);
        pairPolicy.recordGaps(p.gaps);
        pairPolicy.recordRates(p.rates);
        uint256 traded = _execute(s, p, b.grain);
        if (traded > 0) _record(beforeTicks);
        if (moved + traded == 0) {
            if (!stale) return 0;
            emit Responded(0, NONE, NONE, 0, 0);
        }
        lastResponseAt = uint64(block.timestamp);
        emit WheelWork(2, 0, moved + traded);
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
