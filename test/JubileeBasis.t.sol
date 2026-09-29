// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {BuckJubileeReliefTest} from "./BuckJubileeRelief.t.sol";
import {bindCarryingPool} from "./harness/CarryingPool.sol";

/// @title JubileeBasis.t.sol -- the Jubilee's two sides do not balance today.
///
/// The fund takes in 2%/yr of the BUCK in circulation (totalSupply).  Relief
/// should pay back exactly that: 2%/yr of the BUCK each issuer actually put
/// into circulation.  These tests pin the places where the two sides part
/// company today (doc/JUBILEE-ISSUANCE.org, section 1), and one place the
/// fund's own accrual is not the demurrage the holders owe.  Each asserts the
/// CURRENT behaviour and is named test_defect_*: the fix inverts them.
///
/// Inherits BuckJubileeReliefTest's fixture: alice holds a zero-premium
/// credit of FACE = 1000, fully activated, and has drawn DRAW = 100 into bob
/// (a public, Carrying account).  BUCK_K = 1.
contract JubileeBasisTest is BuckJubileeReliefTest {

    address internal constant BASKET = address(0xBA5E7);
    address internal constant CAROL  = address(0xCA201);   // public, non-Carrying

    function _poke() internal {
        buck.settleDemurrage(bob);          // runs _accrueJubilee
    }

    // ---- relief accrues on coverage, not on BUCK issued --------------------

    /// Coverage alice never drew earns relief all the same: the quote is ten
    /// times what the fund took in from the BUCK she did put out.
    function test_defect_undrawnCoverageEarnsRelief() public {
        vm.warp(t0 + YEAR);
        _poke();
        uint256 quote = credit.jubileeRelief(tid);
        uint256 fund  = buck.jubileeActual();
        assertApproxEqRel(quote, FACE * 2 / 100, 0.001e18, "quoted on the 1000 activated");
        assertApproxEqRel(fund,  DRAW * 2 / 100, 0.001e18, "funded by the 100 drawn");
        assertApproxEqRel(quote, fund * FACE / DRAW, 0.002e18, "10x over the fund");
    }

    /// Fully drawn, at K < 1, the quote still runs on coverage: 1/K of what
    /// the credit could ever issue.
    function test_defect_drawnCreditEarnsOnCoverageNotIssuance() public {
        vm.prank(GOV);
        kCtrl.setBuckK(0.75e18);
        uint256 limit = FACE * 3 / 4;
        vm.prank(alice);
        buck.transfer(bob, limit - DRAW);                 // draw to the limit
        assertEq(buck.signedRawBalanceOf(alice), -int256(limit), "fully drawn");
        vm.warp(t0 + YEAR);
        _poke();
        uint256 quote = credit.jubileeRelief(tid);
        uint256 fund  = buck.jubileeActual();
        assertApproxEqRel(fund, limit * 2 / 100, 0.002e18, "fund: 2% of the 750 issued");
        assertApproxEqRel(quote, FACE * 2 / 100, 0.001e18, "quote: 2% of the 1000 covered");
        assertApproxEqRel(quote * 3 / 4, fund, 0.002e18, "over by 1/K");
    }

    /// When the quote exceeds the fund, settlement pays the fund and the rest
    /// is gone: the coverage-seconds are consumed either way.
    function test_defect_theFundCapForfeitsRelief() public {
        vm.warp(t0 + YEAR);
        uint256 quoteBefore = credit.jubileeRelief(tid);
        int256 rawBefore = buck.signedRawBalanceOf(alice);
        uint256[] memory tids = new uint256[](1);
        tids[0] = tid;
        vm.prank(alice);
        buck.burn(FACE / 2 * 98 / 100, tids);             // unwinds ~half the coverage
        uint256 paid = uint256(buck.signedRawBalanceOf(alice) - rawBefore);
        uint256 quoteAfter = credit.jubileeRelief(tid);
        uint256 carriedOut = quoteBefore - quoteAfter;    // relief the unwind consumed
        assertApproxEqRel(carriedOut, quoteBefore * 49 / 100, 0.02e18,
                          "the unwind carried out its pro-rata relief");
        assertLt(paid, carriedOut / 4, "but the fund paid a fraction of it");
        assertLt(buck.jubileeActual(), 1e3, "fund drained");
    }

    // ---- demurrage carried into a lien is never collected -------------------

    /// Aged BUCK that repay a lien repay it in full: the fee they carry is
    /// parked on an account whose fee reads zero while it is negative.
    function test_defect_ageCarriedIntoALienGoesUncollected() public {
        vm.warp(t0 + YEAR);
        _poke();                                          // fund: 2% of the 100
        uint256 bobFee = buck.feeOwing(bob);
        assertApproxEqRel(bobFee, DRAW * 2 / 100, 0.001e18, "bob's 100 carry a year");
        vm.prank(bob);
        buck.transfer(alice, DRAW / 2);                   // repays half alice's lien
        assertEq(buck.signedRawBalanceOf(alice), -int256(DRAW / 2),
                 "the lien fell by the full 50, not 50 less its fee");
        assertEq(buck.feeOwing(alice), 0, "and alice owes no fee while negative");
        assertApproxEqRel(buck.feeOwing(bob), bobFee / 2, 0.001e18,
                          "bob kept only his half");
        assertApproxEqRel(buck.jubileeActual(),
                          buck.feeOwing(bob) + buck.feeOwing(alice) + bobFee / 2, 0.001e18,
                          "the fund holds a fee nobody now owes");
    }

    /// The mirror case: an account holding aged BUCK that spends past them
    /// into its credit.  The fee locked in its balance is spent as if it were
    /// BUCK -- it shrinks the lien -- and then reads zero while negative.
    function test_defect_aFeeSpentIntoCreditShrinksTheLien() public {
        _basket();
        vm.prank(BASKET);
        buck.mintFromBasket(alice, 200e6);                // alice: -100 -> +100, fresh
        vm.warp(t0 + YEAR);
        uint256 fee = buck.feeOwing(alice);
        assertApproxEqRel(fee, 2e6, 0.001e18, "her 100 carry a year: 2");
        vm.prank(alice);
        buck.transfer(bob, 150e6);                        // 98 held + 52 of credit
        assertEq(buck.signedRawBalanceOf(alice), -50e6,
                 "the lien is 50, though she drew 52 of credit");
        assertEq(buck.feeOwing(alice), 0, "and the fee reads zero");
    }

    // ---- the fund accrues at whatever the supply is at its next checkpoint --

    /// Only mint, burn and the demurrage pokes checkpoint the fund; a
    /// transfer that draws credit (or repays it) changes totalSupply without
    /// one.  So the next checkpoint accrues the whole quiet period at the new
    /// supply: a year at 100, then a draw of 900, accrues a year at 1000.
    function test_defect_supplyChangesDoNotCheckpointTheFund() public {
        vm.warp(t0 + YEAR);
        vm.prank(alice);
        buck.transfer(bob, 900e6);                        // supply 100 -> 1000
        _poke();
        assertApproxEqRel(buck.jubileeActual(), 1000e6 * 2 / 100, 0.001e18,
                          "accrued a year on 1000, where 100 circulated");
        assertApproxEqRel(buck.feeOwing(bob), DRAW * 2 / 100, 0.001e18,
                          "while the holders owe a year on 100");
    }

    // ---- burned basket BUCK leave their age behind -------------------------

    function _basket() internal {
        bindCarryingPool(reg, BASKET);
        vm.etch(CAROL, hex"60006000fd");
        reg.bindContract(CAROL, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, false);
        vm.prank(POOL);
        buck.setBasket(BASKET);
    }

    /// The basket burns 900 of 1000 BUCK it held for a year.  The burned
    /// BUCK's age stays on the basket, so its last 100 read ten years old,
    /// and the next recipient of basket BUCK pays for all of it.
    function test_defect_burnedBasketBuckLeaveTheirAgeBehind() public {
        _basket();
        vm.prank(BASKET);
        buck.mintFromBasket(BASKET, 1000e6);
        vm.warp(t0 + YEAR);
        vm.prank(BASKET);
        buck.burnFromBasket(900e6);
        assertEq(buck.rawBalanceOf(BASKET), 100e6);
        assertApproxEqRel(buck.feeOwing(BASKET), 20e6, 0.001e18,
                          "100 BUCK carry the fee of 1000: 20%, not 2%");
        vm.prank(BASKET);
        buck.transfer(CAROL, 50e6);
        assertApproxEqRel(buck.feeOwing(CAROL), 10e6, 0.001e18,
                          "carol pays 10 on the 50 she received, not 1");
    }
}
