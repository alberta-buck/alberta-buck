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
        // Bob acts as a Public-Identity counterparty contract throughout the
        // demurrage tests (no CP-proof receipts are exchanged here -- the
        // tests focus on demurrage / Jubilee / transferCarrying mechanics).
        bob   = address(uint160(_u(".bob.registrant")));
        _registerAlice();
        _bindPublicPool(bob);

        credit = new BuckCredit();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));

        // Mutual decryptability: private EOA Alice must CP-approve the
        // public-identity contract Bob so the operator can decrypt Alice's
        // identity from any transfer receipt.
        bytes32 slot = keccak256(abi.encode(bob, keccak256(abi.encode(alice, uint256(5)))));
        vm.store(address(buck), slot, bytes32(uint256(1)));
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

    /// @dev Plant minimal contract bytecode at `target` (so bindContract's
    ///      code.length check passes), then bind a placeholder Public Identity.
    function _bindPublicPool(address target) internal {
        vm.etch(target, hex"60006000fd");
        BN254.G1Point memory pk = BN254.g1();
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT({
            R: BN254.g1(),
            C: BN254.g1()
        });
        reg.bindContract(target, pk, E, true, true);
    }

    // ---- baseline ---------------------------------------------------------

    function test_demurrage_zeroAtMint() public {
        _setupAliceWithBuck(1000e6, 100e6);
        // Same block as mint: alice's _timestamp == now, _demurrage == 0.
        assertEq(buck.feeOwing(alice),     0, "fee 0 same block as mint");
        assertEq(buck.balanceOfFees(alice), 0, "no locked dust at mint");
        // POOL just received the premium -- crystallized, _demurrage == 0,
        // _timestamp == now -- so its fee is also 0.
        assertEq(buck.feeOwing(POOL),      0, "POOL fee 0 same block");
        // Jubilee got 0 from accrual (elapsed == 0 since contract construction
        // assuming no warp before mint).
        assertEq(buck.jubileeActual(),     0, "Jubilee not yet accrued");
    }

    function test_demurrage_balanceOfReflectsFee() public {
        _setupAliceWithBuck(1000e6, 100e6);
        uint256 balAtMint = buck.balanceOf(alice);

        vm.warp(block.timestamp + 1 hours);
        uint256 fee = buck.feeOwing(alice);
        assertGt(fee, 0, "fee accrues after warp");
        assertEq(buck.balanceOf(alice),     balAtMint - fee, "balanceOf == raw - fee");
        assertEq(buck.balanceOfFees(alice), fee,             "balanceOfFees == fee (uncapped)");
    }

    function test_longIdle_feeTrackBaseRate() public {
        _setupAliceWithBuck(1000e6, 100e6);

        vm.warp(block.timestamp + 365 days);

        uint256 supply    = buck.totalSupply();
        // Expected aggregate locked fees ~= supply * (365/365.25) * 0.02.
        uint256 expected  = supply * 2e25 * 365 days / (uint256(365 days + 6 hours) * 1e27);
        uint256 ownedFees = buck.feeOwing(alice) + buck.feeOwing(POOL);

        assertApproxEqRel(ownedFees, expected, 0.001e18, "flat-rate integral matches");
    }

    // ---- Jubilee accrual at mint/burn -------------------------------------

    function test_jubilee_growsAtMint() public {
        // After 1 hour of idle holding, a follow-up mint accrues Jubilee by
        // approximately rate * old_supply * 1 hour.
        _setupAliceWithBuck(1000e6, 100e6);
        uint256 oldSupply = buck.totalSupply();

        vm.warp(block.timestamp + 1 hours);
        uint256 jubBefore = buck.jubileeActual();
        assertEq(jubBefore, 0, "no accrual yet");

        vm.prank(alice);
        buck.mint(1e6);

        // delta ~= old_supply * RATE_PER_SEC * 3600s / SCALE
        uint256 expected = oldSupply * 2e25 * 1 hours / (uint256(365 days + 6 hours) * 1e27);
        assertApproxEqAbs(buck.jubileeActual(), expected, 2, "Jubilee credited at mint");
    }

    function test_jubilee_growsAtBurn() public {
        _setupAliceWithBuck(1000e6, 100e6);
        uint256 oldSupply = buck.totalSupply();

        vm.warp(block.timestamp + 1 hours);
        uint256 jubBefore = buck.jubileeActual();
        assertEq(jubBefore, 0, "no accrual yet");

        // Burn a small amount that fits inside alice's spendable.
        vm.prank(alice);
        buck.burn(1e6);

        uint256 expected = oldSupply * 2e25 * 1 hours / (uint256(365 days + 6 hours) * 1e27);
        assertApproxEqAbs(buck.jubileeActual(), expected, 2, "Jubilee credited at burn");
    }

    function test_jubilee_idleBetweenMintBurn() public {
        // Plain transfers do NOT trigger Jubilee accrual.  Jubilee actual
        // stays at whatever it was after the most recent mint/burn.
        _setupAliceWithBuck(1000e6, 100e6);

        vm.warp(block.timestamp + 1 hours);
        vm.prank(alice);
        buck.mint(1e6);  // first accrual
        uint256 jubAfterMint = buck.jubileeActual();
        assertGt(jubAfterMint, 0, "first mint accrued");

        vm.warp(block.timestamp + 1 hours);
        // alice (non-carrying) transfer to bob: no accrual.
        vm.prank(alice);
        buck.transfer(bob, 1e6);
        assertEq(buck.jubileeActual(), jubAfterMint, "non-carrying transfer no accrue");

        // bob is a Carrying contract; bob -> alice goes through carrying path.
        vm.warp(block.timestamp + 1 hours);
        vm.prank(bob);
        buck.transfer(alice, 1);
        assertEq(buck.jubileeActual(), jubAfterMint, "carrying transfer no accrue");
    }

    function test_jubilee_accruesItsOwnDemurrage() public {
        // Once Jubilee holds raw, it starts accruing its own self-demurrage
        // -- treated as any other (Carrying) account.  jubileeBalance() is
        // raw - feeOwing.  At 6-decimal scale, short time intervals would
        // round Jubilee's tiny accrual to zero -- use 30-day windows so the
        // accumulated fee is observable.
        _setupAliceWithBuck(1000e6, 100e6);

        vm.warp(block.timestamp + 30 days);
        vm.prank(alice);
        buck.mint(1e6);  // credits Jubilee with ~0.16 BUCK at age 0

        uint256 jubRaw = buck.rawBalanceOf(address(buck));
        assertGt(jubRaw, 0, "Jubilee has raw");
        // Same block -> _timestamp[jubilee] == now -> feeOwing == 0.
        assertEq(buck.feeOwing(address(buck)), 0, "fresh Jubilee credit at age 0");

        vm.warp(block.timestamp + 30 days);
        // Now Jubilee has accrued self-demurrage on its raw.
        uint256 jubFee = buck.feeOwing(address(buck));
        assertGt(jubFee, 0, "Jubilee accrues its own fees on idle raw");
        assertEq(buck.balanceOf(address(buck)), jubRaw - jubFee, "balanceOf reflects fee");
    }

    // ---- Non-Carrying transfer (alice -> bob) -----------------------------

    function test_nonCarrying_recipientGetsFreshBUCKs() public {
        // Alice (EOA, isCarrying=false) -> Bob (EOA): non-carrying path.
        // Bob receives BUCK with no inherited fee debt.
        _setupAliceWithBuck(1000e6, 100e6);

        vm.warp(block.timestamp + 1 hours);
        assertEq(buck.feeOwing(bob), 0, "bob starts at 0");

        vm.prank(alice);
        buck.transfer(bob, 10e6);

        // Bob's rawBalance = 10e6 and his _timestamp == now -> 0 pending.
        assertEq(buck.feeOwing(bob),  0,    "bob has no inherited fee");
        assertEq(buck.balanceOf(bob), 10e6, "bob spendable == full transferred amount");
    }

    function test_nonCarrying_senderKeepsLockedFees() public {
        // Alice's locked fees stay in her account after a non-carrying
        // outflow.  _demurrage[alice] grows on crystallization.
        _setupAliceWithBuck(1000e6, 100e6);

        vm.warp(block.timestamp + 1 hours);
        uint256 feeBeforeTransfer = buck.feeOwing(alice);
        assertGt(feeBeforeTransfer, 0, "alice has fees");

        vm.prank(alice);
        buck.transfer(bob, 10e6);

        // Right after the transfer alice's pending elapsed is 0; her
        // _demurrage holds the same fee value she had pre-transfer.
        assertApproxEqAbs(buck.feeOwing(alice), feeBeforeTransfer, 1,
            "alice's fees crystallized but preserved");
    }

    function test_nonCarrying_spendableCapEnforced() public {
        // Alice's spendable is balanceOf(alice) = raw - feeOwing.  Trying to
        // transfer more than that reverts.
        _setupAliceWithBuck(1000e6, 100e6);

        vm.warp(block.timestamp + 1 hours);
        uint256 spendable = buck.balanceOf(alice);
        assertGt(buck.rawBalanceOf(alice), spendable, "raw > spendable after warp");

        vm.prank(alice);
        vm.expectRevert(bytes("BUCK: amount exceeds spendable"));
        buck.transfer(bob, spendable + 1);

        vm.prank(alice);
        buck.transfer(bob, spendable);  // exact spendable should succeed
    }

    // ---- Carrying transfer (POOL -> bob) ----------------------------------

    function test_carrying_recipientAbsorbsAge() public {
        // Pre-load Carrying-source bob via a non-carrying transfer from alice
        // (bob is a Public-Identity Carrying contract bound in setUp).
        _setupAliceWithBuck(1000e6, 100e6);
        vm.prank(alice);
        buck.transfer(bob, 20e6);

        vm.warp(block.timestamp + 1 hours);
        uint256 bobFeeBefore = buck.feeOwing(bob);
        assertGt(bobFeeBefore, 0, "bob has accumulated fees");

        uint256 aliceFeeBefore = buck.feeOwing(alice);

        // bob (Carrying) -> alice: alice's _demurrage absorbs the proportional fee.
        vm.prank(bob);
        buck.transfer(alice, 10e6);

        uint256 aliceFeeAfter = buck.feeOwing(alice);
        // Alice's fee grew by approximately the carried portion.
        // carried ~= bobFeeBefore * (10e6 / 20e6) = bobFeeBefore / 2.
        uint256 expectedCarried = bobFeeBefore / 2;
        assertApproxEqAbs(aliceFeeAfter - aliceFeeBefore, expectedCarried, 10,
            "alice absorbed bob's carried fee on the transferred portion");
    }

    function test_carrying_senderBasisUnchanged() public {
        // bob's _timestamp and basis are NOT advanced by a Carrying outflow.
        // Its residual continues to age from the original basis -- the fee
        // on the residual scales linearly with the smaller raw.
        _setupAliceWithBuck(1000e6, 100e6);
        vm.prank(alice);
        buck.transfer(bob, 20e6);
        uint256 bobRaw = buck.rawBalanceOf(bob);

        vm.warp(block.timestamp + 1 hours);
        uint256 bobFeeBefore = buck.feeOwing(bob);
        assertGt(bobFeeBefore, 0, "bob has accumulated fees");

        uint256 amount = 10e6;
        vm.prank(bob);
        buck.transfer(alice, amount);

        // bob residual fee = (bobRaw - amount) / bobRaw * bobFeeBefore.
        uint256 expectedResidualFee = bobFeeBefore * (bobRaw - amount) / bobRaw;
        uint256 bobFeeAfter = buck.feeOwing(bob);
        assertApproxEqAbs(bobFeeAfter, expectedResidualFee, 10,
            "bob residual fee scales with residual raw");
    }

    function test_carrying_canTransferUpToRaw() public {
        // bob (Carrying) is allowed to transfer up to its full raw.  Under
        // Carrying-balanceOf-equals-raw semantics, raw == balanceOf, so
        // there is no separate "spendable" cap to evade -- the accumulated
        // fees ride with the outflow into the recipient's _demurrage rather
        // than being locked inside bob's account.
        _setupAliceWithBuck(1000e6, 100e6);
        vm.prank(alice);
        buck.transfer(bob, 20e6);
        uint256 bobRaw = buck.rawBalanceOf(bob);

        vm.warp(block.timestamp + 1 hours);
        // Carrying account: raw == balanceOf, regardless of warp.
        assertEq(buck.balanceOf(bob), bobRaw, "Carrying balanceOf == raw");
        // Fees are visible separately and have grown.
        assertGt(buck.balanceOfFees(bob), 0, "Carrying balanceOfFees grew");

        // Transfer the full raw -- recipient absorbs the carried fee.
        vm.prank(bob);
        buck.transfer(alice, bobRaw);

        assertEq(buck.rawBalanceOf(bob), 0, "bob drained to 0");
        assertEq(buck.balanceOf(bob),    0, "bob spendable also 0");
    }

    // ---- dispatch by isCarrying flag -------------------------------------

    function test_dispatch_followsIsCarryingFlag() public {
        // Same `transfer` call from two different sender types takes
        // different paths.
        _setupAliceWithBuck(1000e6, 100e6);

        // Pre-load bob (Carrying) with BUCK via a non-carrying transfer.
        vm.prank(alice);
        buck.transfer(bob, 20e6);

        // Bob receives fresh BUCKs (no inherited fee) since alice is non-carrying.
        assertEq(buck.feeOwing(bob), 0, "non-carrying source -> recipient fresh");

        vm.warp(block.timestamp + 1 hours);

        // Bob (Carrying) -> alice: alice absorbs carried fee.
        uint256 aliceFeeBefore = buck.feeOwing(alice);
        vm.prank(bob);
        buck.transfer(alice, 5e6);
        uint256 aliceFeeAfter = buck.feeOwing(alice);
        assertGt(aliceFeeAfter, aliceFeeBefore, "carrying source -> recipient inherits fee");
    }

    // ---- conservation invariants ------------------------------------------

    function test_invariant_balanceOfPlusBalanceOfFees() public {
        // For every account: balanceOf(a) + balanceOfFees(a) == rawBalanceOf(a).
        _setupAliceWithBuck(1000e6, 100e6);

        vm.warp(block.timestamp + 30 days);

        for (uint256 i = 0; i < 3; i++) {
            address a = [alice, POOL, address(buck)][i];
            assertEq(
                buck.balanceOf(a) + buck.balanceOfFees(a),
                buck.rawBalanceOf(a),
                "balanceOf + balanceOfFees == rawBalanceOf"
            );
        }
    }

    function test_invariant_sumRawEqualsTotalSupplyPlusJubileeAccrual() public {
        // Under the packed-state model, totalSupply mutates ONLY on user
        // mint/burn.  Jubilee demurrage accrual writes Jubilee's slot
        // directly without touching totalSupply.  The exact invariant:
        //
        //   sum_a rawBalanceOf(a) == totalSupply + jubileeActual()
        //
        // because the Jubilee's raw is the SOLE source of stored balance
        // that wasn't matched by a totalSupply mutation.
        _setupAliceWithBuck(1000e6, 100e6);

        vm.warp(block.timestamp + 1 hours);
        vm.prank(alice);
        buck.transfer(bob, 5e6);

        vm.warp(block.timestamp + 30 days);
        vm.prank(alice);
        buck.mint(1e6);  // triggers Jubilee accrual

        uint256 sumRaw = buck.rawBalanceOf(alice)
                       + buck.rawBalanceOf(bob)
                       + buck.rawBalanceOf(POOL)
                       + buck.rawBalanceOf(address(buck));
        assertEq(sumRaw, buck.totalSupply() + buck.jubileeActual(),
                 "sum_raw == totalSupply + jubilee accrual");
    }

    function test_invariant_carryingPreservesSystemFeeDebt() public {
        // A Carrying-source transfer preserves sum_a feeOwing(a).
        _setupAliceWithBuck(1000e6, 100e6);

        // Pre-load bob.
        vm.prank(alice);
        buck.transfer(bob, 20e6);

        vm.warp(block.timestamp + 1 hours);

        uint256 sumBefore = buck.feeOwing(alice)
                          + buck.feeOwing(bob)
                          + buck.feeOwing(POOL)
                          + buck.feeOwing(address(buck));

        uint256 amount = buck.rawBalanceOf(bob) / 2;
        vm.prank(bob);
        buck.transfer(alice, amount);

        uint256 sumAfter = buck.feeOwing(alice)
                         + buck.feeOwing(bob)
                         + buck.feeOwing(POOL)
                         + buck.feeOwing(address(buck));

        assertApproxEqAbs(sumAfter, sumBefore, 10,
            "carrying transfer conserves total feeOwing");
    }
}
