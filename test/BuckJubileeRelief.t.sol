// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckCreditHarness} from "./harness/BuckCreditHarness.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";
import {bindCarryingPool} from "./harness/CarryingPool.sol";

/// @title BuckJubileeRelief.t.sol -- Jubilee relief on the BUCK issued.
///
/// The position that ages is the LIEN: the BUCK an account's credit actually
/// put into circulation.  Buck integrates it while the account is below zero
/// (issuance-seconds, in the field that counts fee-seconds above zero) and
/// quotes reliefOf(account) / redeemCost(account) -- the amount required to
/// close the lien, melting ~2%/yr.  Undrawn credit earns nothing.  Relief
/// pays out of the fund when the lien is repaid, at burn, or when the holder
/// settles it; positions are never force-closed.  (It used to accrue in
/// BuckCredit on activated coverage: doc/JUBILEE-ISSUANCE.org.)
contract BuckJubileeReliefTest is Test {

    Buck                  internal buck;
    BuckCreditHarness     internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;

    address internal constant GOV     = address(0xA0);
    address internal constant ISSUER  = address(0x1551E1);
    address internal constant POOL    = address(0xBA51C);
    address internal constant REGISTRY_ADDR =
        0x1D1D1D1d1d1D1D1d1d1D1D1d1d1D1d1d1d1d1D1D;

    address internal alice;
    address internal bob;

    string internal vj;

    uint256 internal constant FACE = 1000e6;  // alice's credit face value
    uint256 internal constant DRAW = 100e6;   // drawn into circulation
    uint256 internal constant YEAR = 365 days + 6 hours;
    uint256 internal t0;                      // setUp time; warp to t0+k*YEAR
    uint256 internal tid;                     // alice's credit NFT

    function setUp() public {
        vm.chainId(1);
        vj = vm.readFile("test/vectors/identity.json");

        deployCodeTo(
            "test/harness/IdentityRegistryHarness.sol:IdentityRegistryHarness",
            abi.encode(GOV),
            REGISTRY_ADDR
        );
        reg = IdentityRegistry(REGISTRY_ADDR);
        _trustIssuer();
        alice = address(uint160(_u(".alice.registrant")));
        bob   = address(uint160(_u(".bob.registrant")));
        _registerAlice();
        _bindPublicPool(bob);

        credit = new BuckCreditHarness();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);
        buck   = _newBuck();
        bindCarryingPool(reg, POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));

        bytes32 slot = keccak256(abi.encode(bob, keccak256(abi.encode(alice, uint256(5)))));
        vm.store(address(buck), slot, bytes32(uint256(1)));

        t0 = block.timestamp;
        // Zero-premium credit, minted (activation + mintsBacked) so the
        // burn path can unwind it, then DRAW paid to bob (real supply the
        // Jubilee fund accrues on).
        tid = credit.createCredit(
            alice, 0, FACE, FACE,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(alice);
        buck.mint(FACE);
        vm.prank(alice);
        buck.transfer(bob, DRAW);
        assertEq(buck.signedRawBalanceOf(alice), -int256(DRAW), "lien open");
        assertEq(buck.totalSupply(), DRAW, "supply == bob's received BUCK");
        (, uint256 active,) = credit.creditInfo(tid);
        assertEq(active, FACE, "mint activated the full face");
    }

    /// @dev Production Buck; JubileeBasis.t.sol deploys the sims' hooked
    ///      subclass to test the pro-rata baskets' hooks too.
    function _newBuck() internal virtual returns (Buck) {
        return new Buck(address(credit), address(kCtrl), address(reg), POOL);
    }

    // ---- JSON / identity helpers (copied from BuckDemurrage.t.sol) ---------

    function _u(string memory key) internal view returns (uint256) {
        return vm.parseJsonUint(vj, key);
    }

    function _g1(string memory key) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point(_u(string.concat(key, ".x")), _u(string.concat(key, ".y")));
    }

    function _ps(string memory who) internal view returns (IdentityRegistry.PSPresentation memory s) {
        s.A = _g1(string.concat(".", who, ".ps_presentation.A"));
        s.B = _g1(string.concat(".", who, ".ps_presentation.B"));
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
        p.s_sk = _u(string.concat(base, ".s_sk"));
        p.s_b = _u(string.concat(base, ".s_b"));
        p.C1 = _g1(string.concat(base, ".C1"));
        p.T_C  = _g1(string.concat(base, ".T_C"));
        p.T_R  = _g1(string.concat(base, ".T_R"));
        p.T_key = _g1(string.concat(base, ".T_key"));
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
        ipk.Y1 = _g1(".issuer.pk_Y1");
        vm.prank(GOV);
        reg.trustIssuer(ISSUER, ipk);
    }

    function _registerAlice() internal {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        vm.prank(alice);
        reg.register(ISSUER, pk, E, _ps("alice"), _regProof("alice"));
    }

    function _bindPublicPool(address target) internal {
        vm.etch(target, hex"60006000fd");
        BN254.G1Point memory pk = BN254.g1();
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT({
            R: BN254.g1(),
            C: BN254.g1()
        });
        reg.bindContract(target, pk, E, true, true);
    }

    // ---- quotes -------------------------------------------------------------

    function _lien(address a) internal view returns (uint256) {
        int256 raw = buck.signedRawBalanceOf(a);
        return raw < 0 ? uint256(-raw) : 0;
    }

    function test_relief_zeroAtOpen() public view {
        assertEq(buck.reliefOf(alice), 0, "no relief same block");
        assertEq(buck.redeemCost(alice), DRAW, "redeem cost == the lien at open");
    }

    function test_relief_accruesOnTheLien() public {
        vm.warp(t0 + YEAR);
        uint256 relief = buck.reliefOf(alice);
        assertApproxEqRel(relief, DRAW * 2 / 100, 0.001e18,
                          "~2% of the 100 drawn after one year, not of the 1000 covered");
        assertEq(buck.redeemCost(alice), DRAW - relief, "redeem cost melts by the relief");
        // The quote is a view: nothing settled, coverage + lien unchanged.
        (, uint256 active,) = credit.creditInfo(tid);
        assertEq(active, FACE, "coverage intact");
        assertEq(buck.signedRawBalanceOf(alice), -int256(DRAW), "lien intact");
    }

    function test_relief_undrawnCreditAddsNothing() public {
        uint256 fresh = credit.createCredit(
            alice, 0, FACE, FACE,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(alice);
        credit.forceActivate(fresh, FACE);
        vm.warp(t0 + YEAR);
        assertApproxEqRel(buck.reliefOf(alice), DRAW * 2 / 100, 0.001e18,
                          "a second 1000 of coverage, undrawn, earns nothing");
        assertEq(buck.reliefOf(bob), 0, "a holder of BUCK has no lien");
    }

    function test_relief_cappedAtTheLien() public {
        vm.warp(t0 + 60 * YEAR);
        assertEq(buck.reliefOf(alice), DRAW, "relief never exceeds the lien");
        assertEq(buck.redeemCost(alice), 0, "a lien carried ~50y closes for free");
    }

    function test_aging_integratesTheLien() public {
        // The lien doubles a year in: aging is the integral of the lien over
        // time, not a single timestamp.
        vm.warp(t0 + YEAR);
        vm.prank(alice);
        buck.transfer(bob, DRAW);
        vm.warp(t0 + 2 * YEAR);
        // integral = 100 * 2y + 100 * 1y = 3 * DRAW * 1y
        assertApproxEqRel(buck.reliefOf(alice), (DRAW * 2 / 100) * 3, 0.001e18,
                          "aging integrates the lien over time");
    }

    // ---- settlement -----------------------------------------------------------

    function test_burn_settlesReliefFromFund() public {
        vm.warp(t0 + YEAR);
        uint256 quote = buck.reliefOf(alice);
        uint256[] memory tids = new uint256[](1);
        tids[0] = tid;
        vm.prank(alice);
        buck.burn(FACE / 2 * 98 / 100, tids);   // net amount, ~half coverage

        // The relief paid out whole: the fund accrued exactly it, on the 100
        // issued, and the lien shrank by it.
        assertApproxEqAbs(DRAW - _lien(alice), quote, 1, "the lien fell by the relief");
        assertLt(buck.jubileeActual(), 2, "fund paid out to dust");
        assertEq(buck.reliefOf(alice), 0, "nothing left accrued");
        (, uint256 active,) = credit.creditInfo(tid);
        assertLt(active, FACE, "coverage unwound");
    }

    function test_burn_emitsJubileeRedeemed() public {
        vm.warp(t0 + YEAR);
        uint256[] memory tids = new uint256[](1);
        tids[0] = tid;
        vm.expectEmit(true, false, false, false, address(buck));
        emit Buck.JubileeRedeemed(alice, 0);
        vm.prank(alice);
        buck.burn(FACE / 2 * 98 / 100, tids);
    }

    function test_settleRelief_paysTheHolder() public {
        vm.warp(t0 + YEAR);
        uint256 quote = buck.reliefOf(alice);
        vm.prank(alice);
        buck.settleRelief();
        assertApproxEqAbs(DRAW - _lien(alice), quote, 1, "the lien fell by the relief");
        assertEq(buck.reliefOf(alice), 0, "consumed");
        vm.warp(t0 + 2 * YEAR);
        assertApproxEqRel(buck.reliefOf(alice), (DRAW - quote) * 2 / 100, 0.001e18,
                          "and it accrues on the smaller lien from here");
    }

    /// The two sides balance: bob's 100 BUCK, a year old, repay alice's lien
    /// net of their 2 fee, and the 2 of relief her lien earned pays the rest.
    function test_repayment_feeAndReliefBalance() public {
        vm.warp(t0 + YEAR);
        vm.prank(bob);
        buck.transfer(alice, DRAW);
        assertApproxEqAbs(_lien(alice), DRAW * 2 / 100, 1, "the fee: 98 repaid, 2 left");
        vm.prank(alice);
        buck.settleRelief();
        assertLe(_lien(alice), 1, "the relief pays the 2: closed");
        assertLe(buck.jubileeActual(), 1, "and the fund is square");
    }

    function test_neverForcedClosure() public {
        // Carried for 3 years untouched: the quote melts year by year but
        // the position is exactly as the holder left it.
        vm.warp(t0 + 3 * YEAR);
        (, uint256 active,) = credit.creditInfo(tid);
        assertEq(active, FACE, "coverage untouched");
        assertEq(buck.signedRawBalanceOf(alice), -int256(DRAW), "lien untouched");
        assertApproxEqRel(buck.reliefOf(alice), DRAW * 6 / 100, 0.001e18,
                          "quote melted ~2%/yr for 3 years");
        assertApproxEqRel(buck.redeemCost(alice), DRAW - DRAW * 6 / 100,
                          0.001e18, "redeem cost reflects the melt");
    }
}
