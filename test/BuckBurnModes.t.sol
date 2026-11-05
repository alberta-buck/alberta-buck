// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckNetBurnMath as Planner} from "../src/lib/BuckNetBurnMath.sol";

/// @dev Independent integer enumeration of actual post-settlement balances,
///      covering nominal Carrying targets and full-value sponsored refunds.
contract BuckBurnModesTest is Test {
    uint256 constant Q = 1e36;

    function testFuzz_modesMatchExhaustiveSettlement(uint64 seed) public pure {
        uint256 r = uint256(keccak256(abi.encode(seed)));
        uint256 principal = 1 + r % 40;
        uint256 gross = ((r >> 8) % 81) * Q + ((r >> 16) % 10) * Q / 10;
        bool carrying = (r >> 24) % 2 == 1;
        bool sponsored = !carrying && (r >> 25) % 2 == 1;
        uint256 wallet = carrying ? gross * ((r >> 32) % 101) / 100 : gross;
        uint256 poolGross = ((r >> 40) % 41) * Q;
        uint256 age = principal * (1 + (r >> 48) % 100);
        Planner.Snapshot memory s = Planner.Snapshot({principal:principal, issuedSeconds:age,
            totalSeconds:age + (r >> 56) % 500, fundValueScaled:((r >> 64) % 41) * Q,
            reliefLimitScaled:((r >> 72) % 21) * Q, premiumPrincipal:(r >> 80) % (principal + 1),
            poolGrossScaled:poolGross, poolNetScaled:poolGross * ((r >> 88) % 101) / 100,
            poolAvailableNominal:(r >> 96) % 41, walletScaled:wallet});
        Planner.Owner memory o = Planner.Owner({nominalScaled:gross,
            sponsorAvailableScaled:((r >> 104) % 21) * Q, carrying:carrying,
            sponsored:sponsored, selfRefund:(r >> 112) % 4 == 0});
        uint256 face = 1 + (r >> 120) % 50;
        Planner.Coverage memory c = Planner.Coverage({face:face, active:(r >> 128) % (face + 1),
            mark:(r >> 136) % (face + 1), k:((r >> 144) % 151) * 1e16});
        uint256 requested = 1 + (r >> 152) % 70;
        uint256 expected;
        uint256 bestTarget;
        uint256 bestPrincipal;
        for (uint256 p = 1; p <= principal; ++p) {
            uint256 decrease = _settlementDecrease(s, c, o, p);
            if (decrease >= requested && expected == 0) expected = p;
            uint256 target = Math.min(requested, decrease);
            if (target > bestTarget) { bestTarget = target; bestPrincipal = p; }
        }
        Planner.Plan memory actual = Planner.plan(s, c, o, requested);
        assertEq(uint256(actual.status), uint256(expected == 0 ? Planner.Status.Impossible : Planner.Status.Found));
        if (expected != 0) assertEq(actual.principal, expected);
        (uint256 target_, Planner.Plan memory best) = Planner.planAtMost(s, c, o, requested);
        assertEq(target_, bestTarget);
        if (bestTarget != 0) assertEq(best.principal, bestPrincipal);
    }
    function _settlementDecrease(Planner.Snapshot memory s, Planner.Coverage memory c,
                                 Planner.Owner memory o, uint256 p) private pure returns (uint256) {
        uint256 principal = s.principal;
        uint256 wallet = s.walletScaled;
        uint256 gross = o.nominalScaled;
        bool carrying = o.carrying;
        bool sponsored = o.sponsored;
        uint256 poolGross = s.poolGrossScaled;
        uint256 age = s.issuedSeconds;
        uint256 beforeVisible = (carrying ? gross : wallet) / Q;
        uint256 capacity = (c.mark * c.active / c.face) * c.k / 1e18;
        uint256 shortfall = principal > capacity ? principal - capacity : 0;
        uint256 refund = s.premiumPrincipal * p / principal;
        if (refund > s.poolAvailableNominal || refund * Q > poolGross) return 0;
        uint256 poolNet = refund == 0 ? 0 : s.poolNetScaled * refund * Q / poolGross;
        uint256 received = o.selfRefund ? 0 : sponsored ? refund * Q : poolNet;
        if (sponsored && !o.selfRefund && received - poolNet > o.sponsorAvailableScaled) return 0;
        uint256 selected = age * p / principal;
        uint256 relief = Math.min(p * Q, s.fundValueScaled * selected / s.totalSeconds);
        if (relief > s.reliefLimitScaled || p * Q > wallet + received + relief) return 0;
        uint256 reducedFace = c.active * p / principal;
        uint256 nextCapacity = (c.mark * (c.active - reducedFace) / c.face) * c.k / 1e18;
        uint256 nextShortfall = principal - p > nextCapacity ? principal - p - nextCapacity : 0;
        if (nextShortfall > shortfall) return 0;
        uint256 afterVisible;
        uint256 debit = p * Q - relief;
        if (carrying) {
            uint256 nominal = gross + (o.selfRefund ? 0 : refund * Q);
            uint256 consumed = debit == 0 ? 0 : Math.ceilDiv(nominal * debit, wallet + received);
            afterVisible = (nominal - consumed) / Q;
        } else afterVisible = (wallet + received - debit) / Q;
        if (afterVisible > beforeVisible) return 0;
        uint256 decrease = beforeVisible - afterVisible;
        return decrease;
    }
}
