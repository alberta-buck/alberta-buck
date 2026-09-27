// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {WorkWheel} from "./WorkWheel.sol";

/// @notice The permissionless upkeep the keepers do today, as wheel kinds
///         (doc/BASKET-WHEEL.org 8.1).  Each claims one slot after its bases'
///         when its target is set (zero slots when unset: a kind is enabled by
///         configuration) and hands every other slot to `super`.  They capture
///         nothing; the reserve pays for them.

interface IWheelController {
    function compute() external returns (uint256);
    function lastUpdate() external view returns (uint256);
    function dT() external view returns (uint256);
}

interface IWheelDirector {
    function pending() external view returns (uint256);
    function poke(uint256 maxWork) external returns (uint256);
}

interface IWheelTreasury {
    function treasuryBuckPending() external view returns (uint256);
    function sweepTreasury() external;
}

interface IWheelOps {
    function monetaryOperation() external returns (uint8);
}

interface IWheelEpoch {
    function epochNow() external view returns (uint32);
}

/// @title ComputeKind -- the controller's PID cycle, when its interval is up.
abstract contract ComputeKind is WorkWheel {
    address public controller;

    function setController(address c) external onlyGov { controller = c; }

    function _slotCount() internal view virtual override returns (uint256) {
        return super._slotCount() + (controller == address(0) ? 0 : 1);
    }

    function _due(uint256 s) internal view virtual override returns (bool) {
        uint256 b = super._slotCount();
        if (s < b) return super._due(s);
        IWheelController c = IWheelController(controller);
        return block.timestamp >= c.lastUpdate() + c.dT();
    }

    function _run(uint256 s) internal virtual override returns (uint256) {
        uint256 b = super._slotCount();
        if (s < b) return super._run(s);
        try IWheelController(controller).compute() { return 1; } catch { return 0; }
    }
}

/// @title DirectorKind -- one stale constituent's signal refresh per run.
abstract contract DirectorKind is WorkWheel {
    address public director;

    function setDirector(address d) external onlyGov { director = d; }

    function _slotCount() internal view virtual override returns (uint256) {
        return super._slotCount() + (director == address(0) ? 0 : 1);
    }

    function _due(uint256 s) internal view virtual override returns (bool) {
        uint256 b = super._slotCount();
        if (s < b) return super._due(s);
        try IWheelDirector(director).pending() returns (uint256 p) { return p > 0; }
        catch { return false; }
    }

    function _run(uint256 s) internal virtual override returns (uint256) {
        uint256 b = super._slotCount();
        if (s < b) return super._run(s);
        try IWheelDirector(director).poke(1) returns (uint256 a) { return a; }
        catch { return 0; }
    }
}

/// @title SweepKind -- re-LP the basket treasury's pending BUCK.
abstract contract SweepKind is WorkWheel {
    address public treasuryBasket;
    uint256 public sweepMin = 1e15;         // the shell's MIN_REINVEST_BUCK

    function setSweep(address basket, uint256 minBuck) external onlyGov {
        treasuryBasket = basket;
        sweepMin = minBuck;
    }

    function _slotCount() internal view virtual override returns (uint256) {
        return super._slotCount() + (treasuryBasket == address(0) ? 0 : 1);
    }

    function _due(uint256 s) internal view virtual override returns (bool) {
        uint256 b = super._slotCount();
        if (s < b) return super._due(s);
        try IWheelTreasury(treasuryBasket).treasuryBuckPending() returns (uint256 p) {
            return p >= sweepMin;
        } catch { return false; }
    }

    function _run(uint256 s) internal virtual override returns (uint256) {
        uint256 b = super._slotCount();
        if (s < b) return super._run(s);
        try IWheelTreasury(treasuryBasket).sweepTreasury() { return 1; } catch { return 0; }
    }
}

/// @title OpsKind -- the desk's quadrant operation, once per monetary epoch.
///        The shell guards the epoch itself; the kind keeps its own memo so a
///        refused or already-done epoch is not rescanned.
abstract contract OpsKind is WorkWheel {
    address public opsBasket;
    address public opsDirector;             // the epoch clock the shell uses
    uint32  public opsEpochSeen;            // 1 + the last epoch attempted

    function setOps(address basket, address epochSource) external onlyGov {
        opsBasket = basket;
        opsDirector = epochSource;
    }

    function _slotCount() internal view virtual override returns (uint256) {
        return super._slotCount() + (opsBasket == address(0) ? 0 : 1);
    }

    function _due(uint256 s) internal view virtual override returns (bool) {
        uint256 b = super._slotCount();
        if (s < b) return super._due(s);
        try IWheelEpoch(opsDirector).epochNow() returns (uint32 e) {
            return opsEpochSeen != e + 1;
        } catch { return false; }
    }

    function _run(uint256 s) internal virtual override returns (uint256) {
        uint256 b = super._slotCount();
        if (s < b) return super._run(s);
        opsEpochSeen = IWheelEpoch(opsDirector).epochNow() + 1;
        try IWheelOps(opsBasket).monetaryOperation() { return 1; } catch { return 0; }
    }
}
