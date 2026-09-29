// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "../src/BN254.sol";
import {Buck} from "../src/Buck.sol";
import {BuckWithBasketHooks} from "../src/legacy/BuckWithBasketHooks.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {BuckJubileeReliefTest} from "./BuckJubileeRelief.t.sol";
import {bindCarryingPool} from "./harness/CarryingPool.sol";
import {BuckSlots} from "./harness/BuckSlots.sol";

/// @title JubileeBasis.t.sol -- the Jubilee's two sides balance.
///
/// The fund takes in 2%/yr of the BUCK issued; relief pays back exactly that,
/// 2%/yr of the BUCK each issuer actually put into circulation; and every
/// BUCK carries its own fee to the end.  Each test here was a
/// test_defect_* that pinned the old behaviour (doc/JUBILEE-ISSUANCE.org,
/// section 1, commit e177c79); each now pins the fix.
///
/// Inherits BuckJubileeReliefTest's fixture: alice holds a zero-premium
/// credit of FACE = 1000, fully activated, and has drawn DRAW = 100 into bob
/// (a public, Carrying account).  BUCK_K = 1.
contract JubileeBasisTest is BuckJubileeReliefTest {

    address internal constant BASKET = address(0xBA5E7);
    address internal constant CAROL  = address(0xCA201);   // public, non-Carrying

    /// The pro-rata baskets' hooks live in the sims' subclass: deploy it,
    /// so the basket cases below test the hooks' side of the Jubilee too.
    function _newBuck() internal override returns (Buck) {
        return new BuckWithBasketHooks(address(credit), address(kCtrl), address(reg), POOL);
    }

    function _hooks() internal view returns (BuckWithBasketHooks) {
        return BuckWithBasketHooks(address(buck));
    }

    function _poke() internal {
        buck.settleDemurrage(bob);          // runs _accrueJubilee
    }

    /// Buck's books: the supply is the liens, the basket's net issuance, and
    /// the relief paid out, less the fees realized; and the fund plus the
    /// positive balances is the sum of every positive raw balance.
    function _booksBalance() internal view {
        int256 supply = int256(buck.totalSupply());
        int256 liens  = int256(_lien(alice));
        assertEq(supply, liens + _hooks().basketIssued() + int256(buck.reliefRealized())
                         - int256(buck.feesRealized()), "supply identity");
        uint256 positive = buck.rawBalanceOf(alice) + buck.rawBalanceOf(bob)
                         + buck.rawBalanceOf(POOL) + buck.rawBalanceOf(BASKET)
                         + buck.rawBalanceOf(CAROL);
        assertEq(positive, buck.totalSupply(), "sum of positive raw == totalSupply");
    }

    // ---- relief accrues on the BUCK issued ----------------------------------

    /// Coverage alice never drew earns nothing: the quote is what the fund
    /// took in from the 100 she did put out.
    function test_undrawnCoverageEarnsNothing() public {
        vm.warp(t0 + YEAR);
        _poke();
        uint256 quote = buck.reliefOf(alice);
        assertApproxEqRel(quote, DRAW * 2 / 100, 0.001e18, "quoted on the 100 drawn");
        assertApproxEqAbs(quote, buck.jubileeActual(), 1, "the fund holds exactly it");
        _booksBalance();
    }

    /// Fully drawn at K < 1, the quote runs on what the credit issued.
    function test_aDrawnCreditEarnsOnItsLien() public {
        vm.prank(GOV);
        kCtrl.setBuckK(0.75e18);
        uint256 limit = FACE * 3 / 4;
        vm.prank(alice);
        buck.transfer(bob, limit - DRAW);                 // draw to the limit
        assertEq(buck.signedRawBalanceOf(alice), -int256(limit), "fully drawn");
        vm.warp(t0 + YEAR);
        _poke();
        uint256 quote = buck.reliefOf(alice);
        assertApproxEqRel(quote, limit * 2 / 100, 0.001e18, "2% of the 750 issued");
        assertApproxEqAbs(quote, buck.jubileeActual(), 1, "the fund holds exactly it");
        _booksBalance();
    }

    /// The fund covers the relief, so settlement pays the whole quote.
    function test_theFundPaysTheWholeRelief() public {
        vm.warp(t0 + YEAR);
        uint256 quote = buck.reliefOf(alice);
        uint256[] memory tids = new uint256[](1);
        tids[0] = tid;
        vm.prank(alice);
        buck.burn(FACE / 2 * 98 / 100, tids);
        assertApproxEqAbs(DRAW - _lien(alice), quote, 1, "paid in full");
        assertEq(buck.reliefRealized(), DRAW - _lien(alice), "and booked");
        assertLt(buck.jubileeActual(), 2, "fund paid out to dust");
        _booksBalance();
    }

    // ---- every BUCK carries its fee to the end -----------------------------

    /// Aged BUCK that repay a lien pay their fee on arrival.
    function test_agedBuckRepayALienNetOfTheirFee() public {
        vm.warp(t0 + YEAR);
        _poke();                                          // fund: 2% of the 100
        uint256 bobFee = buck.feeOwing(bob);
        assertApproxEqRel(bobFee, DRAW * 2 / 100, 0.001e18, "bob's 100 carry a year");
        vm.expectEmit(true, false, false, false, address(buck));
        emit Buck.FeeRealized(alice, 0);
        vm.prank(bob);
        buck.transfer(alice, DRAW / 2);                   // repays half alice's lien
        assertApproxEqAbs(_lien(alice), DRAW / 2 + bobFee / 2, 1,
                          "the lien fell by 50 less their fee of 1");
        assertApproxEqAbs(buck.feesRealized(), bobFee / 2, 1, "the fee, realized");
        assertApproxEqRel(buck.feeOwing(bob), bobFee / 2, 0.001e18, "bob keeps his half");
        // The fund holds alice's relief; the fees are paid or owed in full.
        assertApproxEqAbs(buck.jubileeActual(), buck.reliefOf(alice), 1, "fund == relief owed");
        assertApproxEqAbs(buck.feesRealized() + buck.feeOwing(bob), buck.jubileeActual(), 1,
                          "fees realized + owed == the fund");
        _booksBalance();
    }

    function _basket() internal {
        bindCarryingPool(reg, BASKET);
        vm.etch(CAROL, hex"60006000fd");
        reg.bindContract(CAROL, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, false);
        vm.prank(POOL);
        _hooks().setBasket(BASKET);
    }

    /// An account holding aged BUCK that spends past them into its credit
    /// pays the locked fee first: the lien is exactly the credit drawn.
    function test_aFeeSpentIntoCreditIsPaidFirst() public {
        _basket();
        vm.prank(BASKET);
        _hooks().mintFromBasket(alice, 200e6);                // alice: -100 -> +100, fresh
        vm.warp(t0 + YEAR);
        uint256 fee = buck.feeOwing(alice);
        assertApproxEqRel(fee, 2e6, 0.001e18, "her 100 carry a year: 2");
        vm.expectEmit(true, false, false, true, address(buck));
        emit Buck.FeeRealized(alice, fee);
        vm.prank(alice);
        buck.transfer(bob, 150e6);                        // 98 held + 52 of credit
        assertApproxEqAbs(_lien(alice), 52e6, 1, "the lien is the 52 drawn");
        assertEq(buck.feeOwing(alice), 0, "and nothing is left owing");
        assertApproxEqAbs(buck.feesRealized(), fee, 1, "because it was paid");
        _booksBalance();
    }

    /// The basket burns 900 of 1000 BUCK it held for a year: the burned BUCK
    /// take their age with them, so the last 100 keep a year's, and the next
    /// recipient of basket BUCK pays only its own.
    function test_burnedBasketBuckTakeTheirAgeWithThem() public {
        _basket();
        vm.prank(BASKET);
        _hooks().mintFromBasket(BASKET, 1000e6);
        vm.warp(t0 + YEAR);
        vm.expectEmit(true, false, false, false, address(buck));
        emit Buck.FeeRealized(BASKET, 0);
        vm.prank(BASKET);
        _hooks().burnFromBasket(900e6);
        assertEq(buck.rawBalanceOf(BASKET), 100e6);
        assertApproxEqRel(buck.feeOwing(BASKET), 2e6, 0.001e18, "100 BUCK carry 2%");
        assertApproxEqRel(buck.feesRealized(), 18e6, 0.001e18, "the burned 900 paid their 18");
        assertApproxEqAbs(_hooks().basketIssued(), int256(1000e6 - (900e6 - 18e6)), 1,
                          "aged BUCK retire 900 less their fee of the basket's issuance");
        vm.prank(BASKET);
        buck.transfer(CAROL, 50e6);
        assertApproxEqRel(buck.feeOwing(CAROL), 1e6, 0.001e18, "carol pays 1 on her 50");
        _booksBalance();
    }

    /// The basket's issuance is recorded: a year of 1000 issued earns 20 of
    /// relief, which the fund accrued -- and holds, until the basket's
    /// relief is realized (JUBILEE-ISSUANCE 6.2).
    function test_theBasketsIssuanceIsRecorded() public {
        _basket();
        vm.prank(BASKET);
        _hooks().mintFromBasket(BASKET, 1000e6);
        vm.warp(t0 + YEAR);
        _poke();
        assertApproxEqRel(_hooks().basketRelief(), int256(20e6), 0.001e18, "2% of 1000");
        assertApproxEqAbs(buck.jubileeActual(),
                          uint256(_hooks().basketRelief()) + buck.reliefOf(alice), 2,
                          "the fund holds the basket's relief and alice's");
        _booksBalance();
    }

    // ---- random sequences ---------------------------------------------------

    /// Plant a receipt fragment so private alice can transfer with `other`
    /// in both directions (Buck's `_receiptFragments[alice][other]`, slot 5).
    function _frag(address other) internal {
        bytes32 slot = BuckSlots.fragment(alice, other);
        vm.store(address(buck), slot, bytes32(uint256(1)));
    }

    function _pick3(uint256 r, address a, address b, address c) internal pure returns (address) {
        uint256 k = r % 3;
        return k == 0 ? a : (k == 1 ? b : c);
    }

    /// I1: a zero balance holds no seconds, so the seconds' meaning (fee-
    /// seconds above zero, issuance-seconds below) is given by the sign.
    /// Read from the packed account word: balance int80 (bits 0..79),
    /// buckSeconds uint120 (bits 80..199).
    function _assertI1(address a) internal view {
        uint256 w = uint256(vm.load(address(buck), BuckSlots.state(a)));
        int80 bal = int80(uint80(w));
        uint256 bs = uint120(w >> 80);
        if (bal == 0) assertEq(bs, 0, "I1: a zero balance holds no seconds");
    }

    /// The one path that left seconds at a zero balance: spending exactly
    /// all held BUCK when their fee rounds to nothing (100 BUCK held a
    /// second).  The writer now clears them, so a later draw's issuance-
    /// seconds start clean.
    function test_I1_spendingDownToZeroLeavesNoSeconds() public {
        _basket();
        vm.prank(BASKET);
        _hooks().mintFromBasket(alice, 200e6);            // alice: -100 -> +100, fresh
        vm.warp(block.timestamp + 1);
        assertEq(buck.feeOwing(alice), 0, "a second's fee rounds to nothing");
        vm.prank(alice);
        buck.transfer(bob, 100e6);                        // exactly all she holds
        assertEq(buck.signedRawBalanceOf(alice), 0);
        _assertI1(alice);
        vm.prank(alice);
        buck.transfer(bob, 100e6);                        // now draw 100 of credit
        vm.warp(block.timestamp + 365 days + 6 hours);
        assertApproxEqRel(buck.reliefOf(alice), 2e6, 0.001e18,
                          "relief on the new lien alone: 2% of 100 for the year");
    }

    /// Random draws, repayments (fresh and aged), basket mints, burns and
    /// payouts, coverage burns, relief settlements and time: after every
    /// step the supply identity holds, the positive balances are the supply,
    /// and the fund holds at least the relief it owes.
    function testFuzz_theJubileeBalances(uint256 seed) public {
        _basket();
        _frag(CAROL);
        _frag(BASKET);
        uint256 ctid = credit.createCredit(
            CAROL, 0, FACE, FACE, BuckCredit.DepreciationType.NONE, 0, 0, 0);
        vm.prank(CAROL);
        buck.mint(FACE);

        for (uint256 step = 0; step < 32; step++) {
            uint256 r = uint256(keccak256(abi.encode(seed, step)));
            uint256 r2 = r >> 8;
            uint256 op = r % 8;
            if (op == 0) {
                vm.warp(block.timestamp + r2 % 200 days);
            } else if (op == 1) {                                   // draw / spend
                address from = r2 % 2 == 0 ? alice : CAROL;
                address to   = _pick3(r2 >> 8, bob, BASKET, from == alice ? CAROL : alice);
                uint256 amt  = (r2 >> 16) % (buck.balanceOf(from) + 1);
                vm.prank(from);
                try buck.transfer(to, amt) {} catch {}
            } else if (op == 2) {                                   // aged BUCK from bob
                address to = _pick3(r2, alice, CAROL, BASKET);
                uint256 amt = (r2 >> 8) % (buck.rawBalanceOf(bob) + 1);
                vm.prank(bob);
                try buck.transfer(to, amt) {} catch {}
            } else if (op == 3) {                                   // basket mint
                address to = _pick3(r2, BASKET, BASKET, alice);
                vm.prank(BASKET);
                _hooks().mintFromBasket(to, (r2 >> 8) % 500e6);
            } else if (op == 4) {                                   // basket burn
                uint256 amt = r2 % (buck.rawBalanceOf(BASKET) + 1);
                vm.prank(BASKET);
                _hooks().burnFromBasket(amt);
            } else if (op == 5) {                                   // basket pays out
                address to = _pick3(r2, alice, CAROL, bob);
                uint256 amt = (r2 >> 8) % (buck.rawBalanceOf(BASKET) + 1);
                vm.prank(BASKET);
                try buck.transfer(to, amt) {} catch {}
            } else if (op == 6) {                                   // settle relief
                vm.prank(r2 % 2 == 0 ? alice : CAROL);
                buck.settleRelief();
            } else {                                                // burn coverage
                address who = r2 % 2 == 0 ? alice : CAROL;
                uint256[] memory ids = new uint256[](1);
                ids[0] = who == alice ? tid : ctid;
                vm.prank(who);
                try buck.burn((r2 >> 8) % 300e6, ids) {} catch {}
            }

            _poke();
            int256 liens = int256(_lien(alice) + _lien(CAROL));
            assertEq(int256(buck.totalSupply()),
                     liens + _hooks().basketIssued() + int256(buck.reliefRealized())
                           - int256(buck.feesRealized()), "supply identity");
            uint256 positive = buck.rawBalanceOf(alice) + buck.rawBalanceOf(bob)
                             + buck.rawBalanceOf(POOL) + buck.rawBalanceOf(BASKET)
                             + buck.rawBalanceOf(CAROL);
            assertEq(positive, buck.totalSupply(), "positive raw == totalSupply");
            int256 br = _hooks().basketRelief();
            uint256 owed = buck.reliefOf(alice) + buck.reliefOf(CAROL) + (br > 0 ? uint256(br) : 0);
            assertGe(buck.jubileeActual() + step + 2, owed, "the fund holds the relief it owes");
            _assertI1(alice); _assertI1(bob); _assertI1(CAROL); _assertI1(BASKET); _assertI1(POOL);
        }
    }

    // ---- the fund accrues each period at that period's issuance ------------

    /// A transfer that draws credit checkpoints the fund first: a year at
    /// 100, then a draw of 900, accrues a year at 100 -- and the next year
    /// at 1000.
    function test_theFundAccruesAtEachPeriodsIssuance() public {
        vm.warp(t0 + YEAR);
        vm.prank(alice);
        buck.transfer(bob, 900e6);                        // issued: 100 -> 1000
        _poke();
        assertApproxEqRel(buck.jubileeActual(), DRAW * 2 / 100, 0.001e18,
                          "a year on the 100 that circulated");
        vm.warp(t0 + 2 * YEAR);
        _poke();
        assertApproxEqRel(buck.jubileeActual(), DRAW * 2 / 100 + 1000e6 * 2 / 100, 0.001e18,
                          "then a year on 1000");
        assertApproxEqAbs(buck.jubileeActual(), buck.reliefOf(alice), 2, "== the relief owed");
        _booksBalance();
    }
}
