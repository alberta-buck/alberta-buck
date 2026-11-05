// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BasketJubileeMath as JM} from "../../src/basket/BasketJubileeMath.sol";

contract BasketJubileeMathTest is Test {
    uint256 constant Q = 1e36;

    function solve(JM.Inputs memory p) external pure returns (uint256, uint256) {
        return JM.solve(p);
    }

    function _example() private pure returns (JM.Inputs memory p) {
        return JM.Inputs(180e6, 100e6, 20e6 * Q, 1, 4, 50e6, 100e6, 0, 0);
    }

    function test_uncappedMutualClaimMatchesExactFractionExample() public pure {
        (uint256 equity, uint256 relief) = JM.solve(_example());
        // Exact Fraction solution E=680/7, R=120/7; raw-unit conservative
        // rounding is within one raw BUCK of that rational solution here.
        assertEq(equity, uint256(680e6) / 7);
        assertEq(relief, uint256(120e6) / 7);
        assertEq(equity, 180e6 - 100e6 + relief);
    }

    function test_gainFeeIsExcludedFromTheFundReceiptMark() public pure {
        JM.Inputs memory p = _example();
        p.basis = 20e6;
        p.feeBp = 2500;
        (uint256 equity, uint256 relief) = JM.solve(p);
        // Above basis: R=1/4*(20 + 3/4*E/2 + 1/4*20).
        // E=(80+6.25)/(1-3/32)=2760/29, before integer rounding.
        assertApproxEqAbs(equity, uint256(2760e6) / 29, 1);
        assertEq(equity, 80e6 + relief);
        p.basis = 100e6;
        (equity, relief) = JM.solve(p);
        assertEq(equity, uint256(680e6) / 7); // no gain, no fee
        assertEq(relief, uint256(120e6) / 7);
    }

    function test_fullClaimCapCannotMakeEquityExceedBareAssets() public pure {
        JM.Inputs memory p = _example();
        p.cash = 1000e6 * Q;
        (uint256 equity, uint256 relief) = JM.solve(p);
        assertEq(equity, p.assets);
        assertEq(relief, p.principal);
    }

    function test_pricingTheClaimPreventsTheBareEquityEntryWindfall() public pure {
        JM.Inputs memory p = _example();
        (uint256 beforeEquity,) = JM.solve(p);
        uint256 newShares = 10e6 * p.shares / beforeEquity;
        p.assets += 10e6;
        p.shares += newShares;
        (uint256 afterEquity,) = JM.solve(p);
        assertApproxEqAbs(newShares * afterEquity / p.shares, 10e6, 1);
        assertApproxEqAbs(p.fundShares * afterEquity / p.shares, beforeEquity / 2, 1);

        p = _example();
        newShares = 10e6 * p.shares / (p.assets - p.principal);
        p.assets += 10e6;
        p.shares += newShares;
        (afterEquity,) = JM.solve(p);
        assertApproxEqAbs(newShares * afterEquity / p.shares, 11_875_000, 1);
    }

    function test_singularOwnershipSelectsLeastEquityInsteadOfInventingAPrice() public pure {
        JM.Inputs memory p = JM.Inputs(90, 100, 10 * Q, 1, 1, 100, 100, 100, 2500);
        (uint256 equity, uint256 relief) = JM.solve(p);
        assertEq(equity, 0);
        assertEq(relief, 10);
        // All E in [0,90] satisfy the equations: choose the least solution.
    }

    function test_insolvencyAndNoAgeDoNotCreateCreditOrEquity() public pure {
        JM.Inputs memory p = _example();
        p.assets = 10e6;
        (uint256 equity, uint256 relief) = JM.solve(p);
        assertEq(equity, 0);
        assertEq(relief, 5e6);
        p.selected = 0;
        p.total = 0;
        (equity, relief) = JM.solve(p);
        assertEq(equity, 0);
        assertEq(relief, 0);
    }

    function test_noFundSharesUsesCashOnlyIncludingFractionalRawCash() public pure {
        JM.Inputs memory p = _example();
        p.fundShares = 0;
        p.shares = 0;
        p.cash = 23 * Q + Q / 2;
        p.selected = 3;
        (uint256 equity, uint256 relief) = JM.solve(p);
        assertEq(relief, 17);
        assertEq(equity, 80e6 + 17);
    }

    function test_fullWidthAssetsDoNotOverflowWhenClaimIsCapped() public pure {
        JM.Inputs memory p = JM.Inputs(type(uint256).max, 1, Q, 1, 1, 0, 0, 0, 0);
        (uint256 equity, uint256 relief) = JM.solve(p);
        assertEq(equity, type(uint256).max);
        assertEq(relief, 1);
    }

    function test_invalidProportionsAndUnsafeScaleRevert() public {
        JM.Inputs memory p = _example();
        p.selected = p.total + 1;
        vm.expectRevert(JM.InvalidJubileeInputs.selector);
        this.solve(p);
        p = _example();
        p.fundShares = p.shares + 1;
        vm.expectRevert(JM.InvalidJubileeInputs.selector);
        this.solve(p);
        p = _example();
        p.feeBp = 10001;
        vm.expectRevert(JM.InvalidJubileeInputs.selector);
        this.solve(p);
        p = _example();
        p.assets = type(uint256).max;
        vm.expectRevert(JM.JubileeValueOverflow.selector);
        this.solve(p);
    }

    function testFuzz_leastRootMatchesIndependentExhaustiveIntegerOracle(
        uint256 assets, uint256 principal, uint256 cash, uint256 selected,
        uint256 total, uint256 fundShares, uint256 shares, uint256 basis, uint16 feeBp
    ) public pure {
        assets = bound(assets, 0, 200);
        principal = bound(principal, 0, 300);
        total = bound(total, 1, 1000);
        selected = bound(selected, 0, total);
        shares = bound(shares, 1, 1000);
        fundShares = bound(fundShares, 0, shares);
        basis = bound(basis, 0, 300);
        cash = bound(cash, 0, 30000); // hundredths of one raw BUCK
        feeBp = uint16(bound(feeBp, 0, 10000));
        JM.Inputs memory p = JM.Inputs(assets, principal, cash * Q / 100,
            selected, total, fundShares, shares, basis, feeBp);
        (uint256 equity, uint256 relief) = JM.solve(p);
        // Small direct integer products, no production helper or bisection.
        for (uint256 e; e <= assets; ++e) {
            uint256 mark = e * fundShares / shares;
            if (mark > basis) mark -= (mark - basis) * feeBp / 10000;
            uint256 r = (cash + mark * 100) * selected / total / 100;
            if (r > principal) r = principal;
            uint256 rhs = assets + r > principal ? assets + r - principal : 0;
            if (e != rhs) continue;
            assertEq(equity, e);
            assertEq(relief, r);
            return;
        }
        revert("no integer root");
    }
}
