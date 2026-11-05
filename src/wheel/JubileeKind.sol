// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {WorkWheel} from "./WorkWheel.sol";
import {JubileeFund, IJubileeBasket} from "../JubileeFund.sol";
import {IBuckJubilee} from "../IBuckJubilee.sol";

interface IWheelMaturities {
    function processMaturities(uint256 maxSteps) external returns (uint256 processed, bool current);
}

interface IWheelSharePrice {
    function sharePrice() external view returns (uint256);
}

interface IWheelBasketCredit {
    function netAccounting() external view returns (bool);
    function credit() external view returns (address);
    function creditId() external view returns (uint256);
    function liquidityTarget() external view returns (uint256);
    function retireCredit(uint256 principal, uint256 maxCash, uint256 minRelief) external returns (uint256, uint256);
}

/// @notice Bounded jobs in the basket's existing wheel: bring the claim heap
///         current, burn deferred issuance, maintain fund cash, and optionally
///         retire the basket's own principal from cash above its target.
/// @dev PRE: governance binds this wheel as the fund's keeper and valuation
///      wheel, and the basket names this wheel. Every attempt authenticates
///      that wiring again. Callers cannot supply routes, amounts or bounds.
///      POST: work means actual heap progress, principal burned or receipt
///      shares changed, or meaningful basket principal was retired. A failed attempt rolls back its entire job, pays no
///      reward and cannot block the other wheel kinds. Each job is attempted
///      at most once per block, including failures; rearm does not reset it.
///      Facility closure stays with the NFT owner. No job runs through the
///      separate valuation-only publication used during Buck.burn.
abstract contract JubileeKind is WorkWheel {
    uint256 private constant Q = 1e36;
    uint256 private constant BP = 10_000;
    uint160 private constant MIN_PRICE = 4295128739;
    uint160 private constant MAX_PRICE = 1461446703485210103287273052203988822378723970342;

    JubileeFund public jubileeFund;
    struct JubileePolicy {
        uint16 maturitySteps;        // 1..64 heap entries per job
        uint16 saleCapBp;            // maximum fraction of the one receipt
        uint16 slippageBp;           // 2..500, net mark/share and swap bounds
        uint256 deferredBurnCap;     // raw full-value BUCK per job
        uint256 retirementMinimum;   // raw basket principal needed to earn work
        uint256 retirementCap;       // raw basket principal per job; 0 disables
    }
    JubileePolicy public jubileePolicy;
    uint256[4] public jubileeAttemptClock; // block.number + 1, zero = never

    struct ReservePlan {
        bool sale;
        uint256 amount;              // receipt shares OR raw net BUCK
        uint256 nominal;             // exact Carrying outlay for investment
        uint256 minimum;             // raw net sale proceeds OR added shares
    }

    event JubileeConfigured(address indexed fund, JubileePolicy policy);
    event JubileeAttempted(uint8 indexed job, bool succeeded, uint256 work, bytes4 reason);
    error BadJubileeConfiguration();
    error NotWheelSelf();
    error RetirementInvariant();

    /// @notice Enable after configuring the existing basket/fund/wheel;
    ///         zero disables these jobs without changing fund ownership.
    function setJubilee(JubileeFund fund_, JubileePolicy calldata policy) external onlyGov {
        if (address(fund_) != address(0)) {
            if (policy.maturitySteps == 0 || policy.maturitySteps > 64
                || policy.saleCapBp == 0 || policy.saleCapBp > BP
                || policy.slippageBp < 2 || policy.slippageBp > 500
                || policy.deferredBurnCap == 0
                || (policy.retirementCap != 0 && (policy.retirementMinimum == 0
                    || policy.retirementMinimum > policy.retirementCap))) revert BadJubileeConfiguration();
            _checkFund(fund_);
        }
        jubileeFund = fund_;
        jubileePolicy = policy;
        delete jubileeAttemptClock;
        _markDirty();
        emit JubileeConfigured(address(fund_), policy);
    }

    function _checkFund(JubileeFund f) private view {
        IBuckJubilee token = f.token();
        if (address(token) != address(payToken) || token.jubilee() != address(f)
            || f.basket().wheel() != address(this) || f.investmentKeeper() != address(this)
            || address(f.valuationWheel()) != address(this) || valuationSource != address(f)) {
            revert BadJubileeConfiguration();
        }
    }

    function _slotCount() internal view virtual override returns (uint256) {
        return super._slotCount() + (address(jubileeFund) == address(0) ? 0
            : jubileePolicy.retirementCap == 0 ? 3 : 4);
    }

    function _due(uint256 s) internal view virtual override returns (bool) {
        uint256 base = super._slotCount();
        if (s < base) return super._due(s);
        uint8 job = uint8(s - base);
        if (jubileeAttemptClock[job] == block.number + 1) return false;
        // Include every target view in the catch boundary, not just execution.
        try this.jubileeDue(job) returns (bool due) { return due; }
        catch { return false; }
    }

    function _run(uint256 s) internal virtual override returns (uint256) {
        uint256 base = super._slotCount();
        if (s < base) return super._run(s);
        uint8 job = uint8(s - base);
        jubileeAttemptClock[job] = block.number + 1;
        // The self call is an atomic boundary around quotes AND mutations.
        try this.runJubileeJob(job) returns (uint256 work) {
            emit JubileeAttempted(job, true, work, bytes4(0));
            return work;
        } catch (bytes memory reason) {
            emit JubileeAttempted(job, false, 0, reason.length < 4 ? bytes4(0) : bytes4(reason));
            return 0;
        }
    }

    /// @notice Advisory due check; no checkpoint, publication or market writes.
    function jubileeDue(uint8 job) external view returns (bool) {
        JubileeFund f = jubileeFund;
        _checkFund(f);
        IBuckJubilee token = f.token();
        bool pending = token.claimBook().pending();
        if (job == 0) return pending;
        if (pending) return false;
        if (job == 1) return _deferredAmount(token) != 0;
        if (job == 3) { (,, uint256 amount) = _basketRetirement(f); return amount != 0; }
        if (job != 2) return false;
        if (_reservePlan(f).amount != 0) return true;
        (, uint256 excess,) = f.previewReserve();
        return excess != f.observedExcessScaled();
    }

    /// @dev Only the guarded wheel scan can execute. No arbitrary user calldata
    ///      reaches the fund. Relief/deferred burns retain Buck's shared block
    ///      budget. An exact heap batch counts as ONE job, not N work rewards.
    function runJubileeJob(uint8 job) external returns (uint256) {
        if (msg.sender != address(this)) revert NotWheelSelf();
        JubileeFund f = jubileeFund;
        _checkFund(f);
        IBuckJubilee token = f.token();
        if (job == 0) {
            (uint256 processed,) = IWheelMaturities(address(token)).processMaturities(jubileePolicy.maturitySteps);
            return processed == 0 ? 0 : 1;
        }
        if (job == 1) {
            uint256 amount = _deferredAmount(token);
            if (amount == 0) return 0;
            f.burnDeferred(amount);
            return 1;
        }
        if (job == 3) return _retireBasket(f);
        if (job != 2) return 0;
        ReservePlan memory p = _reservePlan(f);
        if (p.amount == 0) {
            // Discovering cash starts the existing half-life clock; it must
            // not backdate new excess or earn a work reward. Otherwise an
            // idle economy with no mint/burn would never start investing.
            f.checkpointReserve();
            return 0;
        }
        if (p.sale) {
            // Positive aggregate proceeds protect the guarded NET receipt
            // mark, including gain fees. Each in-kind leg also has a positive
            // minimum and a price limit derived from its canonical pool.
            uint256 proceeds = f.redeemScheduled(p.amount, p.minimum, _routes(f.basket()));
            return proceeds == 0 ? 0 : 1;
        }
        (,, uint256 shares) = f.investScheduled(p.nominal, p.amount, p.minimum);
        return shares == 0 ? 0 : 1;
    }

    function _deferredAmount(IBuckJubilee token) private view returns (uint256) {
        return Math.min(token.claimBook().deferredPrincipal(),
            Math.min(jubileePolicy.deferredBurnCap, token.availableReliefScaled() / Q));
    }

    /// @dev Read authoritative NFT principal, never infer it from cash. Only
    ///      the fund's own ordinary canonical basket is eligible, with a
    ///      matching facility. Refund/premium rules stay with its monetary
    ///      retirement API. Fully aged facilities need the
    ///      owner's separate maturity release, not repeated repayment attempts.
    function _basketRetirement(JubileeFund f) private view returns (uint256 id, uint256 principal, uint256 amount) {
        if (jubileePolicy.retirementCap == 0) return (0, 0, 0);
        IWheelBasketCredit b = IWheelBasketCredit(address(f.basket()));
        IBuckJubilee token = f.token();
        if (!b.netAccounting() || b.credit() != address(token.buckCredit())
            || token.identity().isCarrying(address(b))) return (0, 0, 0);
        id = b.creditId();
        uint256 seconds_;
        (principal, seconds_) = token.buckCredit().issuanceOf(id);
        if (principal == 0 || seconds_ == principal * (50 * (365 days + 6 hours))) return (id, principal, 0);
        uint256 cash = token.balanceOf(address(b));
        uint256 target = b.liquidityTarget();
        if (cash <= target) return (id, principal, 0);
        amount = Math.min(principal, Math.min(cash - target, jubileePolicy.retirementCap));
        if (amount < jubileePolicy.retirementMinimum && amount != principal) amount = 0;
    }

    /// @dev PRE: exact heap, guarded scan, eligible facility and surplus cash.
    ///      Publish before quoting; the basket prepares/settles separately from
    ///      its position lock and the fund never sells inside that reduction.
    ///      Scale principal down to the shared relief budget rather than skip
    ///      relief or pretend a refused payment occurred. POST: exact principal
    ///      reduction and the recomputed cash target still covered, or rollback.
    ///      A complete dust tail can be cleared but earns no work reward.
    function _retireBasket(JubileeFund f) private returns (uint256) {
        (uint256 id, uint256 principal, uint256 amount) = _basketRetirement(f);
        if (amount == 0) return 0;
        IBuckJubilee token = f.token();
        (uint256 selected,) = token.selectedClaimSeconds(id, principal);
        if (selected != 0) this.refreshValuation();
        (uint256 relief,, bool limited) = f.previewRelief(id, principal);
        if (limited) {
            // Quote the FULL facility before scaling: a partial selection
            // rounds its BUCK-seconds down and can understate the unit rate.
            amount = Math.min(amount, Math.mulDiv(principal, token.availableReliefScaled(), relief));
            if (amount == 0 || (amount < jubileePolicy.retirementMinimum && amount != principal)) return 0;
        }
        (relief,, limited) = f.previewRelief(id, amount);
        if (limited) revert RetirementInvariant();
        IWheelBasketCredit b = IWheelBasketCredit(address(f.basket()));
        b.retireCredit(amount, amount, relief);
        (uint256 afterPrincipal,) = token.buckCredit().issuanceOf(id);
        if (afterPrincipal != principal - amount || token.balanceOf(address(b)) < b.liquidityTarget()) {
            revert RetirementInvariant();
        }
        return amount < jubileePolicy.retirementMinimum ? 0 : 1;
    }

    /// @dev PRE: exact claim heap. Investment reuses the fund's half-life,
    ///      threshold and Carrying-rounding quote; it never competes with
    ///      deferred debt. Sale size is bounded by the deficit AND the governed
    ///      receipt fraction. Zero/unsafe marks and dust fail closed. A TWAP
    ///      share-price bound can refuse entry during volatility; retry rather
    ///      than weakening it or inventing low-K exit liquidity.
    function _reservePlan(JubileeFund f) private view returns (ReservePlan memory p) {
        (uint256 target,,) = f.previewReserve();
        (, uint256 cash) = f.token().fundBalancesScaled();
        if (cash < target) {
            uint256 id = f.positionId();
            if (id == 0) return p;
            (uint256 mark,) = f.wheelValuation(); // live guarded net-of-gain-fee mark
            if (mark == 0) return p;
            (uint256 shares,) = f.basket().holdings(id);
            p.sale = true;
            p.amount = Math.min(Math.mulDiv(target - cash, shares, mark * Q, Math.Rounding.Ceil),
                                Math.mulDiv(shares, jubileePolicy.saleCapBp, BP));
            p.minimum = Math.mulDiv(Math.mulDiv(mark, p.amount, shares), BP - jubileePolicy.slippageBp, BP);
        } else if (f.token().claimBook().deferredPrincipal() == 0) {
            (p.amount, p.nominal) = f.previewInvestment();
            if (p.amount == 0) return p;
            uint256 price = IWheelSharePrice(address(f.basket())).sharePrice();
            if (price == 0) return ReservePlan(false, 0, 0, 0);
            p.minimum = Math.mulDiv(Math.mulDiv(p.amount, 1e18, price), BP - jubileePolicy.slippageBp, BP);
        }
        if (p.minimum == 0) p.amount = 0;
    }

    /// @dev Only registered constituent pools; the fund authenticates every
    ///      callback and can sell only TOKEN newly received from this receipt.
    ///      Limit sqrt-price movement to half the governed price tolerance
    ///      (conservative to first order), in addition to the aggregate bound.
    function _routes(IJubileeBasket basket) private view returns (JubileeFund.SaleRoute[] memory routes) {
        uint256 n = basket.constituentsLength();
        routes = new JubileeFund.SaleRoute[](n);
        uint256 halfBp = jubileePolicy.slippageBp / 2;
        for (uint256 i; i < n; ++i) {
            (,,,,, address pool,,, bool buckIs0,,) = basket.constituents(i);
            (uint160 spot,,,,,,) = IUniswapV3Pool(pool).slot0();
            uint256 limit = Math.mulDiv(spot, buckIs0 ? BP + halfBp : BP - halfBp, BP);
            if (limit <= MIN_PRICE || limit >= MAX_PRICE || limit == spot) revert BadJubileeConfiguration();
            routes[i] = JubileeFund.SaleRoute(uint160(limit), 1);
        }
    }
}
