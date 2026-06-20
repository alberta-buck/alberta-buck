// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {ERC721}  from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {Base64}  from "@openzeppelin/contracts/utils/Base64.sol";
import {Strings} from "@openzeppelin/contracts/utils/Strings.sol";

/// @dev The common deposit getter both basket implementations expose
///      (auto-generated from `mapping(uint256 => Deposit) public deposits`).
interface IBasketDeposits {
    function deposits(uint256 id)
        external view returns (uint256 buckPrincipal, uint256 tokenPrincipal,
                               address token, uint64 depositTime);
}

interface IERC20Symbol {
    function symbol() external view returns (string memory);
}

/// @title BuckBasketReceipt -- ERC-721 claim NFT for direct-mint deposits.
///
/// @notice Each token represents a pro-rata claim on BuckBasket's pooled
///         Uniswap V3 liquidity.  Per-deposit detail lives in
///         `basket.deposits[id]`; this contract carries identity + on-chain
///         metadata (`tokenURI`) so NFT wallets render the claim.
///
///         Mint/burn are restricted to the bound BuckBasket.  `basket` is
///         **adoptable**: the current basket can hand authority to a successor
///         exactly once during migration, so outstanding receipts keep
///         redeeming against the new basket without being reissued.  Shared by
///         both the legacy and pro-rata baskets (identical `deposits` getter).
contract BuckBasketReceipt is ERC721 {
    using Strings for uint256;
    using Strings for address;

    address public basket;
    uint256 private _nextId = 1;

    event BasketAdopted(address indexed from, address indexed to);

    constructor(address _basket) ERC721("BuckBasket Receipt", "BBR") {
        require(_basket != address(0), "basket=0");
        basket = _basket;
    }

    modifier onlyBasket() {
        require(msg.sender == basket, "only basket");
        _;
    }

    function mint(address to) external onlyBasket returns (uint256 id) {
        id = _nextId++;
        _safeMint(to, id);
    }

    function burn(uint256 id) external onlyBasket {
        _burn(id);
    }

    /// @notice One-shot migration handoff: the live basket points the receipt
    ///         at its successor.  Callable only by the current basket.
    function adopt(address successor) external onlyBasket {
        require(successor != address(0), "successor=0");
        emit BasketAdopted(basket, successor);
        basket = successor;
    }

    /// @notice On-chain metadata: principal, original TOKEN, and deposit time,
    ///         read live from the owning basket.  Returned as a base64 data URI
    ///         so NFT wallets render without any off-chain service.
    function tokenURI(uint256 id) public view override returns (string memory) {
        _requireOwned(id);
        (uint256 principal, uint256 tokenAmt, address token, uint64 depositTime) =
            IBasketDeposits(basket).deposits(id);

        string memory attrs = string.concat(
            '[{"trait_type":"BUCK principal","value":"', principal.toString(),
            '"},{"trait_type":"Token","value":"', _symbol(token),
            '"},{"trait_type":"Token address","value":"', token.toHexString(),
            '"},{"trait_type":"Token deposited","value":"', tokenAmt.toString(),
            '"},{"display_type":"date","trait_type":"Deposited","value":',
            uint256(depositTime).toString(), '}]'
        );
        string memory json = string.concat(
            '{"name":"BuckBasket Receipt #', id.toString(),
            '","description":"Redeemable pro-rata claim on BuckBasket pooled liquidity.",',
            '"attributes":', attrs, '}'
        );
        return string.concat(
            "data:application/json;base64,", Base64.encode(bytes(json)));
    }

    /// @dev Best-effort TOKEN symbol; falls back to "TOKEN" for non-standard ERC-20s.
    function _symbol(address token) internal view returns (string memory) {
        try IERC20Symbol(token).symbol() returns (string memory s) {
            return s;
        } catch {
            return "TOKEN";
        }
    }
}
