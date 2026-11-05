// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {BuckCredit} from "./BuckCredit.sol";
import {IdentityRegistry} from "./IdentityRegistry.sol";
import {JubileeClaimBook} from "./JubileeClaimBook.sol";
import {BuckRenewal, BuckRenewalConsent} from "./BuckRenewalConsent.sol";

/// @notice Monetary boundary used by the existing external Jubilee fund.
/// @dev The token authenticates callbacks, holds its monetary lock and
///      enforces exact principal/cash settlement plus the shared relief budget.
///      The fund owns the marked-payout policy and prepares insurer liquidity
///      before entering the token. No concrete token implementation is named.
interface IBuckJubilee is IERC20 {
    function buckCredit() external view returns (BuckCredit);
    function identity() external view returns (IdentityRegistry);
    function jubilee() external view returns (address);
    function claimBook() external view returns (JubileeClaimBook);
    function renewalConsent() external view returns (BuckRenewalConsent);
    function facilityPool(uint256 tokenId) external view returns (address);
    function premiumPrincipal(uint256 tokenId) external view returns (uint256);
    function reductionAuthorization(uint256 tokenId)
        external view returns (uint256 principal, uint256 minimumReliefScaled, uint64 deadline);
    function fundBalancesScaled() external view returns (uint256 gross, uint256 net);
    function fundNominalForNet(uint256 netRaw) external view returns (uint256 nominalRaw);
    function maxFundNetPayout() external view returns (uint256 raw);
    function availableReliefScaled() external view returns (uint256 netScaled);
    function claimSeconds() external view returns (uint256);
    function selectedClaimSeconds(uint256 tokenId, uint256 principal)
        external view returns (uint256 selected, uint256 total);
    function settleFromJubilee(address owner, uint256 tokenId, uint256 principal, uint256 reliefScaled)
        external returns (uint256 nominalRefund);
    function renewFromJubilee(address owner, uint256 tokenId, BuckRenewal.Terms calldata terms, uint256 reliefRaw)
        external returns (uint256 nominalRefund, uint256 premiumRaw);
    function matureFromJubilee(address owner, uint256 tokenId) external returns (uint256 deferredPrincipal);
    function claimMaturedPremium(uint256 tokenId) external returns (uint256 nominalRefund);
    function burnDeferredFromJubilee(uint256 principal) external;
}
