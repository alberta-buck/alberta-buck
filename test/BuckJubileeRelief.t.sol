// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckCreditHarness} from "./harness/BuckCreditHarness.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";

/// @title BuckJubileeRelief.t.sol -- Jubilee aging on the BuckCredit API.
///
/// The position that ages is the CREDIT position: BuckCredit accumulates
/// coverage-seconds (activatedValue * dt) per NFT, quoted continuously via
/// jubileeRelief(tokenId) / redeemCost(tokenId) -- the amount required to
/// close the position, including the Jubilee benefit, melting ~2%/yr.
/// Settlement happens inside the EXISTING burn path: deactivateFromBuck
/// reports the relief carried by the unwound coverage and Buck rebates it
/// from the fund's demurrage accrual.  Positions are never force-closed.
contract BuckJubileeReliefTest is Test {

    Buck                  internal buck;
    BuckCreditHarness     internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;

    address internal constant GOV     = address(0xA0);
    address internal constant ISSUER  = address(0x1551E1);
    address internal constant POOL    = address(0xBA51C);

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

        reg = new IdentityRegistry(GOV);
        _trustIssuer();
        alice = address(uint160(_u(".alice.registrant")));
        bob   = address(uint160(_u(".bob.registrant")));
        _registerAlice();
        _bindPublicPool(bob);

        credit = new BuckCreditHarness();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
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

    // ---- JSON / identity helpers (copied from BuckDemurrage.t.sol) ---------

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

    function _bindPublicPool(address target) internal {
        vm.etch(target, hex"60006000fd");
        BN254.G1Point memory pk = BN254.g1();
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT({
            R: BN254.g1(),
            C: BN254.g1()
        });
        reg.bindContract(target, pk, E, true, true);
    }

    // ---- quotes (the BuckCredit API) ----------------------------------------

    function test_relief_zeroAtOpen() public view {
        assertEq(credit.jubileeRelief(tid), 0, "no relief same block");
        assertEq(credit.redeemCost(tid), FACE, "redeem cost == coverage at open");
    }

    function test_relief_accruesAtBaseRate() public {
        vm.warp(t0 + YEAR);
        uint256 relief = credit.jubileeRelief(tid);
        assertApproxEqRel(relief, FACE * 2 / 100, 0.001e18,
                          "~2% of the coverage after one year");
        assertEq(credit.redeemCost(tid), FACE - relief,
                 "redeem cost melts by the relief");
        // The quote is a view: nothing settled, coverage + lien unchanged.
        (, uint256 active,) = credit.creditInfo(tid);
        assertEq(active, FACE, "coverage intact");
        assertEq(buck.signedRawBalanceOf(alice), -int256(DRAW), "lien intact");
    }

    function test_relief_zeroWhenUnactivated() public {
        uint256 fresh = credit.createCredit(
            alice, 0, FACE, FACE,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.warp(t0 + YEAR);
        assertEq(credit.jubileeRelief(fresh), 0, "unactivated coverage: none");
        assertEq(credit.redeemCost(fresh), 0, "nothing to close");
    }

    function test_relief_cappedAtCoverage() public {
        vm.warp(t0 + 60 * YEAR);
        assertEq(credit.jubileeRelief(tid), FACE,
                 "relief never exceeds the coverage");
        assertEq(credit.redeemCost(tid), 0,
                 "a position carried ~50y redeems for free");
    }

    function test_aging_foldsAcrossActivationChanges() public {
        // A second credit activated in two halves a year apart: aging is the
        // integral of activatedValue, not a single timestamp.
        uint256 t2 = credit.createCredit(
            alice, 0, FACE, FACE,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(alice);
        credit.forceActivate(t2, FACE / 2);
        vm.warp(t0 + YEAR);
        vm.prank(alice);
        credit.forceActivate(t2, FACE / 2);
        vm.warp(t0 + 2 * YEAR);
        // integral = FACE/2 * 2y + FACE/2 * 1y = 1.5 * FACE * 1y
        uint256 expected = (FACE * 2 / 100) * 3 / 2;
        assertApproxEqRel(credit.jubileeRelief(t2), expected, 0.001e18,
                          "aging integrates activatedValue over time");
    }

    // ---- settlement (inside the existing burn path) -------------------------

    function test_burn_settlesReliefFromFund() public {
        vm.warp(t0 + YEAR);
        // Unwind half the coverage.  Its pro-rata relief quote is ~1% of
        // FACE (= 10), but settlement is capped by the fund, which accrued
        // ~2% of totalSupply (= DRAW) => ~2.
        uint256 fundAccrual = DRAW * 2 / 100;
        int256 rawBefore = buck.signedRawBalanceOf(alice);

        uint256[] memory tids = new uint256[](1);
        tids[0] = tid;
        vm.prank(alice);
        buck.burn(FACE / 2 * 98 / 100, tids);   // net amount, ~half coverage

        // Relief landed on alice's raw (climbing her lien toward zero),
        // capped by the fund's accrued balance.
        int256 rawAfter = buck.signedRawBalanceOf(alice);
        int256 delta = rawAfter - rawBefore;
        assertGt(delta, 0, "refund + relief climb the lien");
        assertLt(buck.jubileeActual(), fundAccrual / 100 + 2,
                 "fund consumed to dust");
        // Coverage shrank; the surviving coverage keeps pro-rata aging.
        (, uint256 active,) = credit.creditInfo(tid);
        assertLt(active, FACE, "coverage unwound");
        assertGt(credit.jubileeRelief(tid), 0,
                 "surviving coverage keeps its share of aging");
    }

    function test_burn_emitsJubileeRedeemed() public {
        vm.warp(t0 + YEAR);
        uint256[] memory tids = new uint256[](1);
        tids[0] = tid;
        // Settlement == min(pro-rata quote, fund) -- fund is the binding cap.
        vm.expectEmit(true, false, false, false, address(buck));
        emit Buck.JubileeRedeemed(alice, 0);
        vm.prank(alice);
        buck.burn(FACE / 2 * 98 / 100, tids);
    }

    function test_neverForcedClosure() public {
        // Carried for 3 years untouched: the quote melts year by year but
        // the position is exactly as the holder left it.
        vm.warp(t0 + 3 * YEAR);
        (, uint256 active,) = credit.creditInfo(tid);
        assertEq(active, FACE, "coverage untouched");
        assertEq(buck.signedRawBalanceOf(alice), -int256(DRAW), "lien untouched");
        assertApproxEqRel(credit.jubileeRelief(tid), FACE * 6 / 100, 0.001e18,
                          "quote melted ~2%/yr for 3 years");
        assertApproxEqRel(credit.redeemCost(tid), FACE - FACE * 6 / 100,
                          0.001e18, "redeem cost reflects the melt");
    }
}
