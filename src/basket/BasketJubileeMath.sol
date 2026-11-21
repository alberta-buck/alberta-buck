// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IBuckNetView} from "../IBuckNetView.sol";
import {BuckBasketStorage} from "./BuckBasketStorage.sol";
import {IBuckBasketVenue} from "./IBuckBasketVenue.sol";
import {PortfolioMath} from "./PortfolioMath.sol";
import {BuckTypes} from "../BuckTypes.sol";
import {CarryRatio} from "../lib/CarryRatio.sol";

interface IJubileeCashSnapshot is IBuckNetView {
    function issue(uint256 id, uint256 coverage, uint256 amount, uint256 maxPremium, address pool) external;
    function maturitiesPending() external view returns (bool);
    function totalIssued() external view returns (uint256);
    function balanceOf(address owner) external view returns (uint256);
    function jubilee() external view returns (address);
    function fundBalancesScaled() external view returns (uint256 gross, uint256 net);
    function selectedClaimSeconds(uint256 id, uint256 principal) external view returns (uint256 selected, uint256 total);
}

interface IJubileeFacilitySnapshot {
    function creditInfo(uint256 id) external view returns (uint256, uint256, uint32);
    function issuanceOf(uint256 id) external view returns (uint256 principal, uint256 seconds_);
}

interface IJubileePositionSnapshot {
    function basket() external view returns (address);
    function positionId() external view returns (uint256);
    function reliefPositionMark() external view returns (uint256);
}

interface IJubileeBasketSnapshot {
    function holdings(uint256 id) external view returns (uint128 shares, uint128 basis);
    function totalShares() external view returns (uint256);
    function LAMBDA_BP() external view returns (uint256);
}

