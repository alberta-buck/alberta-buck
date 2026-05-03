// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";

/// @title Buck.t.sol — identity-bound ERC-20 mint / approve / transfer flow.
contract BuckTest is Test {

    Buck                  internal buck;
    BuckCredit            internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;

    address internal constant GOV     = address(0xA0);
    address internal constant ISSUER  = address(0x1551E1);
    address internal constant POOL    = address(0xBA51C);

    address internal alice;
    address internal bob;
    address internal carol = address(0xCABE1);  // unverified outsider

    string internal vj;

    // ---- harness setup -----------------------------------------------------

    function setUp() public {
        vm.chainId(1);
        vj = vm.readFile("test/vectors/identity.json");

        // Identity layer + register Alice and Bob.
        reg = new IdentityRegistry(GOV);
        _trustIssuer();
        alice = address(uint160(_u(".alice.registrant")));
        bob   = address(uint160(_u(".bob.registrant")));
        _registerAlice();
        _registerBob();

        // Buck stack.
        credit = new BuckCredit();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);   // BUCK_K = 1.0
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
    }

    // ---- JSON helpers (duplicated from IdentityRegistry.t.sol intentionally;
    //      keeps tests self-contained and avoids a fragile cross-test base) ----

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

    /// @dev Plant minimal contract bytecode at `target` (so bindContract's
    ///      code.length check passes), bind a placeholder Public Identity,
    ///      and return.  Used to model BUCK-unaware Public-Identity contracts
    ///      (Uniswap pair, custodial vault, etc.) in unit tests.
    function _bindPublicIdentity(address target) internal {
        vm.etch(target, hex"60006000fd");
        BN254.G1Point memory pk = BN254.g1();
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT({
            R: BN254.g1(),
            C: BN254.g1()
        });
        reg.bindContract(target, pk, E, true, true);
    }

    /// @dev Mint a BuckCredit NFT to `client` with a fixed face value, no depreciation,
    ///      and activate it fully.  faceValue is denominated in 1e6-scaled USD.
    function _grantCredit(address client, uint256 faceValue) internal {
        uint256 tokenId = credit.createCredit(
            client,
            0,                  // assetClass
            faceValue,
            faceValue,          // depreciationFloor == faceValue: no depreciation
            BuckCredit.DepreciationType.NONE,
            0, 0, 0
        );
        vm.prank(client);
        credit.activate(tokenId, faceValue);
    }

    // ---- constructor -------------------------------------------------------

    function test_constructor_setsImmutables() public view {
        assertEq(address(buck.buckCredit()),    address(credit));
        assertEq(address(buck.buckK()),         address(kCtrl));
        assertEq(address(buck.identity()),      address(reg));
        assertEq(buck.insurancePool(),          POOL);
        assertEq(buck.name(),                   "Alberta Buck");
        assertEq(buck.symbol(),                 "BUCK");
    }

    function test_constructor_rejectsZeroAddresses() public {
        vm.expectRevert(bytes("buckCredit=0"));
        new Buck(address(0), address(kCtrl), address(reg), POOL);
        vm.expectRevert(bytes("buckK=0"));
        new Buck(address(credit), address(0), address(reg), POOL);
        vm.expectRevert(bytes("identity=0"));
        new Buck(address(credit), address(kCtrl), address(0), POOL);
        vm.expectRevert(bytes("insurancePool=0"));
        new Buck(address(credit), address(kCtrl), address(reg), address(0));
    }

    // ---- mint --------------------------------------------------------------

    function test_mint_requiresVerifiedSender() public {
        vm.prank(carol);
        vm.expectRevert(bytes("BUCK: sender not verified"));
        buck.mint(1e6);
    }

    function test_mint_revertsWithoutCredit() public {
        // Alice has no BuckCredit NFT yet -> credit limit = 0.
        vm.prank(alice);
        vm.expectRevert(bytes("BUCK: exceeds credit limit"));
        buck.mint(1e6);
    }

    function test_mint_succeedsWithinLimit() public {
        _grantCredit(alice, 1000e6);
        uint256 amount = 100e6;

        vm.prank(alice);
        buck.mint(amount);

        // Premium at ~10% utilization (100/1000): rate = 50 + (0.1)^2 * 450 = 50 + 4.5 = 54.5 bp
        // i.e. ~0.545% of 100e6 ≈ 0.545e6.
        uint256 premium = buck.balanceOf(POOL);
        uint256 net     = buck.balanceOf(alice);
        assertEq(net + premium, amount, "net + premium == minted");
        assertGt(premium, 0, "premium should be positive");
        assertEq(buck.storedLimit(alice), 1000e6, "limit ratchet up");
    }

    function test_mint_storedLimitOnlyIncreases() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(50e6);
        assertEq(buck.storedLimit(alice), 1000e6);

        // Reduce BUCK_K to half; credit value would imply 500e6 limit, but stored limit holds.
        vm.prank(GOV);
        kCtrl.setBuckK(0.5e18);
        vm.prank(alice);
        buck.mint(10e6);
        assertEq(buck.storedLimit(alice), 1000e6, "stored limit stays at peak");
    }

    function test_mint_rejectsWhenAggregatedExceedsLimit() public {
        _grantCredit(alice, 100e6);
        vm.prank(alice);
        buck.mint(50e6);
        vm.prank(alice);
        vm.expectRevert(bytes("BUCK: exceeds credit limit"));
        buck.mint(60e6);
    }

    // ---- burn --------------------------------------------------------------

    function test_burn_reducesBalance() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);
        uint256 before_ = buck.balanceOf(alice);

        vm.prank(alice);
        buck.burn(10e6);
        assertEq(buck.balanceOf(alice), before_ - 10e6);
    }

    // ---- approve -----------------------------------------------------------

    function test_plainApprove_isBlocked() public {
        vm.prank(alice);
        vm.expectRevert(bytes("BUCK: use identity-bound approve"));
        buck.approve(bob, 100e6);
    }

    function test_identityApprove_succeedsWithValidProof() public {
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        IdentityRegistry.CPProof memory pi = _cpProof();

        vm.prank(alice);
        buck.approve(bob, 100e6, E_b, pi);

        assertEq(buck.allowance(alice, bob), 100e6);
        bytes32 expected = keccak256(abi.encode(E_b.R.X, E_b.R.Y, E_b.C.X, E_b.C.Y));
        assertEq(buck.receiptFragment(alice, bob), expected);
    }

    function test_identityApprove_freezesSpenderCarryingFlag() public {
        // Pre-approve, bob is an EOA with isCarrying = false (default).
        // After alice approves bob, bob's carryingFrozen flag is set so the
        // flavour bob holds at the moment of approve cannot be changed
        // retroactively.
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        IdentityRegistry.CPProof memory pi = _cpProof();

        assertFalse(reg.carryingFrozen(bob), "not yet frozen");

        vm.prank(alice);
        buck.approve(bob, 100e6, E_b, pi);

        assertTrue(reg.carryingFrozen(bob), "frozen by approve");
    }

    function test_identityApprove_rejectsBadProof() public {
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        IdentityRegistry.CPProof memory bad = _cpProof();
        bad.e = (bad.e + 1) % BN254.R;
        vm.prank(alice);
        vm.expectRevert(bytes("BUCK: bad CP proof"));
        buck.approve(bob, 100e6, E_b, bad);
    }

    function test_identityApprove_rejectsUnverifiedSender() public {
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        vm.prank(carol);
        vm.expectRevert(bytes("BUCK: sender not verified"));
        buck.approve(bob, 100e6, E_b, _cpProof());
    }

    function test_identityApprove_rejectsUnverifiedSpender() public {
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        vm.prank(alice);
        vm.expectRevert(bytes("BUCK: spender not verified"));
        buck.approve(carol, 100e6, E_b, _cpProof());
    }

    function test_identityApprove_publicContractStillRequiresCP() public {
        // Bind a Public-Identity contract (e.g., AMM pair).  Even though its
        // identity is publicly attested off-chain, approve() still requires a
        // valid CP proof so the contract operator obtains a CP-encrypted
        // receipt of the approver's identity for subpoena decryption.
        address pool = address(0xDECAF);
        _bindPublicIdentity(pool);

        IdentityRegistry.ElGamalCT memory junk;
        IdentityRegistry.CPProof memory junkProof;
        vm.prank(alice);
        vm.expectRevert(bytes("BUCK: bad CP proof"));
        buck.approve(pool, 50e6, junk, junkProof);
    }

    // ---- transfer ----------------------------------------------------------

    function test_transfer_requiresVerifiedSender() public {
        vm.prank(carol);
        vm.expectRevert(bytes("BUCK: sender not verified"));
        buck.transfer(bob, 1e6);
    }

    function test_transfer_requiresVerifiedRecipient() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);
        vm.prank(alice);
        vm.expectRevert(bytes("BUCK: recipient not verified"));
        buck.transfer(carol, 1e6);
    }

    function test_transfer_requiresPriorApproveReceipt() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);

        // Alice has not yet approved Bob -> no receipt fragment -> must revert.
        vm.prank(alice);
        vm.expectRevert(bytes("BUCK: missing identity receipt"));
        buck.transfer(bob, 1e6);
    }

    function test_transfer_succeedsAfterApprove() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);

        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        vm.prank(alice);
        buck.approve(bob, 50e6, E_b, _cpProof());

        uint256 aliceBefore = buck.balanceOf(alice);
        vm.prank(alice);
        buck.transfer(bob, 10e6);
        assertEq(buck.balanceOf(alice), aliceBefore - 10e6);
        assertEq(buck.balanceOf(bob),   10e6);
    }

    function test_transfer_publicContractRecipientSkipsReceipt() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);

        // Public-Identity contract recipient -> Alice can transfer without
        // a prior CP approve receipt; the receipt-fragment fallback to the
        // deterministic _identityHash kicks in because the contract's
        // identity is already publicly attested.
        address pool = address(0xDECAF);
        _bindPublicIdentity(pool);
        vm.prank(alice);
        buck.transfer(pool, 5e6);
        assertEq(buck.balanceOf(pool), 5e6);
    }

    function test_transferFrom_consumesAllowance() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);

        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        vm.prank(alice);
        buck.approve(bob, 50e6, E_b, _cpProof());

        vm.prank(bob);
        buck.transferFrom(alice, bob, 25e6);
        assertEq(buck.allowance(alice, bob), 25e6);
        assertEq(buck.balanceOf(bob),        25e6);
    }

    // ---- public-contract sender -> verified-EOA (e.g. Uniswap pair payout) -

    function test_transfer_publicContractSenderToVerifiedSkipsReceiptFragment() public {
        // A Public-Identity contract (proxy for a Uniswap pair) holds BUCK
        // and pays it out to verified Bob.  No prior CP approve from the
        // contract to Bob exists, and the contract has no off-chain crypto
        // material to produce one -- the transfer succeeds because the
        // contract's identity is publicly attested (fallback to identityHash).
        address pool = address(0xDECAF);
        _bindPublicIdentity(pool);

        // Seed the contract with BUCK.  Alice transfers to it directly,
        // exercising the Public-recipient-receipt fallback at the same time.
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);
        vm.prank(alice);
        buck.transfer(pool, 30e6);
        assertEq(buck.balanceOf(pool), 30e6);

        // Now the public contract pays out to Bob (verified, never approved
        // by the contract).  Pre-refactor this reverted with "missing
        // identity receipt"; post-refactor it succeeds via the Public-sender
        // fallback.
        vm.prank(pool);
        buck.transfer(bob, 7e6);
        assertEq(buck.balanceOf(bob), 7e6);
    }
}
