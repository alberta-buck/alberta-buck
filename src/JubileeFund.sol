// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import {IERC721Receiver} from "@openzeppelin/contracts/token/ERC721/IERC721Receiver.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckNet} from "./BuckNet.sol";
import {BuckCredit} from "./BuckCredit.sol";
import {IBuckBasketPosition} from "./basket/IBuckBasketPosition.sol";

interface IJubileeBasket is IBuckBasketPosition {
    function buck() external view returns (address);
    function receipt() external view returns (address);
    function holdings(uint256 id) external view returns (uint128 shares, uint128 basis);
}

/// @notice A pool can realize its investments before its own nominal premium
///         deposit is returned. It must restore enough nominal BUCK and have
///         approved BuckNet to make the refund; failure reverts the reduction.
interface IInsurerRefundLiquidity {
    function prepareRefund(uint256 tokenId, uint256 nominalRequired) external;
}

/// @notice Experimental external Jubilee coordinator for BuckNet.
/// @dev The basket and token are configured once after deployment to break
///      their constructor cycle. This coordinator owns one managed receipt,
///      pays the selected pro-rata cash plus actual net sale proceeds, and
///      never substitutes the target relief for available value. It requires
///      a basket implementing IBuckBasketPosition and is not a production
///      deployment until the real basket supports BuckNet accounting.
contract JubileeFund is IERC721Receiver {
    uint256 private constant Q = 1e36;
    bytes4 private constant UNDERWATER = bytes4(keccak256("Underwater()"));

    address public immutable governance;
    BuckNet public token;
    IJubileeBasket public basket;
    uint256 public positionId;
    bool private transient _entered;
    bool private transient _acceptingReceipt;
    uint256 private transient _receivedReceipt;

    event Configured(address indexed token, address indexed basket, address indexed receipt);
    event Invested(uint256 indexed positionId, uint256 nominal, uint256 netSpentScaled,
                   uint256 credited, uint256 sharesAdded);
    event PositionSale(uint256 indexed positionId, uint256 requestedShares, uint256 retiredShares,
                       uint256 remainingShares, uint256 proceedsScaled, bool underwater);
    event ReliefPaid(address indexed owner, uint256 indexed tokenId, uint256 principal,
                     uint256 selectedSeconds, uint256 totalSeconds, uint256 reliefScaled, uint256 refund);

    error NotGovernance();
    error Reentrant();
    error InvalidConfiguration();
    error AlreadyConfigured();
    error NotConfigured();
    error NotOwner();
    error BadAuthorization();
    error BadAmount();
    error NominalLimit();
    error MinReceived();
    error UnexpectedReceipt();
    error InconsistentPosition();
    error UnexpectedCashDecrease();
    error InsufficientPoolLiquidity();

    modifier nonReentrant() {
        if (_entered) revert Reentrant();
        _entered = true;
        _;
        _entered = false;
    }

    modifier configured() {
        if (address(token) == address(0)) revert NotConfigured();
        _;
    }

    constructor(address governance_) {
        if (governance_ == address(0)) revert InvalidConfiguration();
        governance = governance_;
    }

    /// @notice One-shot binding after the fund, token, basket and identity
    ///         have been deployed. Only a verified Carrying fund may proceed.
    function configure(BuckNet token_, IJubileeBasket basket_) external nonReentrant {
        if (msg.sender != governance) revert NotGovernance();
        if (address(token) != address(0)) revert AlreadyConfigured();
        if (address(token_).code.length == 0 || address(basket_).code.length == 0
            || token_.jubilee() != address(this) || basket_.buck() != address(token_)
            || basket_.receipt().code.length == 0
            || !token_.identity().isVerified(address(this)) || !token_.identity().isCarrying(address(this))) {
            revert InvalidConfiguration();
        }
        token = token_;
        basket = basket_;
        token_.approve(address(basket_), type(uint256).max);
        emit Configured(address(token_), address(basket_), basket_.receipt());
    }

    /// @notice Invest an explicitly bounded full-value amount in the same
    ///         basket receipt. Governance supplies slippage limits until the
    ///         separately specified reserve schedule is implemented.
    /// @param netAmount Minimum net BUCK to send, in raw external units.
    /// @param maxNominal Maximum nominal Carrying BUCK to transfer.
    /// @param minReceived Minimum BUCK credited to basket equity.
    /// @param minShares Minimum shares added to the fund's receipt.
    function invest(uint256 netAmount, uint256 maxNominal, uint256 minReceived, uint256 minShares)
        external configured nonReentrant returns (uint256 id, uint256 nominal, uint256 sharesAdded)
    {
        if (msg.sender != governance) revert NotGovernance();
        if (netAmount == 0) revert BadAmount();
        nominal = token.fundNominalForNet(netAmount);
        if (nominal > maxNominal) revert NominalLimit();
        (, uint256 beforeNet) = token.fundBalancesScaled();
        id = positionId;
        uint256 beforeShares;
        uint256 beforeBasis;
        uint256 reportedCredit;
        uint256 reportedShares;
        if (id == 0) {
            _acceptingReceipt = true;
            uint256 returnedId = basket.deposit(address(token), nominal, minShares);
            _acceptingReceipt = false;
            if (returnedId == 0 || returnedId != _receivedReceipt) revert UnexpectedReceipt();
            _receivedReceipt = 0;
            id = returnedId;
            positionId = id;
        } else {
            (beforeShares, beforeBasis) = basket.holdings(id);
            uint256 returnedId;
            (returnedId, reportedCredit, reportedShares) =
                basket.increasePosition(id, address(token), nominal, minReceived, minShares);
            if (returnedId != id) revert InconsistentPosition();
        }
        if (IERC721(basket.receipt()).ownerOf(id) != address(this)) revert InconsistentPosition();
        (uint256 afterShares, uint256 afterBasis) = basket.holdings(id);
        if (afterShares <= beforeShares || afterBasis < beforeBasis) revert InconsistentPosition();
        uint256 creditedActual = afterBasis - beforeBasis;
        sharesAdded = afterShares - beforeShares;
        if (beforeShares != 0 && (reportedCredit != creditedActual || reportedShares != sharesAdded)) {
            revert InconsistentPosition();
        }
        if (creditedActual < minReceived || sharesAdded < minShares) revert MinReceived();
        (, uint256 afterNet) = token.fundBalancesScaled();
        if (afterNet > beforeNet || beforeNet - afterNet < netAmount * Q) revert InconsistentPosition();
        emit Invested(id, nominal, beforeNet - afterNet, creditedActual, sharesAdded);
    }

    /// @notice Reduce the caller's authorized facility after selling its
    ///         proportional share of the managed position. The claimant gets
    ///         their share of pre-sale cash plus actual net BUCK sale proceeds,
    ///         capped at the principal; TOKEN received in kind is not BUCK.
    function reduce(uint256 tokenId, uint256 principal)
        external configured nonReentrant returns (uint256 reliefScaled, uint256 premiumRefund)
    {
        BuckCredit credit = token.buckCredit();
        if (credit.ownerOf(tokenId) != msg.sender) revert NotOwner();
        (uint256 authorized,, uint64 deadline) = token.reductionAuthorization(tokenId);
        if (principal == 0 || principal != authorized || block.timestamp > deadline) revert BadAuthorization();
        (uint256 priorPrincipal,) = credit.issuanceOf(tokenId);
        if (principal > priorPrincipal) revert BadAmount();
        (uint256 selected, uint256 total) = token.selectedClaimSeconds(tokenId, principal);
        (, uint256 cashBefore) = token.fundBalancesScaled();
        uint256 proceeds = _sellProportion(selected, total, cashBefore);
        uint256 cashShare = total == 0 ? 0 : Math.mulDiv(cashBefore, selected, total);
        uint256 cap = principal * Q;
        reliefScaled = proceeds >= cap || cashShare >= cap - proceeds ? cap : cashShare + proceeds;
        _prepareRefund(tokenId, principal, priorPrincipal);
        premiumRefund = token.settleFromJubilee(msg.sender, tokenId, principal, reliefScaled);
        emit ReliefPaid(msg.sender, tokenId, principal, selected, total, reliefScaled, premiumRefund);
    }

    function _sellProportion(uint256 selected, uint256 total, uint256 cashBefore) private returns (uint256 proceeds) {
        uint256 id = positionId;
        if (id == 0 || selected == 0 || total == 0) return 0;
        (uint256 beforeShares,) = basket.holdings(id);
        uint256 requested = Math.mulDiv(beforeShares, selected, total);
        if (requested == 0) return 0;
        try basket.redeemShares(id, requested, 0) returns (uint256 newId, uint256, uint256 retired, uint256 remaining) {
            if (retired > requested || remaining != beforeShares - retired
                || newId != (remaining == 0 ? 0 : id)) revert InconsistentPosition();
            positionId = newId;
            (, uint256 cashAfter) = token.fundBalancesScaled();
            if (cashAfter < cashBefore) revert UnexpectedCashDecrease();
            proceeds = cashAfter - cashBefore;
            emit PositionSale(id, requested, retired, remaining, proceeds, false);
        } catch (bytes memory reason) {
            // A zero-value or dust exit cannot retire any shares. Preserve the
            // receipt and count zero proceeds; all other failures propagate.
            if (reason.length != 4 || bytes4(reason) != UNDERWATER) _bubble(reason);
            emit PositionSale(id, requested, 0, beforeShares, 0, true);
        }
    }

    function _prepareRefund(uint256 tokenId, uint256 principal, uint256 priorPrincipal) private {
        uint256 deposit = token.premiumPrincipal(tokenId);
        uint256 refund = principal == priorPrincipal ? deposit : Math.mulDiv(deposit, principal, priorPrincipal);
        if (refund == 0) return;
        address pool = token.facilityPool(tokenId);
        if (pool == address(0)) revert InsufficientPoolLiquidity();
        if (token.balanceOf(pool) < refund) {
            IInsurerRefundLiquidity(pool).prepareRefund(tokenId, refund);
            if (token.balanceOf(pool) < refund) revert InsufficientPoolLiquidity();
        }
    }

    function onERC721Received(address operator, address from, uint256 id, bytes calldata)
        external returns (bytes4)
    {
        if (!_acceptingReceipt || msg.sender != basket.receipt() || operator != address(basket)
            || from != address(0) || id == 0 || _receivedReceipt != 0) revert UnexpectedReceipt();
        _receivedReceipt = id;
        return IERC721Receiver.onERC721Received.selector;
    }

    function _bubble(bytes memory reason) private pure {
        assembly ("memory-safe") { revert(add(reason, 32), mload(reason)) }
    }
}
