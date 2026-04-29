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
        reg.bindContract(target, pk, E, true);
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

    function test_demurrage_jubileeIsExempt() public {
        // Conservation model: Jubilee never receives a fee transfer and is
        // exempt from per-account demurrage.  feeOwing(jubilee) is always 0
        // and rawBalanceOf(jubilee) is always 0 (modulo any voluntary
        // transfer in, which tests don't perform).
        _setupAliceWithBuck(1000e18, 100e18);

        vm.warp(block.timestamp + 1 hours);
        vm.prank(alice);
        buck.mint(1e18);

        assertEq(buck.rawBalanceOf(address(buck)), 0, "Jubilee never holds raw under conservation");
        assertEq(buck.feeOwing(address(buck)),     0, "Jubilee exempt from demurrage");
        assertEq(buck.balanceOf(address(buck)),    0, "Jubilee balanceOf == 0");
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

    // ---- transfer conserves supply ----------------------------------------

    function test_transfer_conservesTotalSupply() public {
        // Plain transfer never changes totalSupply -- demurrage is purely a
        // view-time computation; no BUCK is burned or moved to Jubilee.
        _setupAliceWithBuck(1000e18, 100e18);

        vm.warp(block.timestamp + 1 hours);

        uint256 supplyBefore = buck.totalSupply();
        uint256 feeAliceBefore = buck.feeOwing(alice);
        assertGt(feeAliceBefore, 0, "Alice has accrued fee");

        vm.prank(alice);
        buck.transfer(bob, 1e18);

        assertEq(buck.totalSupply(), supplyBefore, "totalSupply unchanged across transfer");
        assertEq(buck.jubileeActual(), 0, "Jubilee never receives BUCK under conservation");
    }

    function test_transfer_senderRetainsBuckAge() public {
        // Sender's idx is NOT reset on outflow -- the residual raw keeps its
        // age basis, and feeOwing on the remaining balance scales linearly
        // with the now-smaller raw.
        _setupAliceWithBuck(1000e18, 100e18);

        vm.warp(block.timestamp + 1 hours);

        uint256 rawBefore = buck.rawBalanceOf(alice);
        uint256 feeBefore = buck.feeOwing(alice);
        assertGt(feeBefore, 0, "Alice has accrued fee");

        vm.prank(alice);
        buck.transfer(bob, 1e18);

        // Alice's residual raw = rawBefore - 1e18.  Her idx is unchanged, so
        // her fee scales linearly: feeAfter / feeBefore == rawAfter / rawBefore.
        uint256 rawAfter = buck.rawBalanceOf(alice);
        uint256 feeAfter = buck.feeOwing(alice);
        assertEq(rawAfter, rawBefore - 1e18, "raw decreased by transfer amount");

        uint256 expectedFee = feeBefore * rawAfter / rawBefore;
        assertApproxEqAbs(feeAfter, expectedFee, 1, "fee scales linearly with residual raw");
    }

    function test_transfer_recipientAbsorbsCarriedAge() public {
        // All transfers are carrying under conservation: the recipient's idx
        // is weighted-merged with the sender's idx, so the recipient inherits
        // a slice of the sender's accumulated age proportional to value.
        _setupAliceWithBuck(1000e18, 100e18);

        vm.warp(block.timestamp + 1 hours);

        // Pre-condition: bob is empty.
        assertEq(buck.balanceOf(bob), 0, "bob starts empty");
        assertEq(buck.feeOwing(bob),  0, "bob has no fee debt");

        vm.prank(alice);
        buck.transfer(bob, 10e18);

        // Bob now has carried-age 10 BUCK -> non-zero feeOwing immediately.
        uint256 bobFee = buck.feeOwing(bob);
        assertGt(bobFee, 0, "bob inherits carried fee debt");
        assertLt(buck.balanceOf(bob), 10e18, "bob's spendable reduced by carried fee");
    }

    function test_transfer_systemFeeDebtPreserved() public {
        // Carrying merge conserves sum_a feeOwing(a) across any transfer.
        _setupAliceWithBuck(1000e18, 100e18);

        vm.warp(block.timestamp + 1 hours);

        uint256 totalDebtBefore = buck.feeOwing(alice) + buck.feeOwing(bob) + buck.feeOwing(POOL);

        vm.prank(alice);
        buck.transfer(bob, 10e18);

        uint256 totalDebtAfter = buck.feeOwing(alice) + buck.feeOwing(bob) + buck.feeOwing(POOL);
        assertApproxEqAbs(totalDebtAfter, totalDebtBefore, 10, "system fee debt preserved");
    }

    function test_transfer_jubileeTargetEqualsSumOfFees() public {
        // Algebraic identity: at any block, sum_a feeOwing(a) ==
        // BASE_RATE * area_under_supply == jubileeTarget().
        _setupAliceWithBuck(1000e18, 100e18);

        vm.warp(block.timestamp + 1 hours);

        // Move some BUCK around (transfers conserve fee debt, not change it).
        vm.prank(alice);
        buck.transfer(bob, 5e18);

        vm.warp(block.timestamp + 12 hours);

        uint256 sumOfFees = buck.feeOwing(alice) + buck.feeOwing(bob) + buck.feeOwing(POOL);
        uint256 target    = buck.jubileeTarget();
        assertApproxEqAbs(sumOfFees, target, 100, "sum_a feeOwing == jubileeTarget");
    }

    // ---- transferCarrying alias -------------------------------------------

    function test_transferCarrying_isAliasForTransfer() public {
        // Under conservation, transfer and transferCarrying have identical
        // semantics: both carry the sender's idx to the recipient via the
        // weighted-merge in _update.  transferCarrying remains for ABI
        // compatibility with BUCK-aware contracts (Notes etc.).
        _setupAliceWithBuck(1000e18, 100e18);

        vm.warp(block.timestamp + 1 hours);

        uint256 supplyBefore   = buck.totalSupply();
        uint256 totalDebtBefore = buck.feeOwing(alice) + buck.feeOwing(bob) + buck.feeOwing(POOL);

        vm.prank(alice);
        buck.transferCarrying(bob, 10e18);

        // Conservation: supply and total fee debt unchanged.
        assertEq(buck.totalSupply(), supplyBefore, "supply unchanged");
        uint256 totalDebtAfter = buck.feeOwing(alice) + buck.feeOwing(bob) + buck.feeOwing(POOL);
        assertApproxEqAbs(totalDebtAfter, totalDebtBefore, 10, "system fee debt preserved");

        // Bob inherited a slice of alice's age -> non-zero feeOwing.
        assertGt(buck.feeOwing(bob), 0, "bob absorbed carried age");
    }

    function test_chainedTransfers_preserveSystemFeeDebt() public {
        _setupAliceWithBuck(1000e18, 100e18);

        vm.warp(block.timestamp + 1 hours);
        uint256 debt0 = buck.feeOwing(alice) + buck.feeOwing(bob) + buck.feeOwing(POOL);

        vm.prank(alice);
        buck.transfer(bob, 10e18);

        vm.prank(bob);
        buck.transfer(alice, 1e18);

        uint256 debt1 = buck.feeOwing(alice) + buck.feeOwing(bob) + buck.feeOwing(POOL);
        assertApproxEqAbs(debt1, debt0, 100, "system fee debt preserved across chained transfers");
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

    /// @notice The Jubilee fund's claim under conservation is purely the
    ///         BASE_RATE * area_under_supply integral; jubileeActual stays at
    ///         0 (no transfers, no advance-mints).  Account dormancy doesn't
    ///         change this -- the implied claim grows continuously with time.
    function test_dormantAccount_jubileeTargetTracksContinuously() public {
        _setupAliceWithBuck(1000e18, 100e18);

        // Everyone is dormant for a full year.
        vm.warp(block.timestamp + 365 days);

        uint256 target = buck.jubileeTarget();
        assertGt(target, 0, "target grew during dormant period");
        assertEq(buck.jubileeActual(), 0, "no BUCK ever moves to Jubilee under conservation");

        // sum_a feeOwing(a) tracks target identically.
        uint256 sumOfFees = buck.feeOwing(alice) + buck.feeOwing(POOL);
        assertApproxEqAbs(sumOfFees, target, 100, "sum_a feeOwing == jubileeTarget");

        // No mint/burn -> jubileeActual stays at 0; transfers do not change that.
        vm.prank(alice);
        buck.transfer(bob, 1e18);
        assertEq(buck.jubileeActual(), 0, "transfer doesn't materialize Jubilee");

        vm.prank(alice);
        buck.mint(1e18);
        assertEq(buck.jubileeActual(), 0, "mint doesn't materialize Jubilee under conservation");
    }
}
