// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckCredit} from "./BuckCredit.sol";
import {IBuckJubilee} from "./IBuckJubilee.sol";
import {IBuckCashView, IBuckCashK} from "./IBuckCashView.sol";
import {BuckTypes} from "./BuckTypes.sol";
import {IdentityRegistry} from "./IdentityRegistry.sol";
import {BuckNetMintMath} from "./lib/BuckNetMintMath.sol";

/// @notice Optional read-only issuance estimates for the canonical Buck cash model.
/// @dev No authorization or settlement uses this contract. Keeping the shared
///      quote outside the token preserves its EVM size headroom and lets Python,
///      JavaScript and wallet clients use the same integer calculation.
contract BuckNetQuoter {
    uint256 private constant SCALE = 1e36;
    uint256 private constant K_SCALE = 1e18;
    uint256 private constant BP = 10_000;
    uint256 private constant POOL_ROI_INV = 10;
    uint256 private constant RELIEF_SECONDS = 50 * (365 days + 6 hours);

    IBuckCashView public immutable token;
    error BadAddress(); error CreditExceeded(); error BadPremiumRate(); error BadAmount();

    constructor(IBuckJubilee token_) {
        if (address(token_).code.length == 0) revert BadAddress();
        token = IBuckCashView(address(token_));
    }

    /// @notice Estimate issuance costs for activating `coverage` on one NFT.
    /// @return presentCoverage Total current value of the activated coverage
    ///         after this activation, not just the added coverage.
    /// @return premium Full-value BUCK deposit for the new coverage.
    /// @return reserveRequired Full-value BUCK held before issuance; it is not spent.
    /// @return maxSpendable Largest new spendable amount within the current K,
    ///         NFT, cash-account and total-issuance numerical bounds.
    /// @dev Uses current K, funding factor and credit mark. `issue` recomputes
    ///      K and checks owner, identity, pool, fund and maturity state at
    ///      execution. A positive estimate does not guarantee success. The
    ///      caller supplies its own maximum premium and expected insurer pool
    ///      to `issue`; the pool is checked only when a premium is due.
    function quoteIssue(uint256 tokenId, uint256 coverage)
        external view returns (uint256 presentCoverage, uint256 premium,
                               uint256 reserveRequired, uint256 maxSpendable)
    {
        BuckCredit credit = token.buckCredit();
        address owner = credit.ownerOf(tokenId);
        (uint256 face, uint256 active, uint32 rate) = credit.creditInfo(tokenId);
        if (coverage > face - active) revert CreditExceeded();
        if (uint256(rate) * POOL_ROI_INV >= BP) revert BadPremiumRate();
        if (face != 0) {
            uint256 mark = credit.depreciatedFaceValue(tokenId);
            presentCoverage = Math.mulDiv(mark, active + coverage, face);
            uint256 beforeValue = Math.mulDiv(mark, active, face);
            premium = Math.mulDiv(presentCoverage - beforeValue, uint256(rate) * POOL_ROI_INV, BP);
        }
        IBuckCashK k = token.buckK();
        reserveRequired = Math.mulDiv(premium, k.fundingFactor(), K_SCALE);
        IdentityRegistry identity = token.identity();
        if (!_eligible(identity, owner)) {
            return (presentCoverage, premium, reserveRequired, 0);
        }
        uint256 limit = Math.min(Math.mulDiv(presentCoverage, k.currentBuckK(), K_SCALE), BuckTypes.MAX_BALANCE);
        (uint256 prior, uint256 seconds_) = credit.issuanceOf(tokenId);
        if (prior != 0 && seconds_ == prior * RELIEF_SECONDS) {
            return (presentCoverage, premium, reserveRequired, 0);
        }
        uint256 room = limit > prior ? limit - prior : 0;
        (uint256 cashScaled,) = token.accountBalancesScaled(owner);
        uint256 accountRoom = (BuckTypes.MAX_BALANCE * SCALE - cashScaled) / SCALE;
        room = Math.min(room, accountRoom);
        room = Math.min(room, type(uint256).max / SCALE - token.totalIssued());
        if (room > premium) maxSpendable = room - premium;
    }

    /// @notice Find the least additional coverage for a desired spendable
    ///         amount on one facility at current observed state.
    /// @dev Status is 0: found, 1: impossible in this snapshot, 2: work limit.
    ///      A work limit is not a proof of impossibility. This is an advisory
    ///      quote, not owner authorization or a guarantee that K, premium,
    ///      reserve, identity, pool or maturity state will remain unchanged.
    ///      Use issue with explicit maximum premium and expected pool.
    function quoteSpendable(uint256 tokenId, uint256 target)
        external view returns (uint256 coverage, uint256 premium, uint256 reserveRequired,
                               BuckNetMintMath.Status status, uint256 iterations)
    {
        if (target == 0) revert BadAmount();
        BuckCredit credit = token.buckCredit();
        address owner = credit.ownerOf(tokenId);
        (uint256 face, uint256 active, uint32 rate) = credit.creditInfo(tokenId);
        if (uint256(rate) * POOL_ROI_INV >= BP) revert BadPremiumRate();
        IdentityRegistry identity = token.identity();
        if (!_eligible(identity, owner)) {
            return (0, 0, 0, BuckNetMintMath.Status.Impossible, 0);
        }
        (uint256 prior, uint256 seconds_) = credit.issuanceOf(tokenId);
        if (prior != 0 && seconds_ == prior * RELIEF_SECONDS) {
            return (0, 0, 0, BuckNetMintMath.Status.Impossible, 0);
        }
        (uint256 cashScaled,) = token.accountBalancesScaled(owner);
        uint256 room = (BuckTypes.MAX_BALANCE * SCALE - cashScaled) / SCALE;
        room = Math.min(room, type(uint256).max / SCALE - token.totalIssued());
        IBuckCashK k = token.buckK();
        BuckNetMintMath.Snapshot memory snapshot = BuckNetMintMath.Snapshot({
            face: face, activated: active, mark: credit.depreciatedFaceValue(tokenId),
            principal: prior, k: k.currentBuckK(), effectiveRate: uint256(rate) * POOL_ROI_INV,
            accountRoom: room
        });
        BuckNetMintMath.Plan memory result = BuckNetMintMath.plan(snapshot, target);
        if (result.status == BuckNetMintMath.Status.Found) {
            uint256 afterValue = Math.mulDiv(snapshot.mark, active + result.coverage, face);
            if (Math.mulDiv(afterValue, snapshot.k, K_SCALE) < prior + target + result.premium) {
                revert CreditExceeded();
            }
            reserveRequired = Math.mulDiv(result.premium, k.fundingFactor(), K_SCALE);
        }
        return (result.coverage, result.premium, reserveRequired, result.status, result.iterations);
    }
    function _eligible(IdentityRegistry identity, address owner) private view returns (bool) {
        if (owner == token.jubilee() || !identity.isVerified(owner)) return false;
        if (!identity.isCarrying(owner)) return true;
        // The older experimental oracle has no approved Carrying issuance.
        // This compatibility read creates no alternate token deployment path.
        try token.approvedCarryingIssuer(owner) returns (bool approved) { return approved; }
        catch { return false; }
    }
}
