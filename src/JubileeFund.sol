// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import {IERC721Receiver} from "@openzeppelin/contracts/token/ERC721/IERC721Receiver.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckNet} from "./BuckNet.sol";
import {BuckCredit} from "./BuckCredit.sol";
import {IBuckBasketPosition} from "./basket/IBuckBasketPosition.sol";
import {JubileeReserveMath} from "./lib/JubileeReserveMath.sol";
import {WideCounter} from "./lib/WideCounter.sol";
import {BuckRenewal} from "./BuckRenewalConsent.sol";

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
///      a basket implementing IBuckBasketPosition. Leveraged basket behavior,
///      in-kind TOKEN conversion and real insurer liquidity are not complete;
///      this is not a production deployment.
contract JubileeFund is IERC721Receiver {
    using WideCounter for WideCounter.Count;
    uint256 private constant Q = 1e36;
    uint256 private constant BP = 10_000;
    uint256 private constant RELIEF_SECONDS = 50 * (365 days + 6 hours);
    bytes4 private constant UNDERWATER = bytes4(keccak256("Underwater()"));

    address public immutable governance;
    address public investmentKeeper;
    BuckNet public token;
    IJubileeBasket public basket;
    uint256 public positionId;
    uint16 public reserveTargetBp = 1000;       // 10% of the capped target
    uint16 public investmentThresholdBp = 100;   // 1% of the reserve target
    uint64 public reserveCheckpoint;
    uint256 public observedExcessScaled;
    uint256 public reserveBudgetScaled;
    WideCounter.Count private _investedNet;
    WideCounter.Count private _saleProceedsNet;
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
    event RenewalRelief(address indexed owner, uint256 indexed tokenId, uint256 relief,
                        uint256 refund, uint256 premium);
    event ReserveCheckpointed(uint256 targetScaled, uint256 excessScaled, uint256 budgetScaled);
    event ReserveParametersSet(uint16 targetBp, uint16 thresholdBp);
    event InvestmentKeeperSet(address indexed keeper);

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
    error NotToken();
    error ReserveBelowThreshold();
    error ReserveBudgetExceeded();
    error TimestampOverflow();
    error NotInvestmentKeeper();

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
        reserveCheckpoint = _now();
        _checkpointReserve();
        emit Configured(address(token_), address(basket_), basket_.receipt());
    }

    /// @notice Governance override for an explicitly bounded investment in
    ///         the same receipt. Scheduled investment is the normal path;
    ///         this override may intentionally spend into the reserve.
    /// @param netAmount Minimum net BUCK to send, in raw external units.
    /// @param maxNominal Maximum nominal Carrying BUCK to transfer.
    /// @param minReceived Minimum BUCK credited to basket equity.
    /// @param minShares Minimum shares added to the fund's receipt.
    function invest(uint256 netAmount, uint256 maxNominal, uint256 minReceived, uint256 minShares)
        external configured nonReentrant returns (uint256 id, uint256 nominal, uint256 sharesAdded)
    {
        if (msg.sender != governance) revert NotGovernance();
        _checkpointReserve();
        uint256 spent;
        (id, nominal, sharesAdded, spent) = _invest(netAmount, maxNominal, minReceived, minShares);
        _spendBudget(spent);
        _checkpointReserve();
    }

    /// @notice Invest the matured budget once it reaches 1% of the target
    ///         reserve by default. Governance or its appointed keeper bounds
    ///         nominal outflow and slippage. If nothing is large enough, the
    ///         checkpoint is saved and the function returns three zeroes.
    function investScheduled(uint256 maxNominal, uint256 minReceived, uint256 minShares)
        external configured nonReentrant returns (uint256 id, uint256 nominal, uint256 sharesAdded)
    {
        if (msg.sender != governance && msg.sender != investmentKeeper) revert NotInvestmentKeeper();
        (uint256 target, uint256 excess, uint256 budget) = _checkpointReserve();
        uint256 threshold = Math.mulDiv(target, investmentThresholdBp, BP, Math.Rounding.Ceil);
        if (budget < threshold || budget < Q) return (0, 0, 0);
        uint256 amount = budget / Q;
        uint256 maxPayable = token.maxFundNetPayout();
        if (amount > maxPayable) amount = maxPayable;
        if (amount == 0) return (0, 0, 0);
        // A whole nominal Carrying transfer can deliver slightly more than
        // the requested whole-net amount. Leave that rounding inside both
        // the matured budget and the current excess, not the cash reserve.
        (uint256 gross, uint256 net) = token.fundBalancesScaled();
        uint256 quotedNominal = token.fundNominalForNet(amount);
        uint256 exactNet = Math.mulDiv(net, quotedNominal * Q, gross);
        if (exactNet > budget || exactNet > excess) {
            amount--;
            if (amount == 0) return (0, 0, 0);
            quotedNominal = token.fundNominalForNet(amount);
            exactNet = Math.mulDiv(net, quotedNominal * Q, gross);
        }
        if (exactNet < threshold) return (0, 0, 0);
        uint256 spent;
        (id, nominal, sharesAdded, spent) = _invest(amount, maxNominal, minReceived, minShares);
        if (spent > budget || spent > excess) revert ReserveBudgetExceeded();
        if (spent < threshold) revert ReserveBelowThreshold();
        _spendBudget(spent);
        _checkpointReserve();
    }

    function _invest(uint256 netAmount, uint256 maxNominal, uint256 minReceived, uint256 minShares)
        private returns (uint256 id, uint256 nominal, uint256 sharesAdded, uint256 spent)
    {
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
        spent = beforeNet - afterNet;
        _investedNet.add(spent);
        emit Invested(id, nominal, spent, creditedActual, sharesAdded);
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
        _checkpointReserve();
        (uint256 selected, uint256 total) = token.selectedClaimSeconds(tokenId, principal);
        (, uint256 cashBefore) = token.fundBalancesScaled();
        uint256 proceeds = _sellProportion(selected, total, cashBefore);
        uint256 cashShare = total == 0 ? 0 : Math.mulDiv(cashBefore, selected, total);
        uint256 cap = principal * Q;
        reliefScaled = proceeds >= cap || cashShare >= cap - proceeds ? cap : cashShare + proceeds;
        _prepareRefund(tokenId, principal, priorPrincipal);
        premiumRefund = token.settleFromJubilee(msg.sender, tokenId, principal, reliefScaled);
        _checkpointReserve();
        emit ReliefPaid(msg.sender, tokenId, principal, selected, total, reliefScaled, premiumRefund);
    }

    /// @notice Renew the caller's facility using the exact insurer proposal
    ///         and owner authorization. The fund sells the selected share
    ///         before Buck's lock and rounds actual relief down to raw units.
    function renew(uint256 tokenId, BuckRenewal.Terms calldata terms)
        external configured nonReentrant returns (uint256 relief, uint256 refund, uint256 premium)
    {
        (uint256 principal,) = token.renewalConsent().checkRenewal(msg.sender, tokenId, terms);
        _checkpointReserve();
        (uint256 selected, uint256 total) = token.selectedClaimSeconds(tokenId, principal);
        (, uint256 cashBefore) = token.fundBalancesScaled();
        uint256 proceeds = _sellProportion(selected, total, cashBefore);
        uint256 cashShare = total == 0 ? 0 : Math.mulDiv(cashBefore, selected, total);
        uint256 cap = principal * Q;
        uint256 available = proceeds >= cap || cashShare >= cap - proceeds ? cap : cashShare + proceeds;
        relief = available / Q;
        if (relief < terms.minimumRelief) revert BadAuthorization();
        _prepareRefund(tokenId, principal, principal);
        (refund, premium) = token.renewFromJubilee(msg.sender, tokenId, terms, relief);
        _checkpointReserve();
        emit RenewalRelief(msg.sender, tokenId, relief, refund, premium);
    }

    /// @notice Observe current excess and age only the excess seen at the
    ///         previous checkpoint. Anyone may keep the budget up to date.
    function checkpointReserve() external configured nonReentrant returns (uint256, uint256, uint256) {
        return _checkpointReserve();
    }

    /// @notice BuckNet invokes this after each issuance; it only checkpoints
    ///         views and never trades while the token's reentrancy lock is held.
    function checkpointIssuance() external configured {
        if (msg.sender != address(token)) revert NotToken();
        // Fund actions checkpoint before and after their own nested calls.
        if (_entered) return;
        _entered = true;
        _checkpointReserve();
        _entered = false;
    }

    function setReserveParameters(uint16 targetBp, uint16 thresholdBp) external configured nonReentrant {
        if (msg.sender != governance) revert NotGovernance();
        if (targetBp > BP || thresholdBp > BP) revert BadAmount();
        _checkpointReserve();
        reserveTargetBp = targetBp;
        investmentThresholdBp = thresholdBp;
        _checkpointReserve();
        emit ReserveParametersSet(targetBp, thresholdBp);
    }

    /// @notice Zero revokes the keeper. Only governance retains the manual
    ///         override and the ability to change reserve parameters.
    function setInvestmentKeeper(address keeper) external configured nonReentrant {
        if (msg.sender != governance) revert NotGovernance();
        investmentKeeper = keeper;
        emit InvestmentKeeperSet(keeper);
    }

    /// @notice The view includes elapsed budget growth without treating newly
    ///         observed cash as though it existed throughout that interval.
    function previewReserve() public view configured returns (uint256 target, uint256 excess, uint256 budget) {
        target = Math.mulDiv(token.claimSeconds(), Q * uint256(reserveTargetBp), RELIEF_SECONDS * BP);
        (, uint256 cash) = token.fundBalancesScaled();
        excess = cash > target ? cash - target : 0;
        budget = JubileeReserveMath.accrue(reserveBudgetScaled, observedExcessScaled,
                                           block.timestamp - uint256(reserveCheckpoint));
        if (budget > excess) budget = excess;
    }

    function _checkpointReserve() private returns (uint256 target, uint256 excess, uint256 budget) {
        (target, excess, budget) = previewReserve();
        reserveCheckpoint = _now();
        observedExcessScaled = excess;
        reserveBudgetScaled = budget;
        emit ReserveCheckpointed(target, excess, budget);
    }

    function _spendBudget(uint256 amount) private {
        reserveBudgetScaled = amount >= reserveBudgetScaled ? 0 : reserveBudgetScaled - amount;
    }

    function _now() private view returns (uint64) {
        if (block.timestamp > type(uint64).max) revert TimestampOverflow();
        return uint64(block.timestamp);
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
            _saleProceedsNet.add(proceeds);
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

    /// @notice Cumulative full-value BUCK invested and actually received from
    ///         position sales, with fractional remainders. In-kind TOKEN is
    ///         excluded until a future conversion records real BUCK proceeds.
    function investmentFlows() external view returns (WideCounter.Count memory invested,
                                                        WideCounter.Count memory saleProceeds) {
        return (_investedNet, _saleProceedsNet);
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
