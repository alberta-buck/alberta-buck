// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IStabilizer} from "../basket/IStabilizer.sol";

/// @title SimStabilizer -- a sim-only level-1 stabilizer with setters
///        (WAVE3.org decision 17; WP-14).
///
/// @notice One instance per agent CLASS of the Python stand-ins -- the
///         undertakings' strong side (issued), their weak side (absorbed),
///         the facility population (drawn lines), the seeder (its converted
///         range) -- so that the observer's per-stabilizer caps, S lambdas
///         and V cost weights of decision 11 (desk 1, undertakings 1 per
///         side, facility 0.5, seeder 0.25) mean what they say in a cell.
///         Each stand-in books its net inventory (absorbed POSITIVE, issued
///         NEGATIVE, BUCK native units) and its REAL cap -- the undertakings'
///         reserve_frac x NAV per side, the facility's vetted limits x
///         max_frac, the seeder's funded amount -- through `setBookAndCap`,
///         which `alberta_buck/sim/shadow_book.py` sends only on change.
///         Replaces the observer's ONE lumped `shadowOffset` (WP-3a /
///         WP-13), which summed three books under one cap ($10M by default,
///         not any class's real bound: WP-16 read a full undertakings book
///         as a fill of 0.76 at depth 10 and 0.20 at depth 40).  Removed
///         once the books are contract-level (WP-11).
///
///         `positionCap()` has IStabilizer's three outcomes, so the
///         observer's hold-last-cap policy (decision 9) is exercised
///         against it exactly as against the desk:
///
///           * the cap when set (the observer refreshes its held cap);
///           * ZERO when the class is disabled -- nothing booked yet, or
///             the class absent from the cell -- so the observer EXCLUDES
///             it and renormalizes the V weights without it;
///           * a REVERT (`CapUnreadable`) while `capReverts` is set, the
///             emulation of an unreadable NAV: the observer keeps its held
///             cap and flags the stabilizer STALE; `netInventory()` stays
///             readable, as the seam requires.
///
///         `capacity()` / `saturation()` are the seam's WP-3a views from
///         the same book and cap (1e18 = all the room / at a bound; a
///         disabled stabilizer reports capacity 0, saturation 1e18, like
///         the disabled desk).
contract SimStabilizer is IStabilizer {

    address public governance;
    bytes32 public immutable tag;      // the class: "uts", "utw", "fac", "sd"
    int256  public book;               // netInventory, BUCK native units
    uint256 public cap;                // positionCap, BUCK native; 0 = disabled
    bool    public capReverts;         // emulate an unreadable NAV

    uint256 internal constant UNIT = 1e18;

    event GovernanceSet(address indexed governance);
    event BookSet(int256 book, uint256 cap);
    event CapRevertsSet(bool reverts);

    error NotGovernance();
    error Gov0();
    error CapUnreadable();

    modifier onlyGov() {
        if (msg.sender != governance) revert NotGovernance();
        _;
    }

    constructor(address _governance, bytes32 _tag) {
        if (_governance == address(0)) revert Gov0();
        governance = _governance;
        tag = _tag;
    }

    function setGovernance(address _governance) external onlyGov {
        if (_governance == address(0)) revert Gov0();
        governance = _governance;
        emit GovernanceSet(_governance);
    }

    /// @notice Book the class's net inventory (absorbed positive, issued
    ///         negative, BUCK native units).
    function setBook(int256 q) external onlyGov {
        book = q;
        emit BookSet(q, cap);
    }

    /// @notice Set the class's inventory bound (BUCK native units); 0
    ///         disables the stabilizer (the observer excludes it).
    function setCap(uint256 c) external onlyGov {
        cap = c;
        emit BookSet(book, c);
    }

    /// @notice Book and cap in one transaction -- what the sim sends.
    function setBookAndCap(int256 q, uint256 c) external onlyGov {
        book = q;
        cap = c;
        emit BookSet(q, c);
    }

    /// @notice Sim-only: make `positionCap()` revert (an unreadable NAV).
    function setCapReverts(bool r) external onlyGov {
        capReverts = r;
        emit CapRevertsSet(r);
    }

    // --- IStabilizer -------------------------------------------------------- //

    function netInventory() external view override returns (int256) {
        return book;
    }

    function positionCap() external view override returns (uint256) {
        if (capReverts) revert CapUnreadable();
        return cap;
    }

    /// @notice 1e18 - min(1, |book| / cap); 0 when disabled.
    function capacity() public view override returns (uint256) {
        if (cap == 0) return 0;
        uint256 f = _absFill();
        return f >= UNIT ? 0 : UNIT - f;
    }

    /// @notice 1e18 - capacity(): the degree at the bound; 1e18 when
    ///         disabled (it truly cannot act), like the desk.
    function saturation() external view override returns (uint256) {
        return UNIT - capacity();
    }

    /// @dev |book| * 1e18 / cap (unclamped).
    function _absFill() internal view returns (uint256) {
        uint256 a = book < 0 ? uint256(-book) : uint256(book);
        return a * UNIT / cap;
    }
}
