// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";
import {PortfolioMath as PM} from "../../src/basket/PortfolioMath.sol";
import {IBasketReference} from "../../src/basket/IBasketReference.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

/// @notice A trial fork of the upkeep, for a basket whose credit is marked
/// net (the shell's netMark): K bounds the BUCKs it has let out, not those
/// still held beside its TOKEN. Then nothing has to be sold to place a
/// deposit or to pay an exit, and the response reduces to three things.
///
/// 1. Pair every waiting TOKEN with issued BUCKs, at once. The BUCKs stay in
///    the position, so this uses none of the line.
/// 2. Size the book. The TOKEN it should hold is its equity plus the BUCKs it
///    aims to have issued. That aim leans against parity: when the basket
///    costs more than one BUCK, the BUCK is cheap and the aim is below zero
///    (hold BUCKs); when it costs less, above (issue them).
/// 3. Work each constituent toward its share of that book on its own: sell
///    one that is over, buy one that is under, each when the pair policy sees
///    it closing against the rest and at the policy's release. The two legs
///    need not meet in one turn: the line carries the difference. Issuance
///    beyond the ceiling, half of K x equity, is sold down at once.
///
/// The built facet's safeguard, liquidity target, placement rule and paired
/// transfer are not used. Its sell and buy primitives and its targets are.
contract BasketUpkeepNetTrial is BasketUpkeepFacet {
    uint256 private constant NO_ONE = type(uint256).max;
    IBasketReference public immutable parity;      // the basket's cost in BUCK, as last sampled
    uint256 public immutable parityGain;   // BUCKs to hold or issue per unit of parity error, as a share of equity, 1e18

    constructor(address policy, address parity_, uint256 parityGain_) BasketUpkeepFacet(policy) {
        parity = IBasketReference(parity_);
        parityGain = parityGain_;
    }

    struct Book {
        uint256[] token;   // each constituent's TOKEN, in its position and the wallet, in BUCK at the TWAP
        uint256[] target;  // what the book asks of it
        uint256 total;     // the TOKEN the book should hold
        int256 issued;     // BUCKs let out: the lien net of relief and of the BUCKs held in the positions
        uint256 ceiling;   // the most to have issued in normal running
        uint256 grain;
    }

    function _ticksNow() private view returns (int24[] memory ticks) {
        ticks = new int24[](constituents.length);
        for (uint256 i; i < ticks.length; ++i) (, ticks[i],,,,,) = IUniswapV3Pool(constituents[i].pool).slot0();
    }

    function _spendable() private view returns (uint256) { return _bk().balanceOf(address(this)); }

    function _respond() internal override returns (uint256) {
        pairPolicy.observe();
        Snap memory s = _snap();
        bool stale = _staleMark(s);
        _markS(s);
        int24[] memory before = _ticksNow();
        bool acted = _placeAll(s);
        if (acted) { s = _snap(); _markS(s); }
        Book memory b = _book(s);
        (uint256[] memory sells, uint256[] memory buys) = _plan(s, b);
        if (_execute(s, sells, buys, b.grain)) acted = true;
        int24[] memory afterTicks = _ticksNow();
        for (uint256 i; i < before.length; ++i)
            if (before[i] != afterTicks[i]) pairPolicy.recordImpact(i, before[i], afterTicks[i]);
        if (!acted && !stale) return 0;
        lastResponseAt = uint64(block.timestamp);
        emit WheelWork(2, 0, 0);
        return 1;
    }

    function _placeAll(Snap memory s) private returns (bool acted) {
        uint256 grain = _grainS(s);
        for (uint256 i; i < s.m.length; ++i) {
            uint256 budget = PM.min(_tokenValue(s, i), _spendable());
            if (s.m[i].depth == 0 || budget <= grain) continue;
            uint256 placed = _buy(i, budget, true);
            if (placed == 0) continue;
            // The BUCKs are still held: marking again gives the room back.
            _markS(_snap());
            emit Responded(4, NO_ONE, i, 0, placed);
            acted = true;
        }
    }

    function _book(Snap memory s) private view returns (Book memory b) {
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
        b.ceiling = s.k * uint256(equity) / 2e18;
        int256 aim = b.issued;
        if (parity.fresh()) {
            // Above zero the basket costs more than a BUCK: the BUCK is cheap.
            aim = -(parity.rawValue() - 1e18) * int256(parityGain) / 1e18 * equity / 1e18;
            if (aim > int256(b.ceiling)) aim = int256(b.ceiling);
            if (aim < -int256(b.ceiling)) aim = -int256(b.ceiling);
        }
        b.total = equity + aim > 0 ? uint256(equity + aim) : 0;
        b.target = _targets(s, b.total);
        b.grain = _grainS(s);
    }

    /// @dev How fast constituent i may be worked: the policy's rate for it
    /// against each of the others, weighed by their declared weights. Sold,
    /// it is the pair's seller; bought, its buyer.
    function _rate(uint256 i, bool sold, uint256 n) private view returns (uint256) {
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

    function _plan(Snap memory s, Book memory b) private returns (uint256[] memory sells, uint256[] memory buys) {
        uint256 n = s.m.length;
        sells = new uint256[](n); buys = new uint256[](n);
        uint256 tokens;
        int256[] memory gaps = new int256[](n);
        for (uint256 i; i < n; ++i) {
            tokens += b.token[i];
            if (b.total > 0) gaps[i] = (int256(b.token[i]) - int256(b.target[i])) * 1e18 / int256(b.total);
        }
        uint256 mask = pairPolicy.patience() != 0 ? pairPolicy.urgent(gaps) : type(uint256).max;
        pairPolicy.recordGaps(gaps);
        // Beyond the ceiling: every constituent gives its share, whatever the policy sees.
        if (b.issued > int256(b.ceiling) && tokens > 0)
            for (uint256 i; i < n; ++i) sells[i] = uint256(b.issued - int256(b.ceiling)) * b.token[i] / tokens;
        uint256[] memory rates = new uint256[](n);
        for (uint256 i; i < n; ++i) {
            if (s.m[i].depth == 0 || (mask >> i) & 1 == 0 || b.token[i] == b.target[i]) continue;
            bool over = b.token[i] > b.target[i];
            rates[i] = _rate(i, over, n);
            if (rates[i] == 0) continue;
            uint256 gap = over ? b.token[i] - b.target[i] : b.target[i] - b.token[i];
            uint256 amount = gap * pairPolicy.share(i, rates[i]) / 1e18;
            if (over) sells[i] += amount; else buys[i] = amount;
        }
        pairPolicy.recordRates(rates);
        uint256 selling;
        uint256 buying;
        for (uint256 i; i < n; ++i) {
            uint256 cap = _swapCap(s.m[i]);
            sells[i] = PM.min(sells[i], PM.min(b.token[i] * eq.stepBp / 10000, cap));
            buys[i] = PM.min(buys[i], PM.min(b.target[i] * eq.stepBp / 10000, cap));
            if (sells[i] <= 2 * b.grain) sells[i] = 0;
            if (buys[i] <= 2 * b.grain) buys[i] = 0;
            selling += sells[i]; buying += buys[i];
        }
        // Purchases may not take the issue past the ceiling.
        int256 line = int256(b.ceiling) - b.issued + int256(selling);
        if (line <= 0) { for (uint256 i; i < n; ++i) buys[i] = 0; }
        else if (buying > uint256(line)) { for (uint256 i; i < n; ++i) buys[i] = buys[i] * uint256(line) / buying; }
    }

    function _execute(Snap memory s, uint256[] memory sells, uint256[] memory buys, uint256 grain)
        private returns (bool acted)
    {
        // Each sale is in its own pool, so the opening snapshot prices them all. A position gives
        // up TOKEN and BUCKs in equal value, so twice the TOKEN's value of it is burned.
        for (uint256 i; i < sells.length; ++i) {
            if (sells[i] == 0 || !_sell(s, i, 2 * sells[i])) continue;
            pairPolicy.recordExecution(i);
            emit Responded(1, i, NO_ONE, sells[i], 0);
            acted = true;
        }
        if (acted) _markS(_snap());
        for (uint256 j; j < buys.length; ++j) {
            // Half buys the TOKEN, half is paired with it.
            uint256 budget = PM.min(2 * buys[j], _spendable());
            if (budget <= 2 * grain) continue;
            uint256 used = _buy(j, budget, false);
            if (used == 0) continue;
            _markS(_snap());
            pairPolicy.recordExecution(j);
            emit Responded(2, NO_ONE, j, 0, used / 2);
            acted = true;
        }
    }
}
