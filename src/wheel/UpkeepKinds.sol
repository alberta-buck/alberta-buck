// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {WorkWheel} from "./WorkWheel.sol";

/// @notice The permissionless upkeep the keepers do today, as wheel kinds
///         (alberta-buck-ethereum-wheel.org, "The work wheel").  Each claims one slot after its bases'
///         when its target is set (zero slots when unset: a kind is enabled by
///         configuration) and hands every other slot to `super`.  They capture
///         nothing; the reserve pays for them.

interface IWheelController {
    function compute() external returns (uint256);
    function lastUpdate() external view returns (uint256);
    function dT() external view returns (uint256);
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

/// @title OpsKind -- the desk's quadrant operation, once per monetary epoch.
///        The shell guards the epoch itself; the kind keeps its own memo so a
///        successful epoch is not rescanned. A refusal can be retried when
///        a sample or the market changes in the same epoch.
abstract contract OpsKind is WorkWheel {
    address public opsBasket;
    address public opsDirector;             // the epoch clock the shell uses
    uint32  public opsEpochSeen;            // 1 + the last successful epoch
    uint256 public opsAttemptBlock;

    function setOps(address basket, address epochSource) external onlyGov {
        opsBasket = basket;
        opsDirector = epochSource;
        opsEpochSeen = 0;
        opsAttemptBlock = 0;
        _markDirty();
    }

    function _slotCount() internal view virtual override returns (uint256) {
        return super._slotCount() + (opsBasket == address(0) ? 0 : 1);
    }

    /// @dev A composed wheel may publish the observation dependency here.
    ///      It survives a caught refusal by the desk (unlike a desk self-call).
    function _prepareOps() internal virtual {}

    /// @dev Whether slot `s` is the desk's operation (a composed wheel refines its view and memory).
    function _isOpsSlot(uint256 s) internal view returns (bool) {
        return opsBasket != address(0) && s == super._slotCount();
    }

    function _due(uint256 s) internal view virtual override returns (bool) {
        uint256 b = super._slotCount();
        if (s < b) return super._due(s);
        try IWheelEpoch(opsDirector).epochNow() returns (uint32 e) {
            return opsEpochSeen != e + 1 && opsAttemptBlock != block.number;
        } catch { return false; }
    }

    function _run(uint256 s) internal virtual override returns (uint256) {
        uint256 b = super._slotCount();
        if (s < b) return super._run(s);
        opsAttemptBlock = block.number;
        _prepareOps();
        uint32 epoch = IWheelEpoch(opsDirector).epochNow();
        try IWheelOps(opsBasket).monetaryOperation() returns (uint8 action) {
            if (action == 0) return 0;
            opsEpochSeen = epoch + 1;
            return 1;
        } catch { return 0; }
    }
}
