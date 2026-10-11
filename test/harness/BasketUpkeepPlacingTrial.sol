// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";
import {PortfolioMath as PM} from "../../src/basket/PortfolioMath.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

/// @notice A trial, for the simulator only: place waiting TOKEN the account
/// has no BUCKs to pair. The built facet pairs wallet TOKEN with what the
/// account can spare beyond its liquidity target and leaves the rest waiting.
/// This one then places what is left the way the older upkeep did: it sells
/// half of a constituent's waiting TOKEN into its own pool and pairs the other
/// half with the proceeds. Nothing is drawn, and no constituent's value in
/// the book moves. It does so only on a turn that made no transfer, never
/// while the book needs cash, and never in a pool the turn has already traded.
contract BasketUpkeepPlacingTrial is BasketUpkeepFacet {
    constructor(address policy) BasketUpkeepFacet(policy) {}

    function _ticks() private view returns (int24[] memory ticks) {
        ticks = new int24[](constituents.length);
        for (uint256 i; i < ticks.length; ++i) (, ticks[i],,,,,) = IUniswapV3Pool(constituents[i].pool).slot0();
    }

    function _respond() internal override returns (uint256 work) {
        int24[] memory before = _ticks();
        work = super._respond();
        if (pairPolicy.lastExecutionAt() == block.timestamp) return work;
        Snap memory s = _snap();
        uint256 grain = _grainS(s);
        if (s.lien > s.k * _equityS(s, MARK_LOW) / 1e18 || s.spend + grain < _targetS(s) * (10000 - eq.bandBp) / 10000)
            return work;
        int24[] memory now_ = _ticks();
        bool placed;
        for (uint256 i; i < before.length; ++i) {
            if (s.m[i].depth == 0 || before[i] != now_[i]) continue;
            uint256 half = PM.min(_tokenValue(s, i) / 2, _swapCap(s.m[i]));
            if (half <= 2 * grain) continue;
            uint256 spend = _bk().balanceOf(address(this));
            if (!_sell(s, i, half)) continue;
            uint256 spendNow = _bk().balanceOf(address(this));
            uint256 proceeds = spendNow > spend ? spendNow - spend : 0;
            uint256 paired = proceeds > 0 ? _buy(i, proceeds, true) : 0;
            emit Responded(6, i, i, proceeds, paired);
            placed = true;
        }
        if (!placed) return work;
        _markS(_snap());
        lastResponseAt = uint64(block.timestamp);
        return 1;
    }
}
