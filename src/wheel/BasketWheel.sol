// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {WorkWheel} from "./WorkWheel.sol";
import {ComputeKind, DirectorKind, SweepKind, OpsKind} from "./UpkeepKinds.sol";
import {ArbKind} from "./ArbKind.sol";

/// @title BasketWheel -- a BuckBasket's work wheel, deployed beside its shell.
///
/// @notice doc/BASKET-WHEEL.org 8.4.  The chassis plus every kind the basket
///         has today; the order of the bases IS the slot layout (the
///         controller's compute, the director's poke, the treasury sweep, the
///         desk's quadrant operation, then one slot per arbitrage triangle).
///         A kind whose target is unset has no slots, so one contract serves
///         a savings basket (compute + director + sweep + arb) and a monetary
///         one (plus ops) by configuration.  The shell grants it one thing:
///         =setWheel=, which lets its arbitrage credit the depositors (TOKEN)
///         and the treasury (BUCK).  Every other entry point it calls is
///         permissionless already.
contract BasketWheel is WorkWheel, ComputeKind, DirectorKind, SweepKind, OpsKind, ArbKind {
    constructor(address buck_, address usdc_, address governance_,
                uint16 kappaBp_, uint256 reserveCap_)
        WorkWheel(buck_, governance_, kappaBp_, reserveCap_)
        ArbKind(buck_, usdc_)
    {}

    function _slotCount() internal view
        override(WorkWheel, ComputeKind, DirectorKind, SweepKind, OpsKind, ArbKind)
        returns (uint256)
    {
        return super._slotCount();
    }

    function _due(uint256 s) internal view
        override(WorkWheel, ComputeKind, DirectorKind, SweepKind, OpsKind, ArbKind)
        returns (bool)
    {
        return super._due(s);
    }

    function _run(uint256 s) internal
        override(WorkWheel, ComputeKind, DirectorKind, SweepKind, OpsKind, ArbKind)
        returns (uint256)
    {
        return super._run(s);
    }
}