/// @notice Nonrecursive receipt pricing when Jubilee owns shares of its debtor.
/// @dev Preconditions: assets exclude this claim; principal is actual issuance;
///      cash is the fund's net scaled BUCK; seconds are capped issuance-seconds;
///      shares/basis describe the fund's one receipt, including its gain fee.
///      No input is a promise of liquid relief or collateral for new issuance.
///
///      R(E) = min(principal, floor((cash + netReceipt(E)*Q)*selected/total/Q))
///      E    = max(0, assets - principal + R(E)).
///
///      All rounding matches the fund: floor gross receipt value and gain fee,
///      then floor relief to raw BUCK. R is nondecreasing and increases by at
///      most one when E increases by one. Thus E - RHS(E) is nondecreasing,
///      with a zero in [0, assets]. Bisection finds its FIRST zero in at most
///      256 iterations, selecting the conservative price when ownership or
///      integer rounding makes the solution nonunique. No iterative external
///      calls, guessed starting mark or floating-point convergence are needed.
///
///      Postconditions: equity <= assets, relief <= principal, and the two
///      equations hold exactly in raw units. Unpaid relief must be excluded
///      separately when marking collateral; this function moves no money.
library BasketJubileeMath {
    uint256 internal constant Q = 1e36;
    uint256 internal constant BP = 10_000;

    struct Inputs {
        uint256 assets;
        uint256 principal;
        uint256 cash;
        uint256 selected;
        uint256 total;
        uint256 fundShares;
        uint256 shares;
        uint256 basis;
        uint256 feeBp;
    }

    error InvalidJubileeInputs();
    error JubileeValueOverflow();

    function weights(BuckBasketStorage.Constituent[] storage assets,
                     mapping(uint256 => uint128) storage liquidity, mapping(uint256 => uint256) storage idle,
                     bool includeIdle) public view returns (uint256[] memory)
    {
        uint256[] memory values = new uint256[](assets.length);
        for (uint256 i; i < values.length; ++i) {
            IBuckBasketVenue.Marks memory m = IBuckBasketVenue(address(this)).marks(i, liquidity[i]);
            values[i] = m.posTwap;
            if (includeIdle) values[i] += idle[i] * m.pTwap / (10 ** assets[i].decimals);
        }
        return PortfolioMath.weights(values);
    }

    error NonzeroBasketPremium();

    /// @notice Executable new principal on the self-insured, zero-premium basket credit.
    function issuable(address buck, address credit, uint256 creditId, uint256 k, uint256 appraised)
        public view returns (uint256 room)
    {
        IJubileeCashSnapshot token = IJubileeCashSnapshot(buck);
        if (token.maturitiesPending()) return 0;
        (uint256 principal, uint256 seconds_) = IJubileeFacilitySnapshot(credit).issuanceOf(creditId);
        if (principal != 0 && seconds_ == principal * (50 * (365 days + 6 hours))) return 0;
        (uint256 face,, uint32 rate) = IJubileeFacilitySnapshot(credit).creditInfo(creditId);
        if (rate != 0) revert NonzeroBasketPremium();
        uint256 mark = Math.min(face, appraised);
        uint256 limit = Math.mulDiv(mark, k, 1e18);
        if (principal >= limit) return 0;
        room = Math.min(limit - principal, BuckTypes.MAX_BALANCE - principal);
        (uint256 gross,) = token.accountBalancesScaled(address(this));
        room = Math.min(room, (BuckTypes.MAX_BALANCE * 1e36 - gross) / 1e36);
        room = Math.min(room, type(uint256).max / 1e36 - token.totalIssued());
    }

    /// @notice The part of a redemption payable without selling TOKEN. New debt and a
    /// payment out of collateral cash both consume economic room. The facility's face,
    /// maturity and supply limits apply separately to the actual draw.
    function fundExit(address buck, address facility, uint256 id, uint256 mark, uint256 k,
                      uint256 spend, uint256 lien, uint256 quote, uint256 shares)
        public returns (uint256 paid, uint256 redeemed)
    {
        uint256 limit = Math.mulDiv(mark, k, 1e18);
        if (limit <= lien || quote == 0) return (0, 0);
        uint256 capacity = Math.min(limit - lien, spend + issuable(buck, facility, id, k, mark));
        redeemed = capacity >= quote ? shares : Math.mulDiv(shares, capacity, quote);
        paid = Math.mulDiv(quote, redeemed, shares);
        if (paid == 0) return (0, 0);
        if (paid > spend) {
            (uint256 face, uint256 active,) = IJubileeFacilitySnapshot(facility).creditInfo(id);
            IJubileeCashSnapshot(buck).issue(id, face - active, paid - spend, 0, address(0));
        }
    }

    /// @notice One cash/principal/claim read for the existing basket shell and
    ///         its facets. Preconditions and exact valuation are as in claim.
    function snapshot(address token_, address credit, uint256 id, uint256 gross)
        public view returns (uint256 cash, uint256 principal, uint256 relief, uint256 spend)
    {
        IJubileeCashSnapshot token = IJubileeCashSnapshot(token_);
        cash = token.netBalanceOf(address(this));
        // Budget full-value cash actually deliverable by an integer ERC-20
        // transfer. Fractional nominal dust remains wealth, not spendable cash.
        (uint256 cashGross, uint256 cashNet) = CarryRatio.read(token_, address(this));
        spend = CarryRatio.toNet(cashGross, cashNet, token.balanceOf(address(this)));
        if (credit != address(0)) (principal,) = IJubileeFacilitySnapshot(credit).issuanceOf(id);
        relief = claim(token_, id, principal, gross + cash);
    }

    /// @notice Read one canonical debtor's claim without calling back through
    ///         its equity view. Linked into the shell and upkeep facets, so
    ///         address(this) is the basket whose snapshot supplied bare assets.
    /// @dev Pre: assets include current net cash and TWAP assets, no claim;
    ///      principal is this NFT's issuance. Post: no state changes or mark
    ///      publication, no external calls inside the bounded solver. Direct
    ///      holdings/supply getters are safe during a captured basket snapshot.
    function claim(address token_, uint256 id, uint256 principal, uint256 assets)
        public view returns (uint256 relief)
    {
        if (principal == 0) return 0;
        IJubileeCashSnapshot token = IJubileeCashSnapshot(token_);
        Inputs memory p;
        p.assets = assets;
        p.principal = principal;
        (p.selected, p.total) = token.selectedClaimSeconds(id, principal);
        if (p.selected == 0) return 0;
        (, p.cash) = token.fundBalancesScaled();
        IJubileePositionSnapshot fund = IJubileePositionSnapshot(token.jubilee());
        uint256 position = fund.positionId();
        if (position != 0) {
            if (fund.basket() == address(this)) {
                IJubileeBasketSnapshot basket = IJubileeBasketSnapshot(address(this));
                (uint128 shares, uint128 basis) = basket.holdings(position);
                p.fundShares = shares;
                p.basis = basis;
                p.shares = basket.totalShares();
                p.feeBp = basket.LAMBDA_BP();
            } else {
                // A fund invested in another basket has no self-price term.
                uint256 mark = fund.reliefPositionMark();
                if (mark > (type(uint256).max - p.cash) / Q) revert JubileeValueOverflow();
                p.cash += mark * Q;
            }
        }
        (, relief) = solve(p);
    }

    function solve(Inputs memory p) internal pure returns (uint256 equity, uint256 relief) {
        if (p.selected > p.total || p.fundShares > p.shares || p.feeBp > BP) {
            revert InvalidJubileeInputs();
        }
        if (p.principal == 0 || p.selected == 0) return (_equity(p, 0), 0);
        // No self-holding: relief is independent of this basket's price.
        if (p.fundShares == 0) {
            relief = _relief(p, 0);
            return (_equity(p, relief), relief);
        }
        uint256 high = p.assets;
        while (equity < high) {
            uint256 mid = equity + (high - equity) / 2;
            if (mid >= _equity(p, _relief(p, mid))) high = mid;
            else equity = mid + 1;
        }
        relief = _relief(p, equity);
    }

    function _relief(Inputs memory p, uint256 equity) private pure returns (uint256) {
        uint256 mark = p.fundShares == 0 ? 0 : Math.mulDiv(equity, p.fundShares, p.shares);
        if (mark > p.basis) mark -= Math.mulDiv(mark - p.basis, p.feeBp, BP);
        if (mark > (type(uint256).max - p.cash) / Q) revert JubileeValueOverflow();
        uint256 relief = Math.mulDiv(p.cash + mark * Q, p.selected, p.total) / Q;
        return Math.min(relief, p.principal);
    }

    function _equity(Inputs memory p, uint256 relief) private pure returns (uint256) {
        // Avoid overflowing assets + relief, including for full-width tests.
        if (p.assets >= p.principal) return p.assets - p.principal + relief;
        uint256 shortfall = p.principal - p.assets;
        return relief > shortfall ? relief - shortfall : 0;
    }
}
