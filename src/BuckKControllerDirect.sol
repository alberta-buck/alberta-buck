// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "./BuckKControllerBase.sol";

interface IBuckBasketRef {
    /// @notice Sum of `basketAmount_i * pool_price_in_BUCK_i` across all
    ///         basket constituents, expressed in 18-dec BUCK.
    function basketValueInBuck() external view returns (int256);
}

/// @title BuckKControllerDirect -- USD-free BUCK_K stabilization.
///
/// @notice The direct embodiment of the BUCK_K PID.  No external stablecoin
///         intermediary; BUCK measures itself against a basket of RWA-token
///         / BUCK Uniswap V3 pools.  Setpoint is the constant 1.0 BUCK by
///         definition; the process variable is the BuckBasket-published
///         basket value.
///
///         Sign convention is inherited from BuckKControllerBase:
///
///           error = buckValue - basketValue
///                 = 1.0 - basketValue (in this embodiment)
///
///         With positive Kp:
///           - basketValue > 1.0  (BUCK undervalued, inflation):
///                error < 0  ->  buckK DECREASES  ->  credit contracts.
///           - basketValue < 1.0  (BUCK overvalued, deflation):
///                error > 0  ->  buckK INCREASES  ->  credit expands.
///
///         BuckBasket is the privileged caller of `reprime()` -- it
///         invokes that hook after `addBasketToken` so the dilution-
///         induced jump in basketValue doesn't manifest as a single-cycle
///         P/I spike.
contract BuckKControllerDirect is BuckKControllerBase {

    IBuckBasketRef public basket;

    event BasketSet(address indexed basket);
    event Reprimed(int256 newP, int256 newI);

    constructor(
        int256 _Kp, int256 _Ki, int256 _Kd,
        uint256 _dT,
        uint256 _buckKMin, uint256 _buckKMax, uint256 _buckK,
        address _governance
    ) BuckKControllerBase(_Kp, _Ki, _Kd, _dT, _buckKMin, _buckKMax, _buckK, _governance) {}

    /// @notice One-shot wiring from governance after BuckBasket is deployed.
    /// @dev    Locked once set; deploying a new BuckBasket requires a new
    ///         controller.
    function setBasket(address _basket) external {
        require(msg.sender == governance, "Not governance");
        require(address(basket) == address(0), "basket already set");
        require(_basket != address(0), "basket=0");
        basket = IBuckBasketRef(_basket);
        emit BasketSet(_basket);
    }

    function _readReferences() internal view override
        returns (int256 buckValue, int256 basketValue)
    {
        buckValue = UNIT;
        basketValue = (address(basket) != address(0))
                    ? basket.basketValueInBuck()
                    : UNIT;
    }

    /// @notice Privileged: BuckBasket calls this after addBasketToken to
    ///         absorb the dilution discontinuity in basketValue.  Recaptures
    ///         P against the new process state and re-derives I so the
    ///         next no-error cycle reproduces the current buckK.
    function reprime() external {
        require(msg.sender == address(basket), "only basket");
        _reprime();
        emit Reprimed(P, I);
    }
}
