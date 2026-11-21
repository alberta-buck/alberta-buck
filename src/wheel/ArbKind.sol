// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {WorkWheel} from "./WorkWheel.sol";

/// @title ArbKind -- the consistency arbitrage's slots: one per registered route.
///
/// @notice (alberta-buck-ethereum-wheel.org, "The consistency cycle".)  A triangle through constituent
///         TOKEN: the basket's own TOKEN/BUCK pool, TOKEN/USDC and BUCK/USDC.  This kind keeps the
///         routes and the wheel's slots for them; a composed wheel plans and executes each cycle
///         (BasketMachineWheel: the basket's funded net-ROI arbitrage, BasketMarketOps).  The older
///         wheel's own flash-swap executor, its caller share and its start modes went with that wheel
///         (2026-11-20).
abstract contract ArbKind is WorkWheel {
    struct Triangle {
        address token;          // the constituent
        address poolOwn;        // TOKEN/BUCK, the basket's
        address poolUsdc;       // TOKEN/USDC
        uint256 basketIndex;    // the constituent's index in the basket
    }

    address public immutable arbBuck;
    address public immutable arbUsdc;
    address public poolUb;                  // BUCK/USDC
    address public creditee;                // the basket that runs and keeps each cycle
    uint16  public capBp = 100;             // max input, bp of the first leg's input reserve
    uint16  public minEdgeBp = 1;           // min profit, bp of the input
    Triangle[] public triangles;

    error NotSelf();

    constructor(address buck_, address usdc_) {
        arbBuck = buck_;
        arbUsdc = usdc_;
    }

    function setArb(address poolUb_, address creditee_, uint16 capBp_, uint16 minEdgeBp_)
        public virtual onlyGov
    {
        poolUb = poolUb_;
        creditee = creditee_;
        capBp = capBp_;
        minEdgeBp = minEdgeBp_;
    }

    function setTriangle(uint256 k, Triangle calldata t) external onlyGov {
        if (k == triangles.length) triangles.push(t);
        else triangles[k] = t;
    }

    function triangleCount() external view returns (uint256) { return triangles.length; }

    /// @dev The canonical machine includes cross-TOKEN cycles in this kind's
    /// range, so it may be placed before portfolio response in the C3 order.
    function _arbSlots() internal view virtual returns (uint256) { return triangles.length; }

    /// @notice The best cycle on route k now: (input, direction, expected gain); amount 0 when none.
    function plan(uint256 k) public view virtual returns (uint256 x, uint8 dir, uint256 profit);

    /// @notice Run route k's cycle.  Self-call only, so a cycle that misses its edge reverts alone
    ///         and the tick goes on.
    function arbCycle(uint256 k, uint8 dir, uint256 x) external virtual returns (uint256 profit);

    // --- the seam --------------------------------------------------------------- //

    function _slotCount() internal view virtual override returns (uint256) {
        return super._slotCount() + (poolUb == address(0) ? 0 : _arbSlots());
    }

    function _due(uint256 s) internal view virtual override returns (bool) {
        uint256 b = super._slotCount();
        if (s < b) return super._due(s);
        (uint256 x,,) = plan(s - b);
        return x > 0;
    }

    function _run(uint256 s) internal virtual override returns (uint256) {
        uint256 b = super._slotCount();
        if (s < b) return super._run(s);
        uint256 k = s - b;
        (uint256 x, uint8 dir,) = plan(k);
        if (x == 0) return 0;
        try this.arbCycle(k, dir, x) returns (uint256) {
            return 1;
        } catch {
            return 0;
        }
    }
}
