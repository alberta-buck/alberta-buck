// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IUniswapV3Factory} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Factory.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {IUniswapV3SwapCallback} from "@uniswap/v3-core/contracts/interfaces/callback/IUniswapV3SwapCallback.sol";
import {BuckNet} from "./BuckNet.sol";

/// @notice Buy full-value BUCK from a canonical V3 pool with an atomic net
///         minimum. The pool's nominal output may carry demurrage.
/// @dev The guard only holds the settlement token temporarily; BUCK goes
///      directly from the Carrying pool to the recipient. The recipient must
///      satisfy BuckNet's own identity checks with that pool. This experimental
///      adapter is not a general router or a production deployment.
contract BuckNetSwapGuard is IUniswapV3SwapCallback {
    using SafeERC20 for IERC20;

    uint256 private constant Q = 1e36;
    uint160 private constant MIN_SQRT = 4295128739;
    uint160 private constant MAX_SQRT = 1461446703485210103287273052203988822378723970342;

    BuckNet public immutable buck;
    IERC20 public immutable settlement;
    IUniswapV3Factory public immutable factory;
    bool private transient _entered;
    address private transient _pool;
    bool private transient _settlementIs0;
    uint256 private transient _maximum;
    uint256 private transient _paid;

    event NetBuckPurchased(address indexed buyer, address indexed recipient, address indexed pool,
                           uint256 paid, uint256 nominal, uint256 netScaled);

    error BadConfiguration();
    error BadPool();
    error BadAmount();
    error Reentrant();
    error BadCallback();
    error SettlementMismatch();
    error NetMinimum();
    error ZeroPoolValue();

    constructor(BuckNet buck_, IERC20 settlement_, IUniswapV3Factory factory_) {
        if (address(buck_).code.length == 0 || address(settlement_).code.length == 0
            || address(factory_).code.length == 0 || address(buck_) == address(settlement_)) {
            revert BadConfiguration();
        }
        buck = buck_;
        settlement = settlement_;
        factory = factory_;
    }

    /// @notice Spend up to `amountIn` settlement units; revert unless at
    ///         least `minimumNet` whole full-value BUCK units reach recipient.
    function buyExactInput(address pool, uint256 amountIn, uint256 minimumNet, address recipient, uint160 priceLimit)
        external returns (uint256 paid, uint256 nominal, uint256 netScaled)
    {
        return _buy(pool, amountIn, minimumNet, recipient, priceLimit, false);
    }

    /// @notice Receive at least `desiredNet` whole full-value BUCK units,
    ///         spending at most `maximumIn` settlement units.
    function buyExactOutput(address pool, uint256 desiredNet, uint256 maximumIn, address recipient, uint160 priceLimit)
        external returns (uint256 paid, uint256 nominal, uint256 netScaled)
    {
        if (desiredNet == 0) revert BadAmount();
        return _buy(pool, maximumIn, desiredNet, recipient, priceLimit, true);
    }

    function _buy(address pool, uint256 maxIn, uint256 minNet, address recipient, uint160 priceLimit, bool exactOut)
        private returns (uint256 paid, uint256 nominal, uint256 netScaled)
    {
        if (_entered) revert Reentrant();
        _entered = true;
        if (recipient == address(0) || recipient == pool || maxIn == 0 || maxIn > uint256(type(int256).max)
            || minNet > type(uint256).max / Q) revert BadAmount();
        IUniswapV3Pool venue = IUniswapV3Pool(pool);
        if (pool.code.length == 0) revert BadPool();
        address token0 = venue.token0();
        address token1 = venue.token1();
        bool zeroForOne = token0 == address(settlement) && token1 == address(buck);
        if (!zeroForOne && !(token1 == address(settlement) && token0 == address(buck))) revert BadPool();
        if (factory.getPool(token0, token1, venue.fee()) != pool) revert BadPool();
        if (!buck.identity().isVerified(pool) || !buck.identity().isCarrying(pool)) revert BadPool();
        if (!buck.identity().isVerified(recipient) || buck.identity().isCarrying(recipient)) revert BadAmount();

        uint256 nominalRequested;
        if (exactOut) {
            (uint256 gross, uint256 net) = buck.accountBalancesScaled(pool);
            if (gross == 0 || net == 0) revert ZeroPoolValue();
            nominalRequested = Math.ceilDiv(Math.mulDiv(minNet * Q, gross, net, Math.Rounding.Ceil), Q);
            if (nominalRequested == 0 || nominalRequested > uint256(type(int256).max)) revert BadAmount();
        }
        (uint256 beforeHeld, uint256 beforeUnspent) = buck.heldAndUnspentScaled(recipient);
        uint256 beforeSettlement = settlement.balanceOf(address(this));
        settlement.safeTransferFrom(msg.sender, address(this), maxIn);
        if (settlement.balanceOf(address(this)) - beforeSettlement != maxIn) revert SettlementMismatch();
        _pool = pool;
        _settlementIs0 = zeroForOne;
        _maximum = maxIn;
        _paid = 0;
        if (priceLimit == 0) priceLimit = zeroForOne ? MIN_SQRT + 1 : MAX_SQRT - 1;
        int256 specified = exactOut ? -int256(nominalRequested) : int256(maxIn);
        (int256 delta0, int256 delta1) = venue.swap(recipient, zeroForOne, specified, priceLimit, "");
        int256 buckDelta = zeroForOne ? delta1 : delta0;
        if (buckDelta > 0 || buckDelta == type(int256).min) revert BadPool();
        nominal = uint256(-buckDelta);
        paid = _paid;
        (uint256 afterHeld, uint256 afterUnspent) = buck.heldAndUnspentScaled(recipient);
        uint256 beforeNet = beforeHeld + beforeUnspent;
        uint256 afterNet = afterHeld + afterUnspent;
        if (afterNet < beforeNet) revert NetMinimum();
        netScaled = afterNet - beforeNet;
        if (netScaled < minNet * Q) revert NetMinimum();
        _pool = address(0);
        _maximum = 0;
        _paid = 0;
        if (maxIn > paid) settlement.safeTransfer(msg.sender, maxIn - paid);
        _entered = false;
        emit NetBuckPurchased(msg.sender, recipient, pool, paid, nominal, netScaled);
    }

    function uniswapV3SwapCallback(int256 delta0, int256 delta1, bytes calldata) external {
        if (!_entered || msg.sender != _pool) revert BadCallback();
        int256 due = _settlementIs0 ? delta0 : delta1;
        int256 other = _settlementIs0 ? delta1 : delta0;
        if (due <= 0 || other > 0) revert BadCallback();
        uint256 amount = uint256(due);
        if (amount > _maximum - _paid) revert BadCallback();
        _paid += amount;
        settlement.safeTransfer(msg.sender, amount);
    }
}
