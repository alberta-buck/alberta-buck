// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import {IERC721Receiver} from "@openzeppelin/contracts/token/ERC721/IERC721Receiver.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {BuckNet} from "./BuckNet.sol";
import {BuckCredit} from "./BuckCredit.sol";
import {BN254} from "./BN254.sol";
import {IdentityRegistry} from "./IdentityRegistry.sol";
import {IBuckBasketPosition} from "./basket/IBuckBasketPosition.sol";
import {IBuckBasketVenue} from "./basket/IBuckBasketVenue.sol";
import {JubileeReserveMath} from "./lib/JubileeReserveMath.sol";
import {WideCounter} from "./lib/WideCounter.sol";
import {BuckRenewal} from "./BuckRenewalConsent.sol";

interface IJubileeBasket is IBuckBasketPosition {
    function buck() external view returns (address);
    function receipt() external view returns (address);
    function holdings(uint256 id) external view returns (uint128 shares, uint128 basis);
    function valueOf(uint256 id) external view returns (uint256);
    function LAMBDA_BP() external view returns (uint256);
    function twapWindow() external view returns (uint32);
    function defaultMaxDeviationBp() external view returns (uint256);
    function liquidityOf(uint256 index) external view returns (uint128);
    function idleToken(uint256 index) external view returns (uint256);
    function constituentsLength() external view returns (uint256);
    function constituents(uint256 index) external view returns (
        address asset, uint8 decimals, uint256 basketAmount, uint256 initialPrice,
        uint24 fee, address pool, int24 lower, int24 upper, bool buckIsToken0,
        uint256 targetWeightBp, uint128 treasuryLiquidity
    );
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
///      pays the selected pro-rata share of net cash and marked basket value
///      from already-liquid BUCK, and never sells during a credit operation. It requires
///      a compatible basket. Leveraged basket accounting and concrete insurer
///      liquidity implementations remain incomplete;
///      this is not a production deployment.
contract JubileeFund is IERC721Receiver {
    using WideCounter for WideCounter.Count;
    using SafeERC20 for IERC20;
    uint256 private constant Q = 1e36;
    uint256 private constant BP = 10_000;
    uint256 private constant AUTO_REFUND_GAS = 500_000;
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
    address private transient _swapPool;
    address private transient _swapAsset;
    uint256 private transient _swapRemaining;
    bool private transient _swapBuckIsToken0;

    /// @notice One entry per basket constituent. A zero price limit means no
    ///         conversion route for that TOKEN. Minimum is full-value BUCK.
    struct SaleRoute { uint160 sqrtPriceLimitX96; uint256 minimumNetBuck; }
    struct SaleAsset { address asset; address pool; bool buckIsToken0; uint256 beforeBalance; }

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
    event SaleDeferred(uint256 indexed positionId, bytes4 reason);
    event TokenConverted(uint256 indexed constituent, address indexed asset, uint256 amountIn, uint256 netBuckScaled);
    event MaturedPremiumPending(uint256 indexed tokenId, address indexed pool, uint256 nominal);

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
    error ReserveNotDeficient();
    error SaleMinOut();
    error MarkUnavailable();
    error MarkPriceDeviation();
    error ReliefLiquidityLimit();
    error NotSelf();
    error BadRoute();
    error MissingRoute();
    error IncompleteSwap();
    error SwapMinOut();
    error BadSwapCallback();

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

    /// @notice Permit a certified operator to bind this fund as a public
    ///         Carrying service in the chosen registry before configuration.
    /// @dev The registry still checks the operator's certified identity and
    ///      the exact key, ciphertext and flags. Only governance may choose
    ///      or replace a pending authorization; no token or fund is configured.
    function authorizeIdentityBinding(
        IdentityRegistry registry, address binder, BN254.G1Point calldata pk, IdentityRegistry.ElGamalCT calldata E
    ) external {
        if (msg.sender != governance) revert NotGovernance();
        if (address(registry).code.length == 0 || address(token) != address(0)) revert InvalidConfiguration();
        registry.authorizeContractBinding(binder, pk, E, true, true);
    }

    /// @notice Cancel a pending identity binding before it is consumed.
    function revokeIdentityBinding(IdentityRegistry registry) external {
        if (msg.sender != governance) revert NotGovernance();
        if (address(registry).code.length == 0 || address(token) != address(0)) revert InvalidConfiguration();
        registry.revokeContractBindingAuthorization();
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

    /// @notice Replenish a below-target reserve in a transaction separate
    ///         from credit settlement. Governance or its keeper selects an
    ///         exact maximum number of receipt shares and bounds the actual
    ///         net BUCK proceeds, including any in-kind conversions.
    /// @dev Sale prices are not known beforehand, so a bounded sale may put
    ///      the reserve above target. Normal scheduled investing can later
    ///      use that excess. Underwater basket exits remain no-ops unless a
    ///      positive minimum is requested; other failures revert atomically.
    function redeemScheduled(uint256 requestedShares, uint256 minimumNetBuck, SaleRoute[] calldata routes)
        external configured nonReentrant returns (uint256 proceedsScaled)
    {
        if (msg.sender != governance && msg.sender != investmentKeeper) revert NotInvestmentKeeper();
        (uint256 target,,) = _checkpointReserve();
        (, uint256 cashBefore) = token.fundBalancesScaled();
        if (cashBefore >= target) revert ReserveNotDeficient();
        uint256 id = positionId;
        if (id == 0 || requestedShares == 0) revert BadAmount();
        (uint256 beforeShares,) = basket.holdings(id);
        if (requestedShares > beforeShares) revert BadAmount();
        proceedsScaled = _sellShares(id, requestedShares, beforeShares, cashBefore, routes);
        if (proceedsScaled / Q < minimumNetBuck) revert SaleMinOut();
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

    /// @notice Reduce the caller's authorized facility using already-liquid
    ///         BUCK. Its age-weighted share of net cash and the basket mark is
    ///         capped at principal and must fit the per-operation cash limit
    ///         and the block budget shared with renewals and deferred burns.
    function reduce(uint256 tokenId, uint256 principal)
        external configured nonReentrant returns (uint256 reliefScaled, uint256 premiumRefund)
    {
        BuckCredit credit = token.buckCredit();
        if (credit.ownerOf(tokenId) != msg.sender) revert NotOwner();
        (uint256 authorized,, uint64 deadline) = token.reductionAuthorization(tokenId);
        if (principal == 0 || principal != authorized || block.timestamp > deadline) revert BadAuthorization();
        (uint256 priorPrincipal,) = credit.issuanceOf(tokenId);
        if (principal > priorPrincipal) revert BadAmount();
        // Pool liquidity preparation is an external call and can change
        // market state. Price the relief only after that callback completes.
        _prepareRefund(tokenId, principal, priorPrincipal);
        _checkpointReserve();
        (uint256 selected, uint256 total) = token.selectedClaimSeconds(tokenId, principal);
        (, uint256 cashBefore) = token.fundBalancesScaled();
        reliefScaled = _markedRelief(principal, selected, total, cashBefore);
        if (reliefScaled > cashBefore / 10) revert ReliefLiquidityLimit();
        premiumRefund = token.settleFromJubilee(msg.sender, tokenId, principal, reliefScaled);
        _checkpointReserve();
        emit ReliefPaid(msg.sender, tokenId, principal, selected, total, reliefScaled, premiumRefund);
    }

    /// @notice Renew the caller's facility using the exact insurer proposal
    ///         and owner authorization. No basket position is sold; actual
    ///         relief is rounded down to whole raw BUCK units.
    function renew(uint256 tokenId, BuckRenewal.Terms calldata terms)
        external configured nonReentrant returns (uint256 relief, uint256 refund, uint256 premium)
    {
        (uint256 principal,) = token.renewalConsent().checkRenewal(msg.sender, tokenId, terms);
        (, uint256 seconds_) = token.buckCredit().issuanceOf(tokenId);
        if (principal != 0 && seconds_ == principal * RELIEF_SECONDS) revert BuckNet.FacilityMatured();
        _prepareRefund(tokenId, principal, principal);
        // The callback can affect other contracts; verify the consent and
        // principal snapshot again before taking the post-callback mark.
        (principal,) = token.renewalConsent().checkRenewal(msg.sender, tokenId, terms);
        _checkpointReserve();
        (uint256 selected, uint256 total) = token.selectedClaimSeconds(tokenId, principal);
        (, uint256 cashBefore) = token.fundBalancesScaled();
        uint256 available = _markedRelief(principal, selected, total, cashBefore);
        relief = available / Q;
        if (relief * Q > cashBefore / 10) revert ReliefLiquidityLimit();
        if (relief < terms.minimumRelief) revert BadAuthorization();
        (refund, premium) = token.renewFromJubilee(msg.sender, tokenId, terms, relief);
        _checkpointReserve();
        emit RenewalRelief(msg.sender, tokenId, relief, refund, premium);
    }

    /// @notice Quote a reduction and whether current liquidity limits block it.
    /// @dev A missing or unsafe basket mark reverts rather than inventing a
    ///      value. The owner still has to authorize execution separately.
    function previewRelief(uint256 tokenId, uint256 principal)
        external view configured returns (uint256 reliefScaled, uint256 cashScaled, bool exceedsLimit)
    {
        (uint256 selected, uint256 total) = token.selectedClaimSeconds(tokenId, principal);
        (, cashScaled) = token.fundBalancesScaled();
        reliefScaled = _markedRelief(principal, selected, total, cashScaled);
        exceedsLimit = reliefScaled > token.availableReliefScaled();
    }

    function _markedRelief(uint256 principal, uint256 selected, uint256 total, uint256 cash)
        private view returns (uint256)
    {
        if (selected == 0 || total == 0) return 0;
        uint256 marked = _positionMark();
        if (marked > (type(uint256).max - cash) / Q) revert BadAmount();
        uint256 proportional = Math.mulDiv(cash + marked * Q, selected, total);
        uint256 cap = principal * Q;
        return proportional < cap ? proportional : cap;
    }

    /// @dev Exclude the basket's accrued gain fee. Require actual TWAP history
    ///      and a spot price within the basket's configured TWAP band for
    ///      every pool contributing assets. A mark inside that band is still
    ///      an estimate, not a promise of executable sale proceeds.
    function _positionMark() private view returns (uint256 mark) {
        uint256 id = positionId;
        if (id == 0) return 0;
        uint256 count = basket.constituentsLength();
        uint32 window = basket.twapWindow();
        for (uint256 i; i < count; ++i) {
            if (basket.liquidityOf(i) == 0 && basket.idleToken(i) == 0) continue;
            (, , , , , address pool, , , , , ) = basket.constituents(i);
            if (window == 0 || pool.code.length == 0 || IUniswapV3Pool(pool).liquidity() == 0) {
                revert MarkUnavailable();
            }
            uint32[] memory ages = new uint32[](2);
            ages[0] = window;
            try IUniswapV3Pool(pool).observe(ages) returns (int56[] memory, uint160[] memory) {}
            catch { revert MarkUnavailable(); }
            uint256 tolerance = basket.defaultMaxDeviationBp();
            if (tolerance == 0 || tolerance > 1000) revert MarkUnavailable();
            IBuckBasketVenue.Marks memory prices = IBuckBasketVenue(address(basket)).marks(i, 0);
            if (prices.pTwap == 0 || prices.pLow == 0 || prices.pTwap < prices.pLow
                || prices.pTwap > prices.pHigh) {
                revert MarkUnavailable();
            }
            if (prices.pHigh - prices.pLow > Math.mulDiv(prices.pTwap, tolerance, BP)) {
                revert MarkPriceDeviation();
            }
        }
        mark = basket.valueOf(id);
        (, uint128 basis) = basket.holdings(id);
        if (mark > basis) mark -= Math.mulDiv(mark - basis, basket.LAMBDA_BP(), BP);
    }

    /// @notice Release the caller's fully aged facility without selling the
    ///         basket or spending the caller's BUCK. The fund assumes all
    ///         unburned principal; the same insurer pool owes the premium.
    /// @dev A failed insurer callback or transfer leaves a claim on that pool
    ///      without undoing lien release. Anyone can retry the refund later.
    function closeMatured(uint256 tokenId) external configured nonReentrant returns (uint256 principal) {
        if (token.buckCredit().ownerOf(tokenId) != msg.sender) revert NotOwner();
        _checkpointReserve();
        principal = token.matureFromJubilee(msg.sender, tokenId);
        _checkpointReserve();
        (, address pool, uint256 nominal) = token.claimBook().premiumRefunds(tokenId);
        if (nominal != 0) {
            // A hostile or expensive insurer callback must not exhaust the
            // gas needed to finish recording the released lien.
            try this.attemptMaturedPremium{gas: AUTO_REFUND_GAS}(tokenId) {} catch {
                emit MaturedPremiumPending(tokenId, pool, nominal);
            }
        }
    }

    /// @notice Retry the original pool's nominal premium refund independently
    ///         of the already released NFT and Jubilee's own liquidity.
    function claimMaturedPremium(uint256 tokenId) external configured nonReentrant returns (uint256) {
        return _claimMaturedPremium(tokenId);
    }

    /// @dev Isolates a failed pool callback so that a matured lien still closes.
    function attemptMaturedPremium(uint256 tokenId) external returns (uint256) {
        if (msg.sender != address(this) || !_entered) revert NotSelf();
        return _claimMaturedPremium(tokenId);
    }

    function _claimMaturedPremium(uint256 tokenId) private returns (uint256) {
        (, address pool, uint256 nominal) = token.claimBook().premiumRefunds(tokenId);
        if (nominal == 0 || pool == address(0)) revert BadAmount();
        if (token.balanceOf(pool) < nominal) IInsurerRefundLiquidity(pool).prepareRefund(tokenId, nominal);
        return token.claimMaturedPremium(tokenId);
    }

    /// @notice Permissioned reserve management burns already liquid BUCK
    ///         against deferred matured issuance; no basket call is made.
    function burnDeferred(uint256 principal) external configured nonReentrant {
        if (msg.sender != governance && msg.sender != investmentKeeper) revert NotInvestmentKeeper();
        _checkpointReserve();
        token.burnDeferredFromJubilee(principal);
        _checkpointReserve();
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

    function _sellShares(uint256 id, uint256 requested, uint256 beforeShares, uint256 cashBefore,
                         SaleRoute[] memory routes) private returns (uint256 proceeds)
    {
        try this.attemptSale(id, requested, beforeShares, routes)
            returns (bool underwater, uint256 newId, uint256 retired, uint256 remaining)
        {
            if (underwater) {
                emit SaleDeferred(id, UNDERWATER);
                emit PositionSale(id, requested, 0, beforeShares, 0, true);
                return 0;
            }
            positionId = newId;
            (, uint256 cashAfter) = token.fundBalancesScaled();
            if (cashAfter < cashBefore) revert UnexpectedCashDecrease();
            proceeds = cashAfter - cashBefore;
            _saleProceedsNet.add(proceeds);
            emit PositionSale(id, requested, retired, remaining, proceeds, false);
        } catch (bytes memory reason) { _bubble(reason); }
    }

    /// @dev External solely to isolate and authenticate one keeper sale.
    function attemptSale(uint256 id, uint256 requested, uint256 beforeShares, SaleRoute[] calldata routes)
        external returns (bool underwater, uint256 newId, uint256 retired, uint256 remaining)
    {
        if (msg.sender != address(this) || !_entered) revert NotSelf();
        uint256 count = basket.constituentsLength();
        if (routes.length != 0 && routes.length != count) revert BadRoute();
        SaleAsset[] memory assets = new SaleAsset[](count);
        for (uint256 i = 0; i < count; i++) {
            (address asset, , , , , address pool, , , bool buckIs0, , ) = basket.constituents(i);
            if (asset == address(0) || asset == address(token)) revert BadRoute();
            assets[i] = SaleAsset(asset, pool, buckIs0, IERC20(asset).balanceOf(address(this)));
        }
        try basket.redeemShares(id, requested, 0)
            returns (uint256 idAfter, uint256, uint256 retiredShares, uint256 remainingShares)
        {
            newId = idAfter;
            retired = retiredShares;
            remaining = remainingShares;
        } catch (bytes memory reason) {
            if (reason.length == 4 && bytes4(reason) == UNDERWATER) return (true, id, 0, beforeShares);
            _bubble(reason);
        }
        if (retired > requested || remaining != beforeShares - retired
            || newId != (remaining == 0 ? 0 : id)) revert InconsistentPosition();
        for (uint256 i = 0; i < count; i++) {
            uint256 balance = IERC20(assets[i].asset).balanceOf(address(this));
            if (balance < assets[i].beforeBalance) revert BadRoute();
            uint256 received = balance - assets[i].beforeBalance;
            if (received == 0) continue;
            if (routes.length == 0 || routes[i].sqrtPriceLimitX96 == 0) revert MissingRoute();
            _convertSaleAsset(i, assets[i], received, routes[i]);
        }
    }

    function _convertSaleAsset(uint256 index, SaleAsset memory asset, uint256 amount, SaleRoute calldata route) private {
        if (asset.pool.code.length == 0 || amount > uint256(type(int256).max)) revert BadRoute();
        (, uint256 cashBefore) = token.fundBalancesScaled();
        _swapPool = asset.pool;
        _swapAsset = asset.asset;
        _swapRemaining = amount;
        _swapBuckIsToken0 = asset.buckIsToken0;
        IUniswapV3Pool(asset.pool).swap(address(this), !asset.buckIsToken0, int256(amount),
                                        route.sqrtPriceLimitX96, "");
        if (_swapRemaining != 0 || IERC20(asset.asset).balanceOf(address(this)) != asset.beforeBalance) {
            revert IncompleteSwap();
        }
        _swapPool = address(0);
        _swapAsset = address(0);
        (, uint256 cashAfter) = token.fundBalancesScaled();
        if (cashAfter < cashBefore) revert UnexpectedCashDecrease();
        uint256 netReceived = cashAfter - cashBefore;
        if (netReceived / Q < route.minimumNetBuck) revert SwapMinOut();
        emit TokenConverted(index, asset.asset, amount, netReceived);
    }

    /// @notice Pay only the active constituent pool and only the TOKEN amount
    ///         received by this sale; no existing fund inventory is approved.
    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata) external {
        if (!_entered || msg.sender != _swapPool || _swapPool == address(0)) revert BadSwapCallback();
        int256 owed = _swapBuckIsToken0 ? amount1Delta : amount0Delta;
        int256 other = _swapBuckIsToken0 ? amount0Delta : amount1Delta;
        if (owed <= 0 || other > 0 || uint256(owed) > _swapRemaining) revert BadSwapCallback();
        _swapRemaining -= uint256(owed);
        IERC20(_swapAsset).safeTransfer(msg.sender, uint256(owed));
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
    ///         position sales, including converted TOKEN, with fractional
    ///         remainders. Unrelated pre-existing TOKEN is excluded.
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
