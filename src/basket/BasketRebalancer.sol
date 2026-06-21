// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBasketRebalancer} from "./IBasketRebalancer.sol";

/// @title BasketRebalancer -- governance-curated FX route registry.
///
/// @notice The replaceable route provider for BuckBasket's BUCK<->TOKEN
///         conversions.  Governance registers, per ordered (tokenIn, tokenOut)
///         pair, a Uniswap V3 encoded path through deep external pools (e.g.
///         TOKEN -> USDC -> BUCK).  The basket reads `pathFor` and executes the
///         path via `ISwapRouter`; an unregistered pair returns empty bytes and
///         the basket falls back to its internal TOKEN/BUCK pool.
///
///         Routing is curated off-chain (which path is cheapest), not searched
///         on-chain -- the registry just stores and serves the chosen paths.
///         Replacing the whole strategy is a single `BuckBasket.setRebalancer`.
contract BasketRebalancer is IBasketRebalancer {

    address public governance;

    /// @dev keccak256(tokenIn, tokenOut) => encoded V3 path.
    mapping(bytes32 => bytes) private _route;

    event RouteSet(address indexed tokenIn, address indexed tokenOut, bytes path);
    event GovernanceTransferred(address indexed from, address indexed to);

    constructor(address _governance) {
        require(_governance != address(0), "gov=0");
        governance = _governance;
    }

    modifier onlyGov() {
        require(msg.sender == governance, "not governance");
        _;
    }

    /// @notice Register (or clear, with empty `path`) the route for swapping
    ///         `tokenIn` into `tokenOut`.  Path is a Uniswap V3 encoded path
    ///         whose first token is `tokenIn` and last is `tokenOut`.
    function setRoute(address tokenIn, address tokenOut, bytes calldata path)
        external onlyGov
    {
        require(tokenIn != address(0) && tokenOut != address(0), "zero token");
        require(tokenIn != tokenOut, "same token");
        _route[_key(tokenIn, tokenOut)] = path;
        emit RouteSet(tokenIn, tokenOut, path);
    }

    /// @inheritdoc IBasketRebalancer
    function pathFor(address tokenIn, address tokenOut)
        external view returns (bytes memory)
    {
        return _route[_key(tokenIn, tokenOut)];
    }

    function transferGovernance(address to) external onlyGov {
        require(to != address(0), "gov=0");
        emit GovernanceTransferred(governance, to);
        governance = to;
    }

    function _key(address tokenIn, address tokenOut) private pure returns (bytes32) {
        return keccak256(abi.encodePacked(tokenIn, tokenOut));
    }
}
