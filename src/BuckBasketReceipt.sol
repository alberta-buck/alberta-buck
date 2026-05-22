// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {ERC721}  from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";

/// @title BuckBasketReceipt -- ERC-721 claim NFT for direct-mint deposits.
///
/// @notice Each token represents a pending claim on a slice of BuckBasket's
///         Uniswap V3 pool liquidity.  All per-deposit detail
///         (TOKEN, principalToken, principalBuck, liquidityShare) lives in
///         BuckBasket.deposits[id]; this contract carries only identity.
///
///         Mint and burn are restricted to the bound BuckBasket address;
///         transfer follows the standard ERC-721 semantics so a holder can
///         freely sell or gift their claim.  Redemption is initiated by the
///         current owner against BuckBasket.redeem(id).
contract BuckBasketReceipt is ERC721 {

    address public immutable basket;
    uint256 private _nextId = 1;

    constructor(address _basket) ERC721("BuckBasket Receipt", "BBR") {
        require(_basket != address(0), "basket=0");
        basket = _basket;
    }

    function mint(address to) external returns (uint256 id) {
        require(msg.sender == basket, "only basket");
        id = _nextId++;
        _safeMint(to, id);
    }

    function burn(uint256 id) external {
        require(msg.sender == basket, "only basket");
        _burn(id);
    }
}
