// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "../../src/BuckKControllerBase.sol";

/// @notice Minimal concrete subclass of BuckKControllerBase that lets tests
///         drive (buckValue, basketValue) via setters.  Used to unit-test
///         the abstract PID core independently of any oracle wiring.
contract BuckKControllerBaseHarness is BuckKControllerBase {

    int256 private _buckValue;
    int256 private _basketValue;

    constructor(
        int256 _Kp, int256 _Ki, int256 _Kd,
        uint256 _dT,
        uint256 _buckKMin, uint256 _buckKMax, uint256 _buckK,
        address _governance
    ) BuckKControllerBase(_Kp, _Ki, _Kd, _dT, _buckKMin, _buckKMax, _buckK, _governance) {
        _buckValue   = int256(1e18);
        _basketValue = int256(1e18);
    }

    function setReferences(int256 buckValue, int256 basketValue) external {
        _buckValue   = buckValue;
        _basketValue = basketValue;
    }

    function _readReferences() internal view override
        returns (int256 buckValue, int256 basketValue)
    {
        buckValue   = _buckValue;
        basketValue = _basketValue;
    }

    /// @notice Public alias for the protected `_reprime`; tests use it to
    ///         verify the re-prime semantics directly (the production
    ///         BuckKControllerDirect.reprime() is access-controlled).
    function harness_reprime() external {
        _reprime();
    }
}
