// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {WorkWheel} from "./WorkWheel.sol";
import {ComputeKind, OpsKind} from "./UpkeepKinds.sol";
import {IBasketUpkeep} from "../basket/IBasketUpkeep.sol";
import {IBasketView} from "../basket/IBasketView.sol";
import {ArbKind} from "./ArbKind.sol";

interface IWheelSample {
    function isDue() external view returns (bool);
    function lastSampleAt() external view returns (uint64);
    function observe() external;
}

abstract contract ResponseKind is WorkWheel {
    address public equityBasket;
    function setEquity(address basket) external onlyGov { equityBasket = basket; _markDirty(); }

    function _equitySlots() private view returns (uint256) {
        if (equityBasket == address(0)) return 0;
        try IBasketView(equityBasket).constituentsLength() returns (uint256 n) { return n + 2; }
        catch { return 0; }
    }

    function _slotCount() internal view virtual override returns (uint256) {
        return super._slotCount() + _equitySlots();
    }

    function _component(uint256 j) private view returns (uint8 kind, uint256 i) {
        if (j == 0) return (0, 0);
        if (j == _equitySlots() - 1) return (2, 0);
        return (1, j - 1);
    }

    function _due(uint256 s) internal view virtual override returns (bool) {
        uint256 base = super._slotCount();
        if (s < base) return super._due(s);
        (uint8 kind, uint256 i) = _component(s - base);
        try IBasketUpkeep(equityBasket).wheelDue(kind, i) returns (bool due) { return due; }
        catch { return false; }
    }

    function _run(uint256 s) internal virtual override returns (uint256) {
        uint256 base = super._slotCount();
        if (s < base) return super._run(s);
        (uint8 kind, uint256 i) = _component(s - base);
        try IBasketUpkeep(equityBasket).wheelStep(kind, i) returns (uint256 work) { return work; }
        catch { return 0; }
    }
}

/// @notice Last in the wheel's slot order. No reward for a failed observation
///         or for calling an already published sample again.
abstract contract SampleKind is WorkWheel {
    address public sampleSource;
    function setSample(address source) external onlyGov { sampleSource = source; _markDirty(); }

    function _slotCount() internal view virtual override returns (uint256) {
        return super._slotCount() + (sampleSource == address(0) ? 0 : 1);
    }

    function _due(uint256 s) internal view virtual override returns (bool) {
        uint256 base = super._slotCount();
        if (s < base) return super._due(s);
        try IWheelSample(sampleSource).isDue() returns (bool due) { return due; }
        catch { return false; }
    }

    function _run(uint256 s) internal virtual override returns (uint256) {
        uint256 base = super._slotCount();
        if (s < base) return super._run(s);
        IWheelSample source = IWheelSample(sampleSource);
        // All target reads belong inside a catch boundary too: a broken
        // service must not trap the cursor before the other kinds can run.
        try source.lastSampleAt() returns (uint64 beforeSample) {
            try source.observe() {
                try source.lastSampleAt() returns (uint64 afterSample) {
                    return afterSample > beforeSample ? 1 : 0;
                } catch { return 0; }
            } catch { return 0; }
        } catch { return 0; }
    }
}

/// @notice Replacement wheel: Compute, Ops, N+2 basket components, Arb,
///         Sample. BasketWheel retains its old slot ABI for comparison runs.
contract BasketMachineWheel is WorkWheel, ComputeKind, OpsKind, ResponseKind, ArbKind, SampleKind {
    constructor(address buck, address stable, address gov, uint16 kappa, uint256 reserveCap)
        WorkWheel(buck, gov, kappa, reserveCap) ArbKind(buck, stable) {}

    function _prepareOps() internal override {
        if (sampleSource != address(0)) { try IWheelSample(sampleSource).observe() {} catch {} }
    }

    function _slotCount() internal view
        override(WorkWheel, ComputeKind, OpsKind, ResponseKind, ArbKind, SampleKind) returns (uint256)
    { return super._slotCount(); }

    function _due(uint256 s) internal view
        override(WorkWheel, ComputeKind, OpsKind, ResponseKind, ArbKind, SampleKind) returns (bool)
    { return super._due(s); }

    function _run(uint256 s) internal
        override(WorkWheel, ComputeKind, OpsKind, ResponseKind, ArbKind, SampleKind) returns (uint256)
    { return super._run(s); }
}
