// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityStorage} from "./BuckBasketEquityStorage.sol";
import {BasketJubileeMath} from "./BasketJubileeMath.sol";
import {BasketMarketOps} from "./BasketMarketOps.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

/// @notice Redemption execution for the equity shell. The shell deploys this
/// immutable facet and delegates only while it holds the position lock.
contract BasketExitFacet is BuckBasketEquityStorage {
    /// @dev Retire `out` shares, paying `to`.  In BUCK from cash when the
    ///      account can spend it and still fund the principal the exit may
    ///      have to retire (at most K x the payment); else from its share of
    ///      the positions (`_exitInKind`): sold for BUCK, or delivered in kind
    ///      on request.  The exit fee (the stress fee's hook) retires the
    ///      shares but leaves its fraction of the assets.  Returns the BUCK
    ///      paid and the shares NOT retired.
    function exitShares(uint256 out, Snap memory sn, address to, bool inKind, uint256 openingShortfall)
        external returns (uint256 pay, uint256 left)
    {
        if (!_positionEntered) revert NotSelf();
        uint256 S = totalShares;
        uint256 fWad = (out - out * eq.exitFeeBp / 10000) * 1e18 / S;
        uint256 eLow = _equityS(sn, MARK_LOW);
        uint256 value = eLow * fWad / 1e18;
        if (value == 0) revert Underwater();
        pay = value * (1e18 - _chargeBuck(sn.k)) / 1e18;

        if (portfolioLimits && netAccounting && netMark && !inKind && address(credit) != address(0) && out < S) {
            uint256 mark = _creditValueS(sn, eLow);
            _markAt(mark);
            uint256 redeemed;
            (pay, redeemed) = BasketJubileeMath.fundExit(address(buck), address(credit), creditId, mark,
                                                         sn.k, sn.spend, sn.lien, pay, out);
            if (redeemed != 0) {
                if (pay > sn.spend) creditLive = true;
                totalShares -= redeemed;
                _payCash(to, pay);
                dayFlow -= int256(pay);
                if (out == redeemed) return (pay, 0);
                out -= redeemed;
                fWad = (out - out * eq.exitFeeBp / 10000) * 1e18 / totalShares;
                sn = _snap();
            }
            (uint256 sold, uint256 remaining) = _exitInKind(out, fWad, sn, to, false, openingShortfall);
            return (pay + sold, remaining);
        }

        // Legacy Buck constrains an implicit draw through this prospective
        // mark. Canonical Buck spends cash only; its final actual collateral
        // and unchanged principal are checked after receipt retirement.
        if (!netAccounting) _markAt(_creditValueS(sn, eLow - pay));
        if (!inKind && pay + (netAccounting ? pay * sn.k / 1e18 : 0) <= _cashAvailable()) {
            totalShares = S - out;
            _payCash(to, pay);
            dayFlow -= int256(pay);
            return (pay, 0);
        }
        (pay, left) = _exitInKind(out, fWad, sn, to, inKind || !netAccounting, openingShortfall);
    }

    /// @dev The pro-rata exit, in kind: fraction `fWad` of every position and
    ///      of the wallet's TOKEN goes to `to` by transfer -- no swap, no
    ///      market moved. Canonical LP proceeds remain cash, never automatic
    ///      repayment. Net equity is paid by retaining TOKEN for a negative
    ///      BUCK residue (released cash + its fraction of cash-principal+claim).
    ///      Actual principal stays fixed; _exitSnapshot guards final backing.
    ///      Legacy Buck alone treats proceeds as implicit repayment. Corners:
    ///        * short of its lien share (leverage above one), it leaves TOKEN
    ///          behind worth the shortfall, at the low marks;
    ///        * legacy: past what an under-water account can spend, the rest stays in
    ///          the receipt as shares at the price that remains -- the exit
    ///          completes, and nobody's claim moves ahead of anybody's.
    function _exitInKind(uint256 out, uint256 fWad, Snap memory sn, address to, bool inKind, uint256 openingShortfall)
        internal returns (uint256 pay, uint256 left)
    {
        uint256 S = totalShares;
        (int256 residue, uint256 flow) = BasketMarketOps.exitAssets(idleToken, liquidityOf, address(buck),
            netAccounting, fWad, sn.buckEq * int256(fWad) / 1e18, to, inKind, constituents.length);
        if (residue > 0) {
            uint256 spend = _cashAvailable();
            if (portfolioLimits && netAccounting && netMark && !inKind) {
                // Selling shifts the LOW mark on the LP that stays. The cash backing
                // the lien is worth one-for-one, so retain the actual post-sale gap.
                Snap memory afterSale = _snap();
                uint256 value = _creditValueS(afterSale, _equityS(afterSale, MARK_LOW));
                uint256 limit = Math.mulDiv(value, afterSale.k, 1e18);
                uint256 allowance = limit + openingShortfall;
                uint256 room = allowance > afterSale.lien ? allowance - afterSale.lien : 0;
                spend = Math.min(spend, room > 0 ? room - 1 : 0);
            }
            pay = uint256(residue) < spend ? uint256(residue) : spend;
            uint256 unpaid = uint256(residue) - pay;
            if (unpaid > 0) {
                // Value what stays from a FRESH read: the positions just burned
                // are no longer in the snapshot's marks.
                uint256 eAfter = _equityS(_snap(), MARK_LOW);
                uint256 stay = S - out;
                left = eAfter > unpaid ? unpaid * stay / (eAfter - unpaid) : 0;
            }
            if (pay > 0) _payCash(to, pay);
            flow += pay;
        }
        totalShares = S - out + left;
        dayFlow -= int256(flow);
        // Canonical mode checks/marks after the receipt holding also changes.
        if (!netAccounting) _markS(_snap());
    }

    /// @notice (1+K)/2 of the dearest pool fee: what placing a BUCK deposit
    ///         costs, charged on BUCK in and BUCK out alike (1e18).
    function _chargeBuck(uint256 k) internal view returns (uint256) {
        uint256 feeMax = 0;
        for (uint256 i = 0; i < constituents.length; i++) {
            if (constituents[i].feeTier > feeMax) feeMax = constituents[i].feeTier;
        }
        return feeMax * 1e12 * (1e18 + k) / 2e18;
    }

}
