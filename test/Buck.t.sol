// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {Vm}   from "forge-std/Vm.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckKControllerDirect} from "../src/BuckKControllerDirect.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";
import {MockBasket} from "./mocks/MockBasket.sol";

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
        credit.setBuck(address(buck));
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

    /// @dev Write a receipt fragment directly to Buck's storage, simulating the
    ///      effect of a CP-bound approve without needing a proof fixture for the
    ///      (from, to) pair.  _receiptFragments is at slot 5 in Buck's layout.
    function _setReceiptFragment(address from, address to, bytes32 value) internal {
        bytes32 slot = keccak256(abi.encode(to, keccak256(abi.encode(from, uint256(5)))));
        vm.store(address(buck), slot, value);
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

    /// @dev Under Phase 1b semantics, balanceOf(non-Carrying holder) ==
    ///      held + max(0, creditLimit - debt).  When the holder has been
    ///      mint()'ed against (signed raw == -principal), this collapses to
    ///      creditLimit - principal.  Helper for assertions that previously
    ///      expected `balanceOf == amount` post-mint.
    function _expectedHolderBalance(address holder, uint256 poolPrincipal)
        internal view returns (uint256)
    {
        uint256 limit = buck.creditLimit(holder);
        return limit > poolPrincipal ? limit - poolPrincipal : 0;
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
        // Alice has no BuckCredit NFT yet -> insufficient credit allocation.
        vm.prank(alice);
        vm.expectRevert(bytes("BUCK: insufficient credit allocation"));
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
        // Phase 1b: mint does NOT deliver `amount` to the holder.  It opens
        // credit headroom: alice's signed raw drops to -poolPrincipal and her
        // balanceOf (held + unused credit) equals creditLimit - poolPrincipal.
        assertEq(buck.signedRawBalanceOf(alice), -int256(uint256(5_263_158)),
                 "alice's signed raw = -principal");
        assertEq(buck.balanceOf(alice), 1000e6 - 5_263_158, "alice spendable = limit - debt");
        assertEq(buck.balanceOf(POOL),  5_263_158,         "pool principal = annual_premium * 10");
        assertEq(buck.totalSupply(),    5_263_158,         "total = poolPrincipal (alice contributes 0 positive)");
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
        uint256 limit = buck.creditLimit(alice);    // 200e6 (sum of both activated NFTs)
        assertEq(buck.mintsBacked(cheap), 84_210_527, "cheap NFT consumed first");
        assertEq(buck.mintsBacked(dear),  0,          "dear NFT untouched");
        assertEq(buck.balanceOf(POOL),    4_210_527,  "pool principal");
        assertEq(buck.signedRawBalanceOf(alice), -int256(uint256(4_210_527)), "alice debt = principal");
        assertEq(buck.balanceOf(alice),   limit - 4_210_527,  "alice spendable = limit - debt");
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
        uint256 limit = buck.creditLimit(alice);
        assertEq(buck.mintsBacked(cheap), 100e6,      "cheap exhausted");
        assertEq(buck.mintsBacked(dear),  68_750_000, "spillover to dear NFT");
        assertEq(buck.balanceOf(POOL),    18_750_000, "pool = 5e6 + 13.75e6");
        assertEq(buck.signedRawBalanceOf(alice), -int256(uint256(18_750_000)));
        assertEq(buck.balanceOf(alice),   limit - 18_750_000);
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
        uint256 limit = buck.creditLimit(alice);
        assertEq(buck.mintsBacked(dear),  100e6, "dear NFT drawn first per caller order");
        assertEq(buck.mintsBacked(cheap), 0);
        assertEq(buck.balanceOf(POOL),    20e6, "100e6 take * 200bp * 10 / BP = 20e6");
        assertEq(buck.signedRawBalanceOf(alice), -int256(uint256(20e6)));
        assertEq(buck.balanceOf(alice),   limit - 20e6);
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

    function test_creditLimit_tracksBuckK() public {
        // Phase 1b: creditLimit is live (no storedLimit ratchet).  Lowering
        // BUCK_K shrinks the holder's credit headroom in proportion.
        _grantCredit(alice, 1000e6);
        assertEq(buck.creditLimit(alice), 1000e6, "K=1.0: limit == activated");

        vm.prank(GOV);
        kCtrl.setBuckK(0.5e18);
        // The cache is per-block; bump the block so the next read recomputes
        // against the new K.  (BUCK_K changes don't fire BuckCredit hooks.)
        vm.roll(block.number + 1);
        assertEq(buck.creditLimit(alice), 500e6, "K=0.5: limit halves");
    }

    function test_mint_rejectsWhenAggregatedExceedsLimit() public {
        _grantCredit(alice, 100e6);
        vm.prank(alice);
        buck.mint(50e6);
        vm.prank(alice);
        // The second mint tries to draw 60e6 net.  At 50bp+10x, that wants
        // take=63.16e6; combined with the first mint's take=52.63e6 the
        // total would be 115.79e6 > 100e6 face -> _allocateMint runs out
        // of capacity before satisfying the remaining draw.
        vm.expectRevert(bytes("BUCK: insufficient credit allocation"));
        buck.mint(60e6);
    }

    // ---- burn --------------------------------------------------------------

    function test_burn_reducesDebt() public {
        // Phase 1b: burn() repays principal -- signed raw climbs toward zero.
        // The user's spendable (balanceOf) GROWS by `refund_i` because their
        // unused credit headroom expands as debt shrinks.
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);
        int256 signedBefore = buck.signedRawBalanceOf(alice);  // ~ -5_263_158

        vm.prank(alice);
        buck.burn(10e6);

        // unwind = ceil(10e6 * 10000 / 9500) = 10_526_316
        // refund = unwind - 10e6              =     526_316
        // alice's signed raw climbs by refund: -5_263_158 + 526_316 = -4_736_842
        int256 signedAfter = buck.signedRawBalanceOf(alice);
        assertEq(signedAfter - signedBefore, int256(uint256(526_316)),
                 "burn refunds principal: signed raw climbs by refund_i");
    }

    function test_burn_refundsPoolPrincipalProportionally() public {
        uint256 tid = _grantCredit(alice, 1000e6);          // 50bp NFT
        vm.prank(alice);
        buck.mint(100e6);
        uint256 backedAfterMint = buck.mintsBacked(tid);    // 105_263_158
        uint256 poolAfterMint   = buck.balanceOf(POOL);     //   5_263_158
        int256  signedAfterMint = buck.signedRawBalanceOf(alice);

        vm.prank(alice);
        buck.burn(10e6);

        // Inverse of mint at the same rate:
        //   unwind = ceil(10e6 * 10000 / 9500) = 10_526_316
        //   refund = unwind - 10e6              =     526_316
        assertEq(buck.signedRawBalanceOf(alice), signedAfterMint + int256(uint256(526_316)),
                 "alice's debt shrinks by refund_i");
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
        int256 signedBefore = buck.signedRawBalanceOf(alice);
        vm.prank(alice);
        buck.burn(10e6, order);
        // refund_cheap = ceil(10e6*10000/9500) - 10e6 = 526_316
        assertEq(buck.signedRawBalanceOf(alice), signedBefore + int256(uint256(526_316)),
                 "alice debt shrinks by refund_i (cheap-rate unwind)");
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

        int256 signedBefore = buck.signedRawBalanceOf(alice);
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
        assertEq(buck.signedRawBalanceOf(alice), signedBefore + int256(uint256(12_500_000)),
                 "alice debt shrinks by dear-rate refund");
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
        int256 signedBefore = buck.signedRawBalanceOf(alice);
        vm.prank(alice);
        buck.burn(100e6);

        assertEq(buck.mintsBacked(dear),  0,                          "dear fully unwound");
        assertEq(buck.mintsBacked(cheap), 100e6 - 47_368_422,         "cheap partially unwound");
        assertEq(buck.signedRawBalanceOf(alice), signedBefore + int256(uint256(16_118_422)),
                 "alice debt shrinks by total refund");
        // 18_750_000 minted to pool initially; 16_118_422 refunded.
        assertEq(buck.balanceOf(POOL),    18_750_000 - 16_118_422,    "pool refund spans both NFTs");
    }

    function test_quoteMint_multiRate_matchesExecution() public {
        uint256 cheap = _grantCreditAtRate(alice, 100e6, 50);
        uint256 dear  = _grantCreditAtRate(alice, 200e6, 200);

        // Quote with a mixed cheap-dear order.
        uint256[] memory order = new uint256[](2);
        order[0] = cheap;
        order[1] = dear;

        (uint256 quotedCoverage, uint256 quotedPrincipal) = buck.quoteMint(250e6, order);

        vm.prank(alice);
        buck.mint(250e6, order);

        uint256 totalBacked = buck.mintsBacked(cheap) + buck.mintsBacked(dear);
        assertEq(totalBacked, quotedCoverage, "coverage matches quote");
        assertEq(buck.balanceOf(POOL), quotedPrincipal, "principal matches quote");
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

    // ---- setBasket ----------------------------------------------------------

    function test_setBasket_onlyInsurancePool() public {
        Buck fresh = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.expectRevert("BUCK: not insurancePool");
        fresh.setBasket(address(0xDECAF));
    }

    function test_setBasket_oneShot() public {
        Buck fresh = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(POOL);
        fresh.setBasket(address(0xB0CC));
        vm.prank(POOL);
        vm.expectRevert("BUCK: basket already set");
        fresh.setBasket(address(0xB0DD));
    }

    function test_setBasket_rejectsZero() public {
        Buck fresh = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(POOL);
        vm.expectRevert("BUCK: basket=0");
        fresh.setBasket(address(0));
    }

    // ---- burn: all NFTs over-rate -------------------------------------------

    function test_burn_revertsWhenAllNFTsOverRate() public {
        // Mint against a reasonable-rate NFT, then the insurer bumps the
        // premium rate so high that effRate >= BP.  On burn, the NFT is
        // silently skipped and the burn fails with insufficient coverage.
        uint256 tid = _grantCreditAtRate(alice, 100e6, 100);  // effRate=1000

        vm.prank(alice);
        buck.mint(50e6);

        // Insurer reappraises: bumps premium rate to 1000 (effRate=10000 == BP).
        vm.prank(address(this));  // insurer == credit creator
        credit.updateCredit(
            tid, 100e6, 100e6,
            BuckCredit.DepreciationType.NONE, 0, 0,
            1000  // premiumRate = 1000 → effRate = 1000*10 = 10000 = BP → skipped
        );

        uint256[] memory order = new uint256[](1);
        order[0] = tid;
        vm.prank(alice);
        vm.expectRevert("BUCK: insufficient coverage to unwind");
        buck.burn(10e6, order);
    }

    // ---- funding factor gate ------------------------------------------------

    /// @dev DISABLED under Phase 1b semantics.
    ///
    /// The funding-factor gate guards on `balanceOf(minter) >= poolPrincipal
    /// * factor / 1e18`.  Phase 1b's balanceOf now equals `held + unused
    /// credit headroom` -- so a holder with a fresh BuckCredit NFT trivially
    /// satisfies the gate (their full credit limit minus current debt is
    /// available as "spendable").  The gate as written cannot reject under
    /// reasonable parameter choices and the test is obsolete.
    ///
    /// Two follow-ups for the equilibrium scenario:
    ///   - Re-spec the gate against signedRawBalanceOf (held positive only),
    ///     making it a "have you actually paid down some debt?" check, OR
    ///   - Re-spec against creditLimit so it gates "how much of your
    ///     remaining headroom you can lock per mint".
    /// Both deferred to the dynamic-issuance sim (Track 3), which will
    /// inform which semantic is most useful in practice.
    function _skip_test_mint_revertsWhenFundingFactorUnsatisfied() internal {
        // Deploy a fresh stack: new IdentityRegistry, controller with real
        // funding factor, and Buck.  Alice needs a fresh registration.
        IdentityRegistry r = new IdentityRegistry(GOV);
        BuckKControllerDirect kc = new BuckKControllerDirect(
            0.1e18, 0.01e18, 0,
            60,
            0.50e18, 1.50e18,
            1.0e18,
            GOV
        );
        Buck b = new Buck(address(credit), address(kc), address(r), POOL);
        vm.prank(GOV);
        r.setBuck(address(b));

        // Register Alice on the fresh registry.
        {
            BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
            IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
            // Need a trusted issuer on the fresh registry.
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
            r.trustIssuer(ISSUER, ipk);
            vm.prank(alice);
            r.register(ISSUER, pk, E, _ps("alice"), _regProof("alice"));
        }

        // Wire a mock basket signalling inflation (basket > 1.0 BUCK).
        MockBasket basket = new MockBasket();
        basket.setBasketValue(int256(1.05e18));
        vm.prank(GOV);
        kc.setBasket(address(basket));

        // Advance time past dT so the PID cycle runs.
        vm.warp(block.timestamp + 61);
        kc.compute();
        uint256 factor = kc.fundingFactor();
        assertGt(factor, 0, "funding factor should be positive");

        // Alice has a credit NFT.  She mints some initial BUCK.
        uint256 tid = _grantCredit(alice, 1000e6);
        vm.prank(alice);
        b.mint(1e6);

        // Compute again to get a non-zero funding factor.
        vm.warp(block.timestamp + 61);
        kc.compute();

        // Now try to mint more — the funding factor requires balanceOf >=
        // poolPrincipal * factor / 1e18, but alice only has 1e6 BUCK.  The
        // poolPrincipal from a 100e6 mint at 50bp is ~526k, and with factor
        // ~1.48, the required balance is ~780k.  Alice only has 1e6 — hmm,
        // that might actually pass.  Let me mint a larger amount.
        // poolPrincipal for 500e6 at 50bp = 500e6 * (BP/(BP-500) - 1) ≈ 26.3e6.
        // required = 26.3e6 * 1.48 / 1e18 ≈ 38.9e6.  Alice only has 1e6 → reverts.
        vm.prank(alice);
        vm.expectRevert("BUCK: insufficient mint funding");
        b.mint(500e6);
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
        vm.expectRevert(bytes("BUCK: sender must identity-approve recipient"));
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

        // Mutual decryptability: the private sender must CP-approve the
        // public recipient so the pool operator can decrypt Alice's identity
        // from the receipt under subpoena.
        _setReceiptFragment(alice, pool, bytes32(uint256(1)));

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
        vm.expectRevert(bytes("BUCK: sender must identity-approve recipient"));
        buck.transfer(bob, 1e6);
    }

    function test_transfer_succeedsAfterApprove() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);

        // Alice CP-approves Bob (sender → recipient direction).
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        vm.prank(alice);
        buck.approve(bob, 50e6, E_b, _cpProof());

        // Bob must also CP-approve Alice (recipient → sender direction) for
        // the bilateral identity invariant to hold: both private parties need
        // a per-pair receipt fragment so each can decrypt the other's identity.
        // Simulate Bob's approve by writing his receipt fragment directly
        // (no Bob→Alice CP proof exists in the shared test vectors).
        _setReceiptFragment(bob, alice, bytes32(uint256(1)));

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

        // Public-Identity contract recipient: the private sender must still
        // CP-approve the pool so the pool operator can decrypt the sender's
        // identity (mutual decryptability).  The pool side falls back to
        // identityHash (it is public, so no CP needed from the pool).
        address pool = address(0xDECAF);
        _bindPublicIdentity(pool);
        _setReceiptFragment(alice, pool, bytes32(uint256(1)));
        vm.prank(alice);
        buck.transfer(pool, 5e6);
        assertEq(buck.balanceOf(pool), 5e6);
    }

    function test_transferFrom_consumesAllowance() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);

        // Alice CP-approves Bob (sender → spender direction).
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        vm.prank(alice);
        buck.approve(bob, 50e6, E_b, _cpProof());

        // Bob must also CP-approve Alice for the bilateral invariant (both private).
        _setReceiptFragment(bob, alice, bytes32(uint256(1)));

        vm.prank(bob);
        buck.transferFrom(alice, bob, 25e6);
        assertEq(buck.allowance(alice, bob), 25e6);
        assertEq(buck.balanceOf(bob),        25e6);
    }

    // ---- receipt-hash invariants across transfer combinations ---------------

    /// @dev Verify that BuckTransferReceipt events carry the correct hash
    ///      values for each transfer combination, matching the identity
    ///      fallback rules in _identityCheckedTransfer.
    ///
    ///      Both parties are private EOAs: bilateral CP-approve required.
    function test_receiptHashes_EOAtoEOA_bilateralCPApprove() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);

        // Alice CP-approves Bob (sender → recipient).
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        vm.prank(alice);
        buck.approve(bob, 50e6, E_b, _cpProof());
        bytes32 aliceForBobFrag = buck.receiptFragment(alice, bob);

        // Bob CP-approves Alice (recipient → sender) — simulated.
        bytes32 bobForAliceFrag = keccak256("bob-for-alice");
        _setReceiptFragment(bob, alice, bobForAliceFrag);

        vm.recordLogs();
        vm.prank(alice);
        buck.transfer(bob, 10e6);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        bytes32 eventSig = keccak256("BuckTransferReceipt(address,address,uint256,bytes32,bytes32)");
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].topics[0] == eventSig) {
                // Non-indexed params: (uint256 amount, bytes32 fromCipherHash, bytes32 toCipherHash)
                (/*amount*/, bytes32 fromHash, bytes32 toHash) =
                    abi.decode(logs[i].data, (uint256, bytes32, bytes32));
                assertEq(toHash, aliceForBobFrag, "toHash: CP fragment");
                assertEq(fromHash, bobForAliceFrag, "fromHash: CP fragment");
                return;
            }
        }
        fail("BuckTransferReceipt event not found");
    }

    function test_receiptHashes_EOAtoPublicPool_senderCPFragment() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);

        address pool = address(0xDECAF);
        _bindPublicIdentity(pool);

        // Mutual decryptability: private sender must CP-approve the public
        // pool so the pool operator can decrypt Alice's identity.
        bytes32 aliceForPool = keccak256("alice-for-pool");
        _setReceiptFragment(alice, pool, aliceForPool);

        vm.recordLogs();
        vm.prank(alice);
        buck.transfer(pool, 5e6);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        bytes32 eventSig = keccak256("BuckTransferReceipt(address,address,uint256,bytes32,bytes32)");
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].topics[0] == eventSig) {
                (/*amount*/, bytes32 fromHash, bytes32 toHash) =
                    abi.decode(logs[i].data, (uint256, bytes32, bytes32));
                // toHash: Alice→pool CP fragment (sender's identity for pool)
                assertEq(toHash, aliceForPool, "toHash: sender CP fragment");
                // fromHash: pool didn't CP-approve alice, but pool is public → identityHash(alice)
                assertEq(fromHash, _idHashOf(alice), "fromHash: alice identityHash (pool public)");
                return;
            }
        }
        fail("BuckTransferReceipt event not found");
    }

    function test_receiptHashes_PublicPoolToEOA_recipientCPFragment() public {
        _grantCredit(alice, 1000e6);
        vm.prank(alice);
        buck.mint(100e6);

        address pool = address(0xDECAF);
        _bindPublicIdentity(pool);

        // Alice CP-approves pool so she can seed it.
        _setReceiptFragment(alice, pool, keccak256("alice-for-pool"));
        vm.prank(alice);
        buck.transfer(pool, 20e6);

        // Bob CP-approves pool (mutual decryptability: private recipient
        // must CP-approve public sender so pool operator can decrypt Bob).
        bytes32 bobForPool = keccak256("bob-for-pool");
        _setReceiptFragment(bob, pool, bobForPool);

        // Pool pays out to Bob.
        vm.recordLogs();
        vm.prank(pool);
        buck.transfer(bob, 7e6);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        bytes32 eventSig = keccak256("BuckTransferReceipt(address,address,uint256,bytes32,bytes32)");
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].topics[0] == eventSig) {
                (/*amount*/, bytes32 fromHash, bytes32 toHash) =
                    abi.decode(logs[i].data, (uint256, bytes32, bytes32));
                // toHash: pool is public sender → identityHash(bob) fallback
                assertEq(toHash, _idHashOf(bob), "toHash: bob identityHash");
                // fromHash: Bob→pool CP fragment (private recipient's identity)
                assertEq(fromHash, bobForPool, "fromHash: recipient CP fragment");
                return;
            }
        }
        fail("BuckTransferReceipt event not found");
    }

    /// @dev helper to compute _identityHash the same way Buck.sol does
    function _idHashOf(address a) internal view returns (bytes32) {
        BN254.G1Point memory pk = reg.pkOf(a);
        IdentityRegistry.ElGamalCT memory E = reg.ciphertextOf(a);
        return keccak256(abi.encode(pk.X, pk.Y, E.R.X, E.R.Y, E.C.X, E.C.Y));
    }
}
