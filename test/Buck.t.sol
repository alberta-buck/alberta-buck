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
    ///      Sets a 50bp annual premiumRate so Buck.mint pulls a non-zero
    ///      premium against this NFT.
    function _grantCredit(address client, uint256 faceValue) internal returns (uint256) {
        return _grantCreditAtRate(client, faceValue, 50);
    }

    function _grantCreditAtRate(address client, uint256 faceValue, uint32 premiumRate)
        internal returns (uint256 tokenId)
    {
        tokenId = credit.createCredit(
            client,
            0,                  // assetClass
            faceValue,
            faceValue,          // depreciationFloor == faceValue: no depreciation
            BuckCredit.DepreciationType.NONE,
            0, 0,
            premiumRate
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

        // 50bp annual rate, 10x pool ROI inverse:
        //   denom = BP - 50*10 = 9500
        //   take  = ceil(100e6 * 10000 / 9500) = 105_263_158
        //   pool  = take - amount             =   5_263_158
        assertEq(buck.balanceOf(alice), amount,            "holder gets exactly amount");
        assertEq(buck.balanceOf(POOL),  5_263_158,         "pool principal = annual_premium * 10");
        assertEq(buck.totalSupply(),    amount + 5_263_158, "total = delivery + principal");
        assertEq(buck.storedLimit(alice), 1000e6,           "limit ratchet up");
    }

    function test_mint_cheapestFirst_picksLowestRateNFT() public {
        // Two NFTs of equal face: rate=200bp and rate=50bp.  The default
        // selector should drain the 50bp NFT first.
        uint256 cheap = _grantCreditAtRate(alice, 100e6, 50);
        uint256 dear  = _grantCreditAtRate(alice, 100e6, 200);

        vm.prank(alice);
        buck.mint(80e6);

        // denom_cheap = 9500, take = ceil(80e6 * 10000/9500) = 84_210_527
        // pool        = take - 80e6 = 4_210_527
        assertEq(buck.mintsBacked(cheap), 84_210_527, "cheap NFT consumed first");
        assertEq(buck.mintsBacked(dear),  0,          "dear NFT untouched");
        assertEq(buck.balanceOf(POOL),    4_210_527,  "pool principal");
        assertEq(buck.balanceOf(alice),   80e6,       "holder gets net amount");
    }

    function test_mint_cheapestFirst_spillsIntoNextNFT() public {
        uint256 cheap = _grantCreditAtRate(alice, 100e6, 50);
        uint256 dear  = _grantCreditAtRate(alice, 100e6, 200);

        vm.prank(alice);
        buck.mint(150e6);

        // Cheap fully drawn: take = 100e6, netCap = 100e6 * 9500/10000 = 95e6,
        // pool_cheap = 5e6, remaining = 150e6 - 95e6 = 55e6.
        // Dear: denom = 8000, take = ceil(55e6 * 10000/8000) = 68_750_000,
        // pool_dear = 68.75e6 - 55e6 = 13_750_000.
        assertEq(buck.mintsBacked(cheap), 100e6,      "cheap exhausted");
        assertEq(buck.mintsBacked(dear),  68_750_000, "spillover to dear NFT");
        assertEq(buck.balanceOf(POOL),    18_750_000, "pool = 5e6 + 13.75e6");
        assertEq(buck.balanceOf(alice),   150e6);
    }

    function test_mint_explicitTokenIds_overridesOrder() public {
        uint256 cheap = _grantCreditAtRate(alice, 100e6, 50);
        uint256 dear  = _grantCreditAtRate(alice, 100e6, 200);

        // Caller forces the dear-first order (e.g. external optimizer reasoning
        // about depreciation or off-chain insurer preferences).
        uint256[] memory order = new uint256[](2);
        order[0] = dear;
        order[1] = cheap;

        vm.prank(alice);
        buck.mint(80e6, order);

        // Dear: denom = 8000, take = ceil(80e6 * 10000/8000) = 100e6 (full cap),
        //        pool_dear = 20e6.  remaining = 80e6 - 80e6 = 0 (full netCap).
        assertEq(buck.mintsBacked(dear),  100e6, "dear NFT drawn first per caller order");
        assertEq(buck.mintsBacked(cheap), 0);
        assertEq(buck.balanceOf(POOL),    20e6, "100e6 take * 200bp * 10 / BP = 20e6");
        assertEq(buck.balanceOf(alice),   80e6);
    }

    function test_mint_explicitTokenIds_revertsIfInsufficient() public {
        uint256 small = _grantCreditAtRate(alice, 50e6, 50);
        _grantCreditAtRate(alice, 100e6, 200); // exists but not in the supplied list

        uint256[] memory order = new uint256[](1);
        order[0] = small;

        vm.prank(alice);
        vm.expectRevert(bytes("BUCK: insufficient credit allocation"));
        buck.mint(80e6, order);
    }

    function test_mint_explicitTokenIds_rejectsNonOwnerToken() public {
        uint256 bobToken = _grantCreditAtRate(bob, 100e6, 50);
        _grantCreditAtRate(alice, 100e6, 50);

        uint256[] memory order = new uint256[](1);
        order[0] = bobToken;

        vm.prank(alice);
        vm.expectRevert(bytes("BUCK: not credit owner"));
        buck.mint(10e6, order);
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

    function test_burn_refundsPoolPrincipalProportionally() public {
        uint256 tid = _grantCredit(alice, 1000e6);          // 50bp NFT
        vm.prank(alice);
        buck.mint(100e6);
        uint256 backedAfterMint = buck.mintsBacked(tid);    // 105_263_158
        uint256 poolAfterMint   = buck.balanceOf(POOL);     //   5_263_158

        vm.prank(alice);
        buck.burn(10e6);

        // Inverse of mint at the same rate:
        //   unwind = ceil(10e6 * 10000 / 9500) = 10_526_316
        //   refund = unwind - 10e6              =     526_316
        assertEq(buck.balanceOf(alice),   100e6 - 10e6, "holder net burn");
        assertEq(buck.balanceOf(POOL),    poolAfterMint - 526_316, "pool refund returned");
        assertEq(buck.mintsBacked(tid),   backedAfterMint - 10_526_316, "coverage unwound");
    }

    function test_burn_explicitTokenIds_unwindsChosenNFT() public {
        uint256 cheap = _grantCreditAtRate(alice, 100e6, 50);
        uint256 dear  = _grantCreditAtRate(alice, 100e6, 200);

        // Mint draws cheap-first; force burn against dear by passing an
        // explicit list (no allocation on dear yet, so this should revert).
        vm.prank(alice);
        buck.mint(50e6);

        uint256[] memory order = new uint256[](1);
        order[0] = dear;

        vm.prank(alice);
        vm.expectRevert(bytes("BUCK: insufficient coverage to unwind"));
        buck.burn(10e6, order);

        // Burn against the cheap NFT (the one with allocation) succeeds.
        order[0] = cheap;
        vm.prank(alice);
        buck.burn(10e6, order);
        assertEq(buck.balanceOf(alice), 50e6 - 10e6);
    }

    function test_burn_mostExpensiveFirst_releasesDearestFirst() public {
        // Both NFTs end up with allocation after a 150e6 mint (cheap NFT
        // exhausted, dear partially drawn).  A subsequent burn(50e6) should
        // unwind dear first under the new most-expensive-first selector.
        uint256 cheap = _grantCreditAtRate(alice, 100e6, 50);
        uint256 dear  = _grantCreditAtRate(alice, 100e6, 200);

        vm.prank(alice);
        buck.mint(150e6);

        uint256 backedCheapBefore = buck.mintsBacked(cheap);   // 100e6 (full cap)
        uint256 backedDearBefore  = buck.mintsBacked(dear);    // 68_750_000
        uint256 poolBefore        = buck.balanceOf(POOL);      // 18_750_000

        vm.prank(alice);
        buck.burn(50e6);

        // Dear-first unwind: denom_dear = 8000, unwind = ceil(50e6 * 10000/8000)
        // = 62_500_000, refund_dear = 62.5e6 - 50e6 = 12_500_000.
        assertEq(buck.mintsBacked(cheap), backedCheapBefore,
                 "cheap NFT untouched while dear has capacity");
        assertEq(buck.mintsBacked(dear),  backedDearBefore - 62_500_000,
                 "dear NFT consumed by 62.5e6");
        assertEq(buck.balanceOf(POOL),    poolBefore - 12_500_000,
                 "pool refunds the dear-rate principal first");
        assertEq(buck.balanceOf(alice),   150e6 - 50e6, "holder net burn");
    }

    function test_burn_mostExpensiveFirst_spillsIntoCheap() public {
        // Burn larger than the dear NFT's outstanding -- spills into cheap.
        uint256 cheap = _grantCreditAtRate(alice, 100e6, 50);
        uint256 dear  = _grantCreditAtRate(alice, 100e6, 200);

        vm.prank(alice);
        buck.mint(150e6);

        // Burn enough to drain dear AND eat into cheap.
        // Dear netCap = 68.75e6 * 8000/10000 = 55e6 of holder reduction.
        // Burn 100e6: dear contributes 55e6 (full), remaining 45e6 goes to cheap.
        // Cheap denom = 9500, unwind = ceil(45e6*10000/9500) = 47_368_422,
        // refund_cheap = 47.37e6 - 45e6 = 2_368_422.
        // Total refund = 13_750_000 (dear) + 2_368_422 (cheap) = 16_118_422.
        vm.prank(alice);
        buck.burn(100e6);

        assertEq(buck.mintsBacked(dear),  0,                          "dear fully unwound");
        assertEq(buck.mintsBacked(cheap), 100e6 - 47_368_422,         "cheap partially unwound");
        assertEq(buck.balanceOf(alice),   50e6,                        "holder burned 100e6");
        // 18_750_000 minted to pool initially; 16_118_422 refunded.
        assertEq(buck.balanceOf(POOL),    18_750_000 - 16_118_422,    "pool refund spans both NFTs");
    }

    function test_quoteMint_matchesExecution() public {
        uint256 cheap = _grantCreditAtRate(alice, 100e6, 50);
        uint256 dear  = _grantCreditAtRate(alice, 100e6, 200);

        uint256[] memory order = new uint256[](2);
        order[0] = cheap;
        order[1] = dear;

        (uint256 quotedCoverage, uint256 quotedPrincipal) = buck.quoteMint(150e6, order);

        vm.prank(alice);
        buck.mint(150e6, order);

        // Total coverage written across the two NFTs should equal the quote.
        assertEq(buck.mintsBacked(cheap) + buck.mintsBacked(dear), quotedCoverage);
        assertEq(buck.balanceOf(POOL), quotedPrincipal);
        assertEq(quotedCoverage, 150e6 + quotedPrincipal, "delivery + principal == coverage");
    }

    function test_quoteBurn_matchesExecution() public {
        uint256 tid = _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);

        uint256[] memory order = new uint256[](1);
        order[0] = tid;

        (uint256 quotedUnwind, uint256 quotedRefund) = buck.quoteBurn(40e6, order);

        uint256 backedBefore = buck.mintsBacked(tid);
        uint256 poolBefore   = buck.balanceOf(POOL);

        vm.prank(alice);
        buck.burn(40e6, order);

        assertEq(backedBefore - buck.mintsBacked(tid), quotedUnwind, "unwound matches quote");
        assertEq(poolBefore   - buck.balanceOf(POOL),  quotedRefund, "refund matches quote");
    }

    // ---- approve -----------------------------------------------------------

    /// @dev Plain ERC-20 approve is now permitted (required for standard
    ///      router / Permit2 infrastructure).  It sets the allowance but
    ///      does NOT establish a Chaum-Pedersen receipt fragment and does
    ///      NOT freeze the spender's carrying flag -- those remain exclusive
    ///      to the 4-arg identity-bound approve.  Identity is enforced at
    ///      transfer time, not here.
    function test_plainApprove_setsAllowanceButNoReceiptOrFreeze() public {
        assertFalse(reg.carryingFrozen(bob), "not yet frozen");

        vm.prank(alice);
        bool ok = buck.approve(bob, 100e6);
        assertTrue(ok, "plain approve returns true");

        assertEq(buck.allowance(alice, bob), 100e6, "allowance set");
        assertEq(buck.receiptFragment(alice, bob), bytes32(0),
                 "plain approve must NOT establish a receipt fragment");
        assertFalse(reg.carryingFrozen(bob),
                    "plain approve must NOT freeze the spender carrying flag");
    }

    /// @dev GUARANTEE: a plain allowance cannot bypass the confidential-
    ///      transfer rule.  Bob is verified but non-public; with only a
    ///      plain approve (no 4-arg receipt) and neither party public, a
    ///      spender-initiated transferFrom into a non-public party still
    ///      reverts exactly as a direct transfer would.
    function test_plainApprove_transferFrom_toConfidential_stillReverts() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);

        vm.prank(alice);
        buck.approve(bob, 50e6);                 // plain approve, no receipt

        vm.prank(bob);
        vm.expectRevert(bytes("BUCK: missing identity receipt"));
        buck.transferFrom(alice, bob, 10e6);     // alice & bob both non-public
    }

    /// @dev GUARANTEE: transferFrom still requires a verified recipient,
    ///      regardless of the plain allowance.
    function test_plainApprove_transferFrom_requiresVerifiedRecipient() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);

        vm.prank(alice);
        buck.approve(bob, 50e6);

        vm.prank(bob);
        vm.expectRevert(bytes("BUCK: recipient not verified"));
        buck.transferFrom(alice, carol, 10e6);   // carol unverified
    }

    /// @dev The intended new capability (the Permit2 / router path): a
    ///      plain-approved spender CAN move the owner's BUCK to a
    ///      public-identity recipient (a bound pool), because the
    ///      (from,to) gate is satisfied by the public side -- no receipt
    ///      fragment, no CP proof.  This is the standard-router flow.
    function test_plainApprove_transferFrom_toPublicRecipient_succeeds() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);

        address pool = address(0xDECAF);
        _bindPublicIdentity(pool);

        // `bob` stands in for a router/Permit2 spender: alice grants it a
        // plain allowance; it pulls her BUCK into the public pool.
        vm.prank(alice);
        buck.approve(bob, 50e6);

        uint256 aliceBefore = buck.balanceOf(alice);
        vm.prank(bob);
        buck.transferFrom(alice, pool, 10e6);

        assertEq(buck.balanceOf(alice), aliceBefore - 10e6, "owner debited");
        assertEq(buck.balanceOf(pool),  10e6,                "pool credited");
        assertEq(buck.allowance(alice, bob), 40e6,           "allowance spent");
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
