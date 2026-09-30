// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquity}    from "./BuckBasketEquity.sol";
import {MonetaryDesk}        from "./MonetaryDesk.sol";

/// @title BuckBasketEquityOps -- the equity basket plus the monetary desk.
///
/// @notice The monetary desk (MonetaryDesk; alberta-buck-operations.org) on
///         the equity shell (alberta-buck-ethereum.org, "BuckBasketEquity: the Basket as a Credit Holder", "The Desk as a Sub-Account"): NAV is the
///         equity at the TWAP marks, prices the TWAP.
///
///         The basket's BUCK is one signed account at Buck, so the desk keeps
///         its BUCK as a sub-account of explicit counters rather than as a
///         balance: `monetaryBuckHeld` (bought and held -- on arrival those
///         BUCK repaid the lien) and `monetaryOutstanding` (issued, net of
///         retired -- selling spent the credit).  Equity's BUCK is the account
///         less that net position, so the desk's book stays outside every
///         receipt's claim, as it did on the pro-rata shell.  Its TOKEN is
///         plain balances, which the equity books (explicit counters) never
///         count.
///
///         Its issuance is the basket's credit: bounded by what the account
///         can spend beyond the liquidity target, and backed in the mark by
///         the desk's own book.  The relief the desk's share of the lien
///         earns melts its outstanding issuance.
contract BuckBasketEquityOps is BuckBasketEquity, MonetaryDesk {

    constructor(
        address _buck,
        address _controller,
        address _v3Factory,
        address _governance,
        uint24  _defaultFeeTier,
        uint32  _twapWindow,
        uint16  _observationCardinality,
        uint256 _defaultMaxDeviationBp,
        uint256 _minSeedLiquidity
    ) BuckBasketEquity(_buck, _controller, _v3Factory, _governance,
                       _defaultFeeTier, _twapWindow, _observationCardinality,
                       _defaultMaxDeviationBp, _minSeedLiquidity) {}

    function _deskNav() internal view override returns (uint256 nav) {
        nav = _equity(MARK_TWAP);
        if (nav == 0) revert NoValue();
    }

    function _deskNavSafe() internal view override returns (uint256) {
        return _equity(MARK_TWAP);
    }

    function _deskPrices() internal view override returns (uint256[] memory prices) {
        uint256 n = constituents.length;
        prices = new uint256[](n);
        for (uint256 i = 0; i < n; i++) prices[i] = _marks(i).pTwap;
    }

    // --- The desk as a sub-account of the basket's credit ------------------- //

    // The desk's own relief: Buck's rate on the desk's net issuance, integrated
    // like any lien and folded whenever that issuance changes (every desk
    // swap), so the desk earns exactly the relief its own issuance accrued and
    // equity the rest -- however the split of the lien moves in between.
    uint256 internal constant JUB_SCALE = 1e27;
    uint256 internal constant JUB_RATE  = uint256(2e25) / (365 days + 6 hours);   // Buck's BASE_RATE_PER_SEC
    uint256 internal _deskIssuanceSeconds;
    uint64  internal _deskFoldedAt;

    /// @dev The desk's net issuance: what it sold beyond what it holds.
    function _deskIssued() internal view returns (uint256) {
        int256 net = monetaryOutstanding - int256(monetaryBuckHeld);
        return net > 0 ? uint256(net) : 0;
    }

    function _deskSecondsLive() internal view returns (uint256) {
        return _deskIssuanceSeconds + _deskIssued() * (block.timestamp - uint256(_deskFoldedAt));
    }

    function _deskFold() internal {
        _deskIssuanceSeconds = _deskSecondsLive();
        _deskFoldedAt = uint64(block.timestamp);
    }

    function deskPosition(uint256 relief, uint256)
        external view override returns (int256 b, int256 value)
    {
        uint256 own = _deskSecondsLive() * JUB_RATE / JUB_SCALE;
        if (own > relief) own = relief;                  // never more than the account accrued
        b = int256(monetaryBuckHeld) - monetaryOutstanding + int256(own);
        value = int256(monetaryTokenValue()) + b;
    }

    /// @dev Relief just paid into the account: the desk's own accrual (capped
    ///      by what was paid) melts its outstanding issuance.
    function deskRelief(uint256 relief, uint256) external override {
        if (msg.sender != address(this)) revert NotSelf();
        uint256 own = _deskSecondsLive() * JUB_RATE / JUB_SCALE;
        if (own > relief) own = relief;
        monetaryOutstanding -= int256(own);
        _deskIssuanceSeconds = 0;
        _deskFoldedAt = uint64(block.timestamp);
    }

    /// @dev Issue by spending the basket's credit: at most what it can spend
    ///      beyond the liquidity target, so the desk never takes the exits'
    ///      liquidity.
    function _deskIssue(uint256 size) internal override returns (uint256) {
        Snap memory s = _snap();
        _markS(s);
        uint256 room = _usableS(s);
        return size < room ? size : room;
    }

    /// @dev Nothing to retire: BUCK the basket receives repay its lien.
    function _deskRetire(uint256) internal override {}

    /// @dev BUCK the desk buys arrive carrying their age and, into an account
    ///      below zero, pay its fee on arrival: book what arrived, so the fee
    ///      is the desk's, not equity's.
    function _swapAcross(bool sellBuck, uint256 sizeBuck, OpsParams memory op)
        internal override returns (uint256 amount)
    {
        _deskFold();                                     // its issuance may change
        int256 s0 = _bk().signedBalanceOf(address(this));
        amount = super._swapAcross(sellBuck, sizeBuck, op);
        if (!sellBuck) {
            int256 d = _bk().signedBalanceOf(address(this)) - s0;
            amount = d > 0 ? uint256(d) : 0;
        }
    }
}
