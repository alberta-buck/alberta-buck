// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {WorkWheel, CompletionMemo} from "./WorkWheel.sol";
import {ComputeKind, OpsKind} from "./UpkeepKinds.sol";
import {IBasketUpkeep} from "../basket/IBasketUpkeep.sol";
import {IBasketView} from "../basket/IBasketView.sol";
import {ArbKind} from "./ArbKind.sol";
import {JubileeKind} from "./JubileeKind.sol";
import {IBuckJubilee} from "../IBuckJubilee.sol";
import {IBasketArb} from "../basket/IBasketArb.sol";
import {IDeskPolicy} from "../basket/IDeskPolicy.sol";
import {BN254} from "../BN254.sol";
import {IdentityRegistry} from "../IdentityRegistry.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

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

/// @notice The machine's wheel: Sample, Jubilee, Compute, Arb, Ops, N+2 basket
///         components. Arb precedes capital deployment. Unconfigured kinds add no slots. The initial
///         observation precedes all portfolio work; Ops also refreshes it.
contract BasketMachineWheel is WorkWheel, CompletionMemo, SampleKind, JubileeKind, ComputeKind, ArbKind, OpsKind, ResponseKind {
    using SafeERC20 for IERC20;
    constructor(address buck, address stable, address gov, uint16 kappa, uint256 reserveCap)
        WorkWheel(buck, gov, kappa, reserveCap) ArbKind(buck, stable) {}

    error InvalidArbConfiguration();

    /// @notice The canonical wheel delegates ROI work to its Carrying basket.
    /// The older wheel retains the isolated flash comparison implementation.
    function setArb(address pool, address basket, uint16 share, uint16 cap, uint16 edge)
        public override onlyGov
    {
        if (share != 0 || cap > 500 || edge > 10000) revert InvalidArbConfiguration();
        super.setArb(pool, basket, share, cap, edge);
    }

    function setStartMode(uint8) public override onlyGov { revert InvalidArbConfiguration(); }

    /// @notice The basket borrows each cycle's BUCK fresh from its credit line for the cycle's length
    ///         (Buck.flashIssue; owner, 2026-11-20) instead of spending its wallet: cycles as large as
    ///         the line's room, and fresh BUCK into the pool each cycle sells into. Off by default.
    bool public arbFlash;
    event ArbFlashSet(bool on);
    function setArbFlash(bool on) external onlyGov { arbFlash = on; emit ArbFlashSet(on); }

    error NoSuchSlot();

    /// @notice A keeper's direct entry (owner, 2026-11-20): run `slot` now if it has work, without
    ///         passing the slots before it, as one step of tick would (the same memory of idle slots,
    ///         reward and Ticked event). pending() names the slot: (cursor + steps) % slotCount(). The
    ///         cursor moves past it.
    function tickAt(uint256 slot) external workGuard returns (uint256 work, uint256 reservePay) {
        uint256 n = _slotCount();
        if (slot >= n) revert NoSuchSlot();
        if (!_settled(slot) && _due(slot)) {
            work = _run(slot);
            if (work == 0) _ranIdle(slot);
            else _moved();
        }
        cursor = (slot + 1) % n;
        if (work > 0) {
            scannedIdle = 0;
            reservePay = reserve * kappaBp / 10000;
            if (reservePay > 0) {
                reserve -= reservePay;
                payToken.safeTransfer(msg.sender, reservePay);
            }
        }
        emit Ticked(msg.sender, work, reservePay);
    }

    function plan(uint256 route) public view override returns (uint256 amount, uint8 direction, uint256 gain) {
        try IBasketArb(creditee).basketArbPlan(route) returns (IBasketArb.ArbPlan memory p) {
            return (p.amount, p.direction, p.totalReturn > 0 ? uint256(p.totalReturn) : 0);
        } catch { return (0, 0, 0); }
    }

    function arbCycle(uint256 route, uint8 direction, uint256 amount) external override returns (uint256) {
        if (msg.sender != address(this)) revert NotSelf();
        IBasketArb.ArbPlan memory result = IBasketArb(creditee).executeBasketArb(route, direction, amount, 2);
        return uint256(result.totalReturn);
    }

    // Proceeds and LP fees already belong to the basket. No caller bounty
    // is required and the reward reserve never supplies arb inventory.
    function _distribute(uint256, uint8, uint256, uint256) internal pure override {}

    function uniswapV3SwapCallback(int256, int256, bytes calldata) external pure override { revert BadCallback(); }

    /// @notice N triangles plus each unordered cross-TOKEN four-pool cycle.
    function arbRouteCount() external view returns (uint256) {
        return poolUb == address(0) ? 0 : _arbSlots();
    }

    function _arbSlots() internal view override returns (uint256) {
        uint256 n = triangles.length;
        return n + (n > 1 ? n * (n - 1) / 2 : 0);
    }

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

    /// @notice A slot that ran idle is retried at most this often while nothing else moves.
    uint256 public constant RETRY_SPAN = 1 hours;

    /// @dev What turns an idle slot into work: a trade on any pool the wheel reads (its price or
    ///      active liquidity), a change in what the basket holds (a deposit or an exit), Buck's
    ///      relief budget running out or refilling (a retirement it refused may then fit), or the hour turning
    ///      (the sample's interval, the desk's epoch and the jubilee's budget move with the clock).
    ///      With no trades and no flows `pending()` stays settled within the hour.
    function _inputs() internal view override returns (bytes32 h) {
        bytes memory held = abi.encodeWithSignature("balanceOf(address)", equityBasket);
        address buck = address(payToken);
        // Relief accrues every second: only whether it is spent says a refused retirement may now fit.
        (bool ok, bytes memory relief) = buck.staticcall(abi.encodeWithSignature("availableReliefScaled()"));
        h = keccak256(abi.encode(block.timestamp / RETRY_SPAN, _pool(poolUb), _peek(buck, held),
                                 ok && relief.length == 32 && abi.decode(relief, (uint256)) == 0));
        uint256 k = triangles.length;
        for (uint256 j; j < k; ++j) {
            Triangle storage t = triangles[j];
            h = keccak256(abi.encode(h, _pool(t.poolOwn), _pool(t.poolUsdc), _peek(t.token, held)));
        }
    }

    function _pool(address pool) private view returns (bytes32) {
        return _peek(pool, abi.encodeWithSignature("slot0()")) ^ _peek(pool, abi.encodeWithSignature("liquidity()"));
    }

    /// @dev The hash of what a view returns; 0 when the target has no code or refuses.
    function _peek(address target, bytes memory data) private view returns (bytes32) {
        if (target.code.length == 0) return 0;
        (bool ok, bytes memory ret) = target.staticcall(data);
        return ok ? keccak256(ret) : bytes32(0);
    }

    function _prepareOps() internal override {
        if (sampleSource != address(0)) { try IWheelSample(sampleSource).observe() {} catch {} }
    }

    function _settled(uint256 s) internal view override(WorkWheel, CompletionMemo) returns (bool) {
        return super._settled(s);
    }
    function _ranIdle(uint256 s) internal override(WorkWheel, CompletionMemo) { super._ranIdle(s); }
    function _moved() internal override(WorkWheel, CompletionMemo) { super._moved(); }
    function _rearmed() internal override(WorkWheel, CompletionMemo) { super._rearmed(); }

    function _slotCount() internal view
        override(WorkWheel, ComputeKind, OpsKind, ResponseKind, ArbKind, SampleKind, JubileeKind) returns (uint256)
    { return super._slotCount(); }

    /// @dev The desk's slot is due only when its policy advises an operation (owner, 2026-11-20: views
    ///      that report work their run does not do cost every keeper a tick): its run reverts NoAdvice
    ///      otherwise, and that was most of its runs.
    function _due(uint256 s) internal view
        override(WorkWheel, ComputeKind, OpsKind, ResponseKind, ArbKind, SampleKind, JubileeKind) returns (bool)
    {
        if (!super._due(s)) return false;
        if (!_isOpsSlot(s)) return true;
        try IDeskPolicy(opsDirector).monetaryEffort() returns (int32 effort, bool) { return effort != 0; }
        catch { return false; }
    }

    /// @dev Per kind, what an idle slot waits on (owner, 2026-11-20). The Jubilee fund's jobs and the
    ///      desk's operation run on day-scale schedules (the reserve's half-life, the desk's epoch) and
    ///      read neither the pools' spot prices nor the basket's positions: an idle one is retried when
    ///      the hour turns or Buck's relief budget runs out or refills, not after every arbitrage
    ///      cycle or other slot's work (a re-arm still reopens them). The arbitrage routes and the basket's responses read the pools
    ///      and the holdings, and keep the wheel's whole state (_inputs, and every work).
    function _memoKey(uint256 s) internal view override returns (bytes32) {
        if (_isJubileeSlot(s) || _isOpsSlot(s)) {
            (bool ok, bytes memory relief) = address(payToken).staticcall(
                abi.encodeWithSignature("availableReliefScaled()"));
            return keccak256(abi.encode(s, _rearms, block.timestamp / RETRY_SPAN,
                                        ok && relief.length == 32 && abi.decode(relief, (uint256)) == 0));
        }
        return super._memoKey(s);
    }

    function _run(uint256 s) internal
        override(WorkWheel, ComputeKind, OpsKind, ResponseKind, ArbKind, SampleKind, JubileeKind) returns (uint256)
    { return super._run(s); }
}
