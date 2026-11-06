// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {WorkWheel} from "./WorkWheel.sol";
import {ComputeKind, OpsKind} from "./UpkeepKinds.sol";
import {IBasketUpkeep} from "../basket/IBasketUpkeep.sol";
import {IBasketView} from "../basket/IBasketView.sol";
import {ArbKind} from "./ArbKind.sol";
import {JubileeKind} from "./JubileeKind.sol";
import {IBuckJubilee} from "../IBuckJubilee.sol";
import {BN254} from "../BN254.sol";
import {IdentityRegistry} from "../IdentityRegistry.sol";

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

/// @notice First in the wheel's slot order, so every other kind acts on a
///         market the sample has already read, and no trade of the same
///         turn can spoil the reading. No reward for a failed observation
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

/// @notice The machine's wheel: Sample, Jubilee, Compute, Ops, N+2 basket
///         components, Arb. Unconfigured kinds add no slots. The initial
///         observation precedes all portfolio work; Ops also refreshes it.
contract BasketMachineWheel is WorkWheel, SampleKind, JubileeKind, ComputeKind, OpsKind, ResponseKind, ArbKind {
    constructor(address buck, address stable, address gov, uint16 kappa, uint256 reserveCap)
        WorkWheel(buck, gov, kappa, reserveCap) ArbKind(buck, stable) {}

    /// @notice Authorize this service's public Carrying identity in BUCK's
    ///         own registry; the certified operator completes the binding.
    /// @dev PRE: current governance, no active work/funding callback, and a
    ///      BUCK pay token exposing its immutable registry. POST: only an
    ///      exact binder/key/ciphertext/public/Carrying authorization is set.
    ///      No cash, reward reserve, work cursor or valuation is changed.
    ///      Carrying keeps reserve transfers and balanceOf in nominal units;
    ///      recipients still receive net BUCK after the pool's carried fees.
    function authorizeIdentityBinding(address binder, BN254.G1Point calldata pk,
        IdentityRegistry.ElGamalCT calldata E) external onlyGov workGuard
    {
        IBuckJubilee(address(payToken)).identity().authorizeContractBinding(binder, pk, E, true, true);
    }

    /// @notice Clear an unconsumed authorization in that same registry.
    /// @dev This does not revoke an already-certified identity or move money.
    function revokeIdentityBinding() external onlyGov workGuard {
        IBuckJubilee(address(payToken)).identity().revokeContractBindingAuthorization();
    }

    function _prepareOps() internal override {
        if (sampleSource != address(0)) { try IWheelSample(sampleSource).observe() {} catch {} }
    }

    function _slotCount() internal view
        override(WorkWheel, ComputeKind, OpsKind, ResponseKind, ArbKind, SampleKind, JubileeKind) returns (uint256)
    { return super._slotCount(); }

    function _due(uint256 s) internal view
        override(WorkWheel, ComputeKind, OpsKind, ResponseKind, ArbKind, SampleKind, JubileeKind) returns (bool)
    { return super._due(s); }

    function _run(uint256 s) internal
        override(WorkWheel, ComputeKind, OpsKind, ResponseKind, ArbKind, SampleKind, JubileeKind) returns (uint256)
    { return super._run(s); }
}
