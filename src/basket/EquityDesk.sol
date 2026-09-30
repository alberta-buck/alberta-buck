// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBuckKController}   from "../IBuckKController.sol";
import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";
import {IBuckMintBurn}      from "./BuckBasketStorage.sol";
import {IBuckHolder, IMarkedCredit} from "./BuckBasketEquityStorage.sol";
import {MonetaryDesk}       from "./MonetaryDesk.sol";

/// @notice What the desk reads from the equity basket it runs beside.
interface IEquityBasketView {
    function equity() external view returns (uint256);
    function twapWindow() external view returns (uint32);
    function constituentsLength() external view returns (uint256);
    function constituents(uint256 i) external view returns (
        address token, uint8 decimals, uint256 basketAmount, uint256 initialPriceInBuck,
        uint24 feeTier, address pool, int24 tickLower, int24 tickUpper, bool buckIsToken0,
        uint256 targetWeightBp, uint128 treasuryLiquidity);
}

/// @title EquityDesk -- the monetary desk as its own BUCK credit holder,
///        beside the equity basket.
///
/// @notice The desk (MonetaryDesk: the four quadrants, the bounds, the
///         stabilizer seam; alberta-buck-operations.org) runs beside a
///         BuckBasketEquity and shares nothing with it but the pools it
///         trades in:
///
///           * its own address, so its own account at Buck: its own signed
///             balance, lien, relief, and limit;
///           * its own self-issued MARKED BuckCredit, marked at its own net
///             value -- its TOKEN at the pools' low marks, plus its account and
///             the relief accrued on it -- so Buck holds its issuance within
///             K x the desk's value and the basket's within K x the
///             depositors' equity, each account separately;
///           * its own capital: the founding grant (`capitalizeMonetary`) is
///             the desk's TOKEN, collateral for the desk alone;
///           * its own invoker: `monetaryOperation`, called by its own work
///             wheel (the ops kind, pointed at this contract) or a keeper.
///
///         From the basket it reads only two things: the basket's equity,
///         which sizes the desk's bounds (as the shell's NAV sized them when
///         the desk was a mixin), and its constituents, mirrored here so the
///         desk trades in the same pools through the same venue facet (this
///         contract's fallback delegatecalls to it, as a basket shell does).
///
///         Invariants:
///           D1  the desk spends only after a mark at its own net value, so
///               its lien stays within K x that value (Buck enforces it)
///           D2  nothing the desk holds or owes enters the basket's books,
///               mark or limit: they are two accounts
///           D3  relief on the desk's lien is the desk's, and melts its
///               outstanding issuance
contract EquityDesk is MonetaryDesk {

    IEquityBasketView public basket;
    IMarkedCredit     public credit;
    uint256           public creditId;
    bool              public creditLive;

    uint8 internal constant DEP_MARKED = 3;              // BuckCredit.DepreciationType.MARKED

    event CreditOpened(address indexed credit, uint256 indexed tokenId, uint256 face);
    event DeskMarked(uint256 value);

    constructor(address _buck, address _controller, address _basket, address _governance) {
        buck       = IBuckMintBurn(_buck);
        controller = IBuckKController(_controller);
        basket     = IEquityBasketView(_basket);
        governance = _governance;
    }

    // --- Wiring (governance) ------------------------------------------------ //

    function setVenue(address v) external onlyGov {
        venue = IBuckBasketVenue(v);
        emit VenueSet(v);
    }

    /// @notice Mirror the basket's constituents (the same tokens and pools)
    ///         and its TWAP window; appends any the basket added since.
    function mirrorConstituents() external onlyGov {
        twapWindow = basket.twapWindow();
        uint256 n = basket.constituentsLength();
        for (uint256 i = constituents.length; i < n; i++) {
            (address token, uint8 dec, uint256 amt, uint256 p0, uint24 fee, address pool,
             int24 lo, int24 hi, bool b0, uint256 w,) = basket.constituents(i);
            constituents.push(Constituent(token, dec, amt, p0, fee, pool, lo, hi, b0, w, 0));
            indexOf[token] = i + 1;
        }
    }

    /// @notice Open the desk's own MARKED credit, the desk its own insurer
    ///         (zero premium); activated by its first mark above zero.
    function openCredit(address credit_, uint256 face) external onlyGov {
        if (address(credit) != address(0)) revert AlreadyPresent();
        IMarkedCredit c = IMarkedCredit(credit_);
        c.setCreditIssuer(address(this), true);
        creditId = c.createCredit(address(this), 0, face, 0, DEP_MARKED, 0, 0, 0);
        credit = c;
        emit CreditOpened(credit_, creditId, face);
    }

    // --- The desk's own value, and its mark ------------------------------------ //

    function _bh() internal view returns (IBuckHolder) {
        return IBuckHolder(address(buck));
    }

    function _venueMarks(uint256 i) internal view returns (IBuckBasketVenue.Marks memory) {
        return IBuckBasketVenue(address(this)).marks(i, 0);
    }

    /// @notice The desk's net value in BUCK at the pools' low marks: its TOKEN,
    ///         plus its account at Buck (negative: its lien) and the relief
    ///         accrued on it.  What its credit is marked at (D1).
    function netValue() public view returns (uint256) {
        int256 v = int256(_bh().signedBalanceOf(address(this)))
                 + int256(_bh().reliefOf(address(this)));
        for (uint256 i = 0; i < constituents.length; i++) {
            uint256 held = monetaryTokenHeld[i];
            if (held == 0) continue;
            v += int256(held * _venueMarks(i).pLow / (10 ** constituents[i].decimals));
        }
        return v > 0 ? uint256(v) : 0;
    }

    function markNow() external view returns (uint256) {
        return address(credit) == address(0) ? 0 : credit.markOf(creditId);
    }

    /// @dev Mark the credit at the desk's net value, activating it the first
    ///      time the value is above zero.
    function _mark() internal {
        IMarkedCredit c = credit;
        if (address(c) == address(0)) return;
        uint256 value = netValue();
        if (c.markOf(creditId) != value) {
            c.mark(creditId, value);
            emit DeskMarked(value);
        }
        if (!creditLive && value > 0) {
            // One mint for all the credit can give activates its whole face:
            // zero premium, so no deposit.
            uint256[] memory ids = new uint256[](1);
            ids[0] = creditId;
            _bh().mint(type(uint256).max, ids);
            creditLive = true;
        }
    }

    /// @dev Relief paid into the desk's account melts its outstanding issuance
    ///      (D3): collected before each operation.
    function _collectRelief() internal {
        int256 s0 = _bh().signedBalanceOf(address(this));
        if (s0 >= 0 || _bh().reliefOf(address(this)) == 0) return;
        _bh().settleRelief();
        int256 paid = _bh().signedBalanceOf(address(this)) - s0;
        if (paid <= 0) return;
        int256 o = monetaryOutstanding;
        monetaryOutstanding = o > paid ? o - paid : (o > 0 ? int256(0) : o);
    }

    // --- MonetaryDesk's hooks -------------------------------------------------- //

    /// @dev The bounds are sized in the basket's equity, as they were sized in
    ///      the shell's NAV when the desk was a mixin.
    function _deskNav() internal view override returns (uint256 nav) {
        nav = basket.equity();
        if (nav == 0) revert NoValue();
    }

    function _deskNavSafe() internal view override returns (uint256) {
        try basket.equity() returns (uint256 e) { return e; } catch { return 0; }
    }

    function _deskPrices() internal view override returns (uint256[] memory prices) {
        uint256 n = constituents.length;
        prices = new uint256[](n);
        for (uint256 i = 0; i < n; i++) prices[i] = _venueMarks(i).pTwap;
    }

    /// @dev Issue by spending the desk's own credit: at most what its account
    ///      can spend after a mark (D1).
    function _deskIssue(uint256 size) internal override returns (uint256) {
        _mark();
        uint256 room = _bh().balanceOf(address(this));
        return size < room ? size : room;
    }

    /// @dev Nothing to retire: BUCK the desk receives repay its lien.
    function _deskRetire(uint256) internal override {}

    /// @dev Every operation collects the desk's relief and marks before it
    ///      trades; BUCK bought arrive carrying their age and, into an account
    ///      below zero, pay its fee on arrival -- book what arrived.
    function _swapAcross(bool sellBuck, uint256 sizeBuck, OpsParams memory op)
        internal override returns (uint256 amount)
    {
        _collectRelief();
        _mark();
        int256 s0 = _bh().signedBalanceOf(address(this));
        amount = super._swapAcross(sellBuck, sizeBuck, op);
        if (!sellBuck) {
            int256 d = _bh().signedBalanceOf(address(this)) - s0;
            amount = d > 0 ? uint256(d) : 0;
        }
    }

    // --- Dispatch: the venue facet (its swaps, callbacks and marks) ------------- //

    fallback() external {
        address v = address(venue);
        if (v == address(0)) revert VenueUnset();
        assembly {
            calldatacopy(0, 0, calldatasize())
            let ok := delegatecall(gas(), v, 0, calldatasize(), 0, 0)
            returndatacopy(0, 0, returndatasize())
            switch ok
            case 0 { revert(0, returndatasize()) }
            default { return(0, returndatasize()) }
        }
    }
}
