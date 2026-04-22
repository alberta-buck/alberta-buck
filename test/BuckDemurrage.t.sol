// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";

/// @title BuckDemurrage.t.sol -- demurrage / Jubilee / transferCarrying invariants.
///
/// Flat-rate cumulative-index model: cumIndex grows linearly at BASE_RATE_PER_SEC,
/// so there is no time-quantization error and no long-idle overshoot.  Jubilee
/// is a Carrying account that grows via advance-mint on every _update toward
/// BASE_RATE * integral(totalSupply dt).  Fees on Deducting transfers are
/// BURNED (totalSupply drops); Jubilee growth is independent of individual
/// account burns.
contract BuckDemurrageTest is Test {

    Buck                  internal buck;
    BuckCredit            internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;

    address internal constant GOV     = address(0xA0);
    address internal constant ISSUER  = address(0x1551E1);
    address internal constant POOL    = address(0xBA51C);

    address internal alice;
    address internal bob;

    string internal vj;

    function setUp() public {
        vm.chainId(1);
        vj = vm.readFile("test/vectors/identity.json");

        reg = new IdentityRegistry(GOV);
        _trustIssuer();
        alice = address(uint160(_u(".alice.registrant")));
        bob   = address(uint160(_u(".bob.registrant")));
        _registerAlice();
        _registerBob();

        credit = new BuckCredit();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
    }

    // ---- JSON / identity helpers (copied from Buck.t.sol) ------------------

    function _u(string memory key) internal view returns (uint256) {
        return vm.parseJsonUint(vj, key);
    }

    function _g1(string memory key) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point(_u(string.concat(key, ".x")), _u(string.concat(key, ".y")));
    }

    function _ps(string memory who) internal view returns (IdentityRegistry.PSSig memory s) {
        s.sigma_1 = _g1(string.concat(".", who, ".ps_sig_rerand.sigma_1"));
        s.sigma_2 = _g1(string.concat(".", who, ".ps_sig_rerand.sigma_2"));
    }

    function _ct(string memory key) internal view returns (IdentityRegistry.ElGamalCT memory c) {
        c.R = _g1(string.concat(key, ".R"));
        c.C = _g1(string.concat(key, ".C"));
    }

    function _regProof(string memory who) internal view returns (IdentityRegistry.RegistrationProof memory p) {
        string memory base = string.concat(".", who, ".registration_proof");
        p.e    = _u(string.concat(base, ".e"));
        p.s_m  = _u(string.concat(base, ".s_m"));
        p.s_r  = _u(string.concat(base, ".s_r"));
        p.A_ps = _g1(string.concat(base, ".A_ps"));
        p.T_C  = _g1(string.concat(base, ".T_C"));
        p.T_R  = _g1(string.concat(base, ".T_R"));
    }

    function _cpProof() internal view returns (IdentityRegistry.CPProof memory p) {
        p.e  = _u(".approve.cp_proof.e");
        p.s1 = _u(".approve.cp_proof.s1");
        p.s2 = _u(".approve.cp_proof.s2");
        p.T1 = _g1(".approve.cp_proof.T1");
        p.T2 = _g1(".approve.cp_proof.T2");
        p.T3 = _g1(".approve.cp_proof.T3");
    }

    function _trustIssuer() internal {
        IdentityRegistry.PSPubKey memory ipk;
        ipk.X.X[0] = _u(".issuer.pk_X.x[0]");
        ipk.X.X[1] = _u(".issuer.pk_X.x[1]");
        ipk.X.Y[0] = _u(".issuer.pk_X.y[0]");
        ipk.X.Y[1] = _u(".issuer.pk_X.y[1]");
        ipk.Y.X[0] = _u(".issuer.pk_Y.x[0]");
        ipk.Y.X[1] = _u(".issuer.pk_Y.x[1]");
        ipk.Y.Y[0] = _u(".issuer.pk_Y.y[0]");
        ipk.Y.Y[1] = _u(".issuer.pk_Y.y[1]");
        vm.prank(GOV);
        reg.trustIssuer(ISSUER, ipk);
    }

    function _registerAlice() internal {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        vm.prank(alice);
        reg.register(ISSUER, pk, E, _ps("alice"), _regProof("alice"));
    }

    function _registerBob() internal {
        BN254.G1Point memory pk = _g1(".bob.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".bob.ciphertext");
        vm.prank(bob);
        reg.register(ISSUER, pk, E, _ps("bob"), _regProof("bob"));
    }

    function _grantCredit(address client, uint256 faceValue) internal {
        uint256 tokenId = credit.createCredit(
            client, 0, faceValue, faceValue,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(client);
        credit.activate(tokenId, faceValue);
    }

    function _setupAliceWithBuck(uint256 face, uint256 mintAmt) internal {
        _grantCredit(alice, face);
        vm.prank(alice);
        buck.mint(mintAmt);
    }

    function _approveBobMax() internal {
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        vm.prank(alice);
        buck.approve(bob, type(uint256).max, E_b, _cpProof());
    }

    function _bobPublic() internal {
        vm.prank(bob);
        reg.setPublic(true);
    }

    // ---- baseline behaviour -----------------------------------------------

    function test_demurrage_zeroAtMint() public {
        _setupAliceWithBuck(1000e18, 100e18);
        // Same block as mint: no time has elapsed since the index was bumped, so
        // alice's lastTouch equals cumIndex.
        assertEq(buck.feeOwing(alice), 0, "fee should be 0 same block as mint");
        assertEq(buck.feeOwing(POOL),  0, "POOL fee should be 0 same block as mint");
        assertEq(buck.jubileeActual(), 0, "no fees burned yet");
    }

    function test_demurrage_jubileeIsCarryingAccount() public {
        // Jubilee participates in demurrage: after it accrues a balance, it
        // owes fee on that balance like any other Carrying account.
        _setupAliceWithBuck(1000e18, 100e18);
        _bobPublic();

        // Warp and transfer to trigger an _update, which advance-mints to Jubilee.
        vm.warp(block.timestamp + 1 hours);
        vm.prank(alice);
        buck.transfer(bob, 1e18);

        uint256 jubRaw1 = buck.rawBalanceOf(address(buck));
        assertGt(jubRaw1, 0, "Jubilee has been advance-minted");

        // Right after the mint, Jubilee's fee owing on the fresh delta is 0
        // (fresh BUCKs at age 0).  Any pre-existing debt is preserved.  Since
        // this is the first Jubilee mint, total fee == 0.
        assertEq(buck.feeOwing(address(buck)), 0, "first Jubilee mint: fresh BUCKs at age 0");

        // Warp again without updating: Jubilee now owes fee on its balance.
        vm.warp(block.timestamp + 1 hours);
        uint256 jubFee = buck.feeOwing(address(buck));
        assertGt(jubFee, 0, "Jubilee owes fee after dormant period");
    }

    function test_demurrage_balanceOfReflectsFee() public {
        _setupAliceWithBuck(1000e18, 100e18);
        uint256 balAtMint = buck.balanceOf(alice);

        vm.warp(block.timestamp + 1 hours);
        uint256 fee = buck.feeOwing(alice);
        assertGt(fee, 0, "fee must accrue after warp");
        assertEq(buck.balanceOf(alice), balAtMint - fee, "balanceOf == raw - fee");
    }

    function test_demurrage_jubileeTargetTracksAreaUnderSupply() public {
        _setupAliceWithBuck(1000e18, 100e18);
        uint256 supply = buck.totalSupply();

        // After 1 day: target ~= supply * (1day/365.25day) * 0.02
        vm.warp(block.timestamp + 1 days);
        uint256 expected = supply * 2 * 1 days / (uint256(365 days + 6 hours) * 100);
        assertApproxEqRel(buck.jubileeTarget(), expected, 0.001e18, "target ~= supply * day/year * 2%");
    }

    // ---- standard transfer settles fees -----------------------------------

    function test_transfer_burnsSenderFee_andAdvanceMintsJubilee() public {
        _setupAliceWithBuck(1000e18, 100e18);
        _bobPublic();

        vm.warp(block.timestamp + 1 hours);

        uint256 supplyBefore = buck.totalSupply();
        uint256 jubBefore    = buck.jubileeActual();
        uint256 feeAlice     = buck.feeOwing(alice);
        uint256 feePool      = buck.feeOwing(POOL);
        uint256 target       = buck.jubileeTarget();
        assertGt(feeAlice, 0, "Alice has accrued fee");
        assertEq(jubBefore, 0, "Jubilee not yet minted");

        vm.prank(alice);
        buck.transfer(bob, 1e18);

        // 1) Jubilee advance-minted up to target (independent of fee burns).
        uint256 jubAfter = buck.jubileeActual();
        assertApproxEqAbs(jubAfter, target, 1, "Jubilee == target after update");

        // 2) Alice's fee was BURNED (totalSupply dropped by fee, rose by Jubilee mint).
        uint256 supplyAfter = buck.totalSupply();
        uint256 expectedDelta = int256(jubAfter) > int256(feeAlice)
            ? jubAfter - feeAlice
            : 0;
        assertApproxEqAbs(supplyAfter, supplyBefore + expectedDelta, 2, "supply = prior + jub_mint - alice_fee");

        // 3) POOL was not touched; its fee debt persists.
        assertGe(buck.feeOwing(POOL), feePool, "POOL fee unchanged by Alice/Bob transfer");

        // 4) Alice's index reset: her fee owing on the remaining balance is 0.
        assertEq(buck.feeOwing(alice), 0, "Alice's fee owing reset after settle");
    }

    function test_transfer_remainingBalanceStartsFreshClock() public {
        _setupAliceWithBuck(1000e18, 100e18);
        _bobPublic();

        vm.warp(block.timestamp + 1 hours);

        // First transfer pays Alice's fee on her FULL balance.
        vm.prank(alice);
        buck.transfer(bob, 1e18);

        // Right after settle: Alice's fee owing is 0 even though she still holds BUCK.
        assertEq(buck.feeOwing(alice), 0, "post-settle fee = 0");

        // Warp again; new fee accrues only on the now-smaller remaining balance.
        vm.warp(block.timestamp + 1 hours);
        uint256 newFee = buck.feeOwing(alice);
        assertGt(newFee, 0, "fee resumes accruing on remaining balance");
    }

    // ---- transferCarrying invariants --------------------------------------

    function test_transferCarrying_noExtraJubileeTip() public {
        // transferCarrying triggers the normal Jubilee advance-mint but MUST
        // NOT burn any extra fee from Alice to Jubilee.  After the carrying
        // transfer, Jubilee should equal exactly jubileeTarget at this block.
        _setupAliceWithBuck(1000e18, 100e18);
        _bobPublic();

        vm.warp(block.timestamp + 1 hours);
        uint256 target = buck.jubileeTarget();

        vm.prank(alice);
        buck.transferCarrying(bob, 10e18);

        assertApproxEqAbs(buck.jubileeActual(), target, 1,
            "Jubilee == target; no extra burn from Alice");
    }

    function test_transferCarrying_preservesTotalSpendable() public {
        _setupAliceWithBuck(1000e18, 100e18);
        _bobPublic();

        vm.warp(block.timestamp + 1 hours);

        uint256 sumBefore = buck.balanceOf(alice) + buck.balanceOf(bob);
        vm.prank(alice);
        buck.transferCarrying(bob, 10e18);
        uint256 sumAfter = buck.balanceOf(alice) + buck.balanceOf(bob);

        // Net spendable across alice+bob is preserved (within rounding from the
        // weighted-average index merge).
        assertApproxEqAbs(sumAfter, sumBefore, 10, "alice+bob spendable preserved");
    }

    function test_transferCarrying_recipientAbsorbsCarriedAge() public {
        _setupAliceWithBuck(1000e18, 100e18);
        _bobPublic();

        vm.warp(block.timestamp + 1 hours);

        // Pre-condition: bob has no balance, no fee.
        assertEq(buck.balanceOf(bob), 0, "bob starts empty");
        assertEq(buck.feeOwing(bob),  0, "bob has no fee debt");

        vm.prank(alice);
        buck.transferCarrying(bob, 10e18);

        // Bob now holds 10 BUCK with carried age -> non-zero feeOwing
        // immediately, equal to what alice would have owed on her 10-BUCK slice.
        uint256 bobFee = buck.feeOwing(bob);
        assertGt(bobFee, 0, "bob inherits carried fee debt");

        // Sanity: bob's balanceOf < 10 (some debt is already owed).
        assertLt(buck.balanceOf(bob), 10e18, "bob's spendable reduced by carried fee");
    }

    function test_transferCarrying_senderRemainingKeepsOldAge() public {
        _setupAliceWithBuck(1000e18, 100e18);
        _bobPublic();

        vm.warp(block.timestamp + 1 hours);
        uint256 aliceFeeBefore = buck.feeOwing(alice);

        vm.prank(alice);
        buck.transferCarrying(bob, 10e18);

        // Alice's index was NOT reset by carrying.  Her fee owing now reflects
        // the same age basis applied to the smaller remaining balance.
        uint256 aliceFeeAfter = buck.feeOwing(alice);

        // alice_fee scales linearly with raw balance.  raw went from
        // (mintAmount - premium) to (mintAmount - premium - 10e18).
        // Let raw_old = aliceFeeBefore / per_unit_fee.  Ratio:
        //   aliceFeeAfter / aliceFeeBefore == (raw_old - 10e18) / raw_old
        // Avoid recomputing per_unit; just check ratio approximately.
        // Mint amount 100e18, premium ~0.5e18 -> raw_old ~99.5e18.
        // After carry: raw_new ~89.5e18.  Ratio ~0.8995.
        uint256 expectedAfter = aliceFeeBefore * 895 / 995;
        assertApproxEqRel(aliceFeeAfter, expectedAfter, 0.01e18, "alice fee scales with remaining balance");
    }

    function test_transferCarrying_systemFeeDebtPreserved() public {
        _setupAliceWithBuck(1000e18, 100e18);
        _bobPublic();

        vm.warp(block.timestamp + 1 hours);

        uint256 totalDebtBefore = buck.feeOwing(alice) + buck.feeOwing(bob);
        vm.prank(alice);
        buck.transferCarrying(bob, 10e18);
        uint256 totalDebtAfter  = buck.feeOwing(alice) + buck.feeOwing(bob);

        // Mathematically exact; allow 1 wei tolerance for integer rounding in
        // the weighted-average merge.
        assertApproxEqAbs(totalDebtAfter, totalDebtBefore, 10, "system fee debt preserved");
    }

    function test_transferCarrying_thenStandardTransferBurnsCarriedFee() public {
        _setupAliceWithBuck(1000e18, 100e18);
        _bobPublic();

        vm.warp(block.timestamp + 1 hours);

        vm.prank(alice);
        buck.transferCarrying(bob, 10e18);

        uint256 bobFeeOwed = buck.feeOwing(bob);
        assertGt(bobFeeOwed, 0, "bob inherited carried fee");

        // Make alice public so bob can transfer to her without a receipt fragment.
        vm.prank(alice);
        reg.setPublic(true);

        uint256 supplyBefore = buck.totalSupply();
        vm.prank(bob);
        buck.transfer(alice, 1e18);

        // Bob's carried fee was BURNED (totalSupply dropped by bob's fee).
        // Same-block as prior transfer, so Jubilee advance-mint delta is 0.
        uint256 supplyAfter = buck.totalSupply();
        assertEq(supplyBefore - supplyAfter, bobFeeOwed, "supply dropped by bob's carried fee");
        assertEq(buck.feeOwing(bob), 0, "bob's index reset after settle");
    }

    // ---- long-idle behaviour ----------------------------------------------

    /// @notice Flat-rate cumulative-index model: after 1 year of idle with no
    ///         updates, total fee owed is ~2 percent of totalSupply, within
    ///         integer-rounding tolerance.  (Replaces the dynamic-rate
    ///         overshoot test from the old model.)
    function test_longIdle_feeTrackBaseRate() public {
        _setupAliceWithBuck(1000e18, 100e18);

        vm.warp(block.timestamp + 365 days);

        uint256 supply    = buck.totalSupply();
        // Expected: supply * BASE_RATE_PER_SEC * elapsed / SCALE.
        // BASE_RATE_PER_SEC = 2e25 / (365.25 days), so 365 days elapsed gives
        // ~(365/365.25) * 0.02 ~= 0.019986.
        uint256 expected  = supply * 2e25 * 365 days / (uint256(365 days + 6 hours) * 1e27);
        uint256 actualOwed = buck.feeOwing(alice) + buck.feeOwing(POOL);

        assertApproxEqRel(actualOwed, expected, 0.001e18, "flat-rate integral matches");
    }

    /// @notice Jubilee accrual catches up on any update, no matter how long
    ///         every account lies dormant.  The integral target grows
    ///         continuously; the actual balance catches up on first activity.
    function test_dormantAccount_caughtUpByActivity() public {
        _setupAliceWithBuck(1000e18, 100e18);
        _bobPublic();

        // Alice is dormant for a full year.
        vm.warp(block.timestamp + 365 days);

        // Target grew continuously; actual is still 0.
        uint256 target = buck.jubileeTarget();
        assertGt(target, 0, "target grew during dormant period");
        assertEq(buck.jubileeActual(), 0, "actual still 0 before any update");

        // Any update catches Jubilee up.
        vm.prank(alice);
        buck.transfer(bob, 1e18);

        assertApproxEqAbs(buck.jubileeActual(), target, 2, "Jubilee caught up on first activity");
    }
}
