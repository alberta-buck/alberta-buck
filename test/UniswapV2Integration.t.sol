// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";

/// @dev Plain ERC-20 stand-in for USDC.  Lives in the test file so it does not
///      collide with the OpenZeppelin ERC20.json artifact path.
contract MockUSDC is ERC20 {
    constructor(uint256 supply) ERC20("Mock USD Coin", "USDC") {
        _mint(msg.sender, supply);
    }
}

interface IUniswapV2Factory {
    function createPair(address tokenA, address tokenB) external returns (address pair);
    function getPair(address tokenA, address tokenB) external view returns (address pair);
}

interface IUniswapV2Router02 {
    function factory() external view returns (address);
    function WETH() external view returns (address);
    function addLiquidity(
        address tokenA,
        address tokenB,
        uint amountADesired,
        uint amountBDesired,
        uint amountAMin,
        uint amountBMin,
        address to,
        uint deadline
    ) external returns (uint amountA, uint amountB, uint liquidity);
    function swapExactTokensForTokens(
        uint amountIn,
        uint amountOutMin,
        address[] calldata path,
        address to,
        uint deadline
    ) external returns (uint[] memory amounts);
}

interface IERC20Like {
    function approve(address spender, uint256 value) external returns (bool);
    function transfer(address to, uint256 value) external returns (bool);
    function balanceOf(address owner) external view returns (uint256);
    function totalSupply() external view returns (uint256);
}

interface IUniswapV2Pair {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function getReserves() external view returns (uint112, uint112, uint32);
    function sync() external;
    function skim(address to) external;
}

/// @title UniswapV2Integration.t.sol -- BUCK/USDC AMM round-trip on a locally
///        deployed Uniswap V2 stack.
///
/// @notice Validates that BUCK's identity-bound transfer rules co-exist with
///         a stock Uniswap V2 deployment: the pair and router are bound under
///         a Public Identity via `IdentityRegistry.bindContract`, which
///         (a) waives the receipt-fragment requirement on pair->user payouts
///         (Public-sender fallback inside `_identityCheckedTransfer`), and
///         (b) lets the user-side `transferFrom(alice, pair, ...)` succeed
///         via the Public-recipient fallback even when no prior CP receipt
///         from the user to the pair exists.
///
///         BUCK `approve(router, ...)` still requires a valid CP proof from
///         the user to the router (per the architectural constraint that
///         every approve carries a receipt the spender's operator can
///         decrypt for subpoena response).  We do not have CP fixtures
///         tailored to the locally-deployed router address, so the tests
///         seed BUCK allowances directly via `vm.store`, exercising the
///         AMM swap mechanics without bypassing CP enforcement in the
///         production approve() path (covered separately in Buck.t.sol).
contract UniswapV2IntegrationTest is Test {

    Buck                  internal buck;
    BuckCredit            internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;

    address internal constant GOV     = address(0xA0);
    address internal constant POOL    = address(0xBA51C);
    address internal constant CAROL   = address(0xCABE1);  // unverified outsider

    address internal alice;     // verified LP / swapper
    address internal bob;       // verified swapper

    address internal usdc;      // mock ERC-20 (v2-periphery test token)
    address internal weth;
    address internal factory;
    address internal router;
    address internal pair;      // BUCK / USDC

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

        // BUCK stack.
        credit = new BuckCredit();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));

        // Mock USDC: 18-decimal ERC-20 minted to this test contract.
        usdc = address(new MockUSDC(1_000_000_000e6));

        // WETH9 for router constructor (unused by BUCK/USDC swaps).
        weth = deployCode("out/WETH9.sol/WETH9.json");

        // Factory + Router.  feeToSetter = this test contract.
        factory = deployCode(
            "out/UniswapV2Factory.sol/UniswapV2Factory.json",
            abi.encode(address(this))
        );
        router = deployCode(
            "out/UniswapV2Router02.sol/UniswapV2Router02.json",
            abi.encode(factory, weth)
        );

        // Pre-create the BUCK/USDC pair.
        pair = IUniswapV2Factory(factory).createPair(address(buck), usdc);

        // Bind the pair and router under a Public Identity.  The operator
        // (GOV here) publicly attests off-chain that m_pair / m_router refer
        // to the canonical Uniswap V2 deployment for BUCK/USDC; auditors
        // recompute the deterministic _identityHash from the registry record
        // when reconciling pair-side transfer receipts.
        IdentityRegistry.ElGamalCT memory placeholderE = IdentityRegistry.ElGamalCT({
            R: BN254.g1(), C: BN254.g1()
        });
        reg.bindContract(pair,   BN254.g1(), placeholderE, true, true);
        reg.bindContract(router, BN254.g1(), placeholderE, true, true);

        // Mutual decryptability: private EOAs must CP-approve the public
        // router/pair so the operator can decrypt identities from receipts.
        // Both the router and the pair can appear as counterparties in BUCK
        // transfers during addLiquidity / swap / removeLiquidity.
        bytes32 _fragSlot =
            keccak256(abi.encode(pair, keccak256(abi.encode(alice, uint256(5)))));
        vm.store(address(buck), _fragSlot, bytes32(uint256(1)));
        _fragSlot =
            keccak256(abi.encode(pair, keccak256(abi.encode(bob, uint256(5)))));
        vm.store(address(buck), _fragSlot, bytes32(uint256(1)));
        _fragSlot =
            keccak256(abi.encode(router, keccak256(abi.encode(alice, uint256(5)))));
        vm.store(address(buck), _fragSlot, bytes32(uint256(1)));
        _fragSlot =
            keccak256(abi.encode(router, keccak256(abi.encode(bob, uint256(5)))));
        vm.store(address(buck), _fragSlot, bytes32(uint256(1)));

        // Distribute USDC from this test contract to Alice and Bob.
        IERC20Like(usdc).transfer(alice, 1_000_000e6);
        IERC20Like(usdc).transfer(bob,   1_000_000e6);

        // Mint BUCK to Alice and Bob via grantCredit + Buck.mint.
        _grantCredit(alice, 1_000_000e6);
        _grantCredit(bob,   1_000_000e6);
        vm.prank(alice);
        buck.mint(500_000e6);
        vm.prank(bob);
        buck.mint(500_000e6);
    }

    // ---- pair address sanity -----------------------------------------------

    function test_pairAddress_matchesPairForLibrary() public view {
        // The init-code hash baked into UniswapV2Library.pairFor() must agree
        // with the locally-compiled UniswapV2Pair creation bytecode, otherwise
        // router-internal `pairFor` calls (used by addLiquidity / swap) will
        // resolve to the wrong address and revert.
        address fromFactory = IUniswapV2Factory(factory).getPair(address(buck), usdc);
        assertEq(fromFactory, pair, "factory pair == pre-created pair");
    }

    // ---- addLiquidity ------------------------------------------------------

    function test_addLiquidity_fromVerifiedAlice() public {
        _aliceApproveRouter(100_000e6);

        uint256 buckBefore = buck.balanceOf(alice);
        uint256 usdcBefore = IERC20Like(usdc).balanceOf(alice);

        vm.prank(alice);
        (uint amtA, uint amtB, uint liq) = IUniswapV2Router02(router).addLiquidity(
            address(buck), usdc,
            100_000e6, 100_000e6,
            0, 0,
            alice,
            block.timestamp + 1
        );

        assertEq(amtA, 100_000e6, "BUCK side fully deposited");
        assertEq(amtB, 100_000e6, "USDC side fully deposited");
        assertGt(liq, 0, "Alice received LP tokens");
        assertEq(IERC20Like(pair).balanceOf(alice), liq, "LP balance = returned liquidity");

        // Pair holds the underlying.
        assertEq(buck.balanceOf(pair), 100_000e6, "pair holds BUCK");
        assertEq(IERC20Like(usdc).balanceOf(pair), 100_000e6, "pair holds USDC");

        // Alice's tokens debited.
        assertEq(buck.balanceOf(alice), buckBefore - 100_000e6, "Alice BUCK debited");
        assertEq(IERC20Like(usdc).balanceOf(alice), usdcBefore - 100_000e6, "Alice USDC debited");
    }

    // ---- swap BUCK -> USDC -------------------------------------------------

    function test_swap_BUCKtoUSDC_fromVerifiedBob() public {
        _seedPool();

        _bobApproveRouter(1_000e6);

        address[] memory path = new address[](2);
        path[0] = address(buck);
        path[1] = usdc;

        uint256 usdcBefore = IERC20Like(usdc).balanceOf(bob);
        vm.prank(bob);
        uint[] memory amounts = IUniswapV2Router02(router).swapExactTokensForTokens(
            1_000e6, 0, path, bob, block.timestamp + 1
        );
        assertEq(amounts[0], 1_000e6, "input BUCK == 1000");
        assertGt(amounts[1], 0,        "non-zero USDC out");
        assertEq(IERC20Like(usdc).balanceOf(bob), usdcBefore + amounts[1], "Bob USDC credited");
    }

    // ---- swap USDC -> BUCK (exercises the public->verified payout path) ---

    function test_swap_USDCtoBUCK_fromVerifiedBob() public {
        _seedPool();

        vm.prank(bob);
        IERC20Like(usdc).approve(router, 1_000e6);

        address[] memory path = new address[](2);
        path[0] = usdc;
        path[1] = address(buck);

        uint256 buckBefore = buck.balanceOf(bob);
        vm.prank(bob);
        uint[] memory amounts = IUniswapV2Router02(router).swapExactTokensForTokens(
            1_000e6, 0, path, bob, block.timestamp + 1
        );
        assertEq(amounts[0], 1_000e6, "input USDC == 1000");
        assertGt(amounts[1], 0,        "non-zero BUCK out");
        assertEq(buck.balanceOf(bob), buckBefore + amounts[1], "Bob BUCK credited from pair");
    }

    // ---- negative: unverified Carol cannot receive BUCK from a swap -------

    function test_swap_USDCtoBUCK_rejectsUnverifiedRecipient() public {
        _seedPool();

        // Give Carol some USDC and a router approval; she's still not in the
        // identity registry, so the pair's BUCK payout to her must revert.
        // The pair wraps the BUCK transfer in a try/_safeTransfer, so the
        // revert string surfaces as "UniswapV2: TRANSFER_FAILED".
        IERC20Like(usdc).transfer(CAROL, 10_000e6);
        vm.prank(CAROL);
        IERC20Like(usdc).approve(router, 1_000e6);

        address[] memory path = new address[](2);
        path[0] = usdc;
        path[1] = address(buck);

        vm.prank(CAROL);
        vm.expectRevert(bytes("UniswapV2: TRANSFER_FAILED"));
        IUniswapV2Router02(router).swapExactTokensForTokens(
            1_000e6, 0, path, CAROL, block.timestamp + 1
        );
    }

    // ---- negative: unverified Carol cannot put BUCK into the pool ---------

    function test_swap_BUCKtoUSDC_rejectsUnverifiedSender() public {
        _seedPool();
        // Carol has no BUCK (mint requires identity verification, so she
        // could never legitimately hold any).  The router's safeTransferFrom
        // would call buck.transferFrom(carol, pair, ...) which fails the
        // sender-verified check inside _identityCheckedTransfer.  We exercise
        // the same revert directly via buck.transfer to avoid the allowance
        // dance -- BUCK's demurrage override defeats forge-std's `deal`.
        vm.prank(CAROL);
        vm.expectRevert(bytes("BUCK: sender not verified"));
        buck.transfer(pair, 1_000e6);
    }

    // ---- Demurrage / Carrying-account interaction with V2 ------------------
    //
    // These tests validate the assumption that a Carrying account's
    // balanceOf does not decay with demurrage -- it returns the raw ERC-20
    // balance.  The accumulated fees are visible separately via
    // balanceOfFees(a) and are carried with outflows.  This is the only
    // semantic that lets stock Uniswap V2 work with BUCK over time:
    //
    //   * Pair's balanceOf == cached reserve (modulo legitimate inflows /
    //     outflows), so the K invariant remains satisfiable across long
    //     idle periods without periodic sync().
    //   * skim() never reverts on a "negative" surplus.
    //   * mint()/burn() proportions match what the router quoted.
    //
    // Demurrage on the pool's BUCK still accumulates -- it just rides with
    // outflows: when the pair carrying-transfers BUCK to a non-Carrying
    // recipient (e.g., a swapper), the recipient's _demurrage absorbs the
    // proportional carried fee, and the recipient's spendable = received raw
    // minus that carried fee.

    function test_decay_pairBalanceOfRetainsRawAcrossYear() public {
        _seedPool();
        uint256 pairBuckRaw = buck.rawBalanceOf(pair);
        uint256 pairBuckBalance = buck.balanceOf(pair);
        assertEq(pairBuckBalance, pairBuckRaw, "Carrying balanceOf == raw at deposit");

        vm.warp(block.timestamp + 365 days);

        assertEq(buck.rawBalanceOf(pair), pairBuckRaw, "raw unchanged across warp");
        assertEq(buck.balanceOf(pair),    pairBuckRaw, "Carrying balanceOf still == raw after 1yr");

        // Fees are observable separately and have grown.
        uint256 pairFees = buck.balanceOfFees(pair);
        assertGt(pairFees, 0, "balanceOfFees grew with time");
        // ~2% of 100k = ~2k after 1yr.
        assertApproxEqRel(pairFees, 2_000e6, 0.01e18, "fees ~2% of pair raw");
    }

    function test_decay_swapBUCKtoUSDC_succeedsAfterYear() public {
        _seedPool();
        _bobApproveRouter(1_000e6);

        vm.warp(block.timestamp + 365 days);

        // The pair's balanceOf is unchanged (Carrying semantics), so reserve
        // and balance still match -- swap proceeds at the cached price.
        address[] memory path = new address[](2);
        path[0] = address(buck);
        path[1] = usdc;

        uint256 usdcBefore = IERC20Like(usdc).balanceOf(bob);
        vm.prank(bob);
        uint[] memory amounts = IUniswapV2Router02(router).swapExactTokensForTokens(
            1_000e6, 0, path, bob, block.timestamp + 1
        );
        assertEq(amounts[0], 1_000e6, "input BUCK == 1000");
        assertGt(amounts[1], 0,        "non-zero USDC out after 1yr");
        assertEq(IERC20Like(usdc).balanceOf(bob), usdcBefore + amounts[1], "Bob USDC credited");
    }

    function test_decay_swapUSDCtoBUCK_succeedsAfterYear() public {
        _seedPool();
        vm.prank(bob);
        IERC20Like(usdc).approve(router, 1_000e6);

        vm.warp(block.timestamp + 365 days);

        address[] memory path = new address[](2);
        path[0] = usdc;
        path[1] = address(buck);

        uint256 bobBuckBefore = buck.balanceOf(bob);
        uint256 bobRawBefore  = buck.rawBalanceOf(bob);
        vm.prank(bob);
        uint[] memory amounts = IUniswapV2Router02(router).swapExactTokensForTokens(
            1_000e6, 0, path, bob, block.timestamp + 1
        );
        assertEq(amounts[0], 1_000e6, "input USDC == 1000");
        assertGt(amounts[1], 0,        "non-zero BUCK out after 1yr");

        // Bob (non-Carrying EOA) receives the gross amount as raw, but his
        // spendable rises by less (recipient absorbs the pair's carried fee).
        uint256 bobBuckAfter = buck.balanceOf(bob);
        assertGt(bobBuckAfter, bobBuckBefore, "bob's spendable increased");

        uint256 bobRawDelta = buck.rawBalanceOf(bob) - bobRawBefore;
        assertEq(bobRawDelta, amounts[1], "bob raw rose by gross swap output");
        // Spendable rise < raw rise: the difference is the carried fee.
        assertLt(bobBuckAfter - bobBuckBefore, bobRawDelta,
            "bob's spendable rise < raw rise (carried fee absorbed)");
    }

    function test_decay_skimDoesNotRevert() public {
        _seedPool();

        vm.warp(block.timestamp + 365 days);

        // Under Carrying semantics balanceOf(BUCK, pair) == reserve0, so
        // skim's `balance.sub(reserve)` is zero, not underflow.  USDC is a
        // plain ERC-20 with no decay; its sub is also zero.  skim succeeds
        // and transfers nothing.  The destination must still be a verified
        // BUCK recipient because skim invokes BUCK.transfer (even with
        // value=0, BUCK's identity check fires); use alice.
        IUniswapV2Pair(pair).skim(alice);
    }

    function test_decay_addLiquidityAfterYear_noSyncNeeded() public {
        _seedPool();
        uint256 aliceLP = IERC20Like(pair).balanceOf(alice);

        vm.warp(block.timestamp + 365 days);

        // Bob adds the same proportional liquidity 1yr later, no sync first.
        // Under Carrying semantics, pair's balanceOf still matches reserves,
        // so the router's quoting is correct and Bob's deposit is measured
        // accurately -- he should get the same shares Alice did, modulo the
        // MINIMUM_LIQUIDITY locked at first mint.
        _setBuckAllowance(bob, router, 100_000e6);
        vm.prank(bob);
        IERC20Like(usdc).approve(router, 100_000e6);

        vm.prank(bob);
        (uint amtA, uint amtB, uint bobLP) = IUniswapV2Router02(router).addLiquidity(
            address(buck), usdc,
            100_000e6, 100_000e6,
            0, 0,
            bob,
            block.timestamp + 1
        );

        assertEq(amtA, 100_000e6, "Bob deposits full BUCK side");
        assertEq(amtB, 100_000e6, "Bob deposits full USDC side");
        // Bob's LP shares should be proportional to his deposit, modulo the
        // 1000-wei MINIMUM_LIQUIDITY locked at Alice's first mint.  Bob
        // gets ~aliceLP + 1000.
        assertApproxEqAbs(bobLP, aliceLP + 1000, 1, "Bob LP shares ~= Alice LP shares + MIN_LIQUIDITY");
    }

    // ---- helpers -----------------------------------------------------------

    function _seedPool() internal {
        _aliceApproveRouter(100_000e6);
        vm.prank(alice);
        IUniswapV2Router02(router).addLiquidity(
            address(buck), usdc,
            100_000e6, 100_000e6,
            0, 0, alice,
            block.timestamp + 1
        );
    }

    /// @dev Seed the ERC-20 allowance owner -> spender by writing directly to
    ///      Buck's `_allowances` slot (slot 1 in the OpenZeppelin layout).
    ///      This sidesteps `buck.approve(...)` so the test does not need a
    ///      contract-pinned CP proof fixture (production approve() still
    ///      requires CP -- exercised in Buck.t.sol).
    function _setBuckAllowance(address owner_, address spender, uint256 amount) internal {
        // _allowances at slot 2 in packed-state Buck layout.
        bytes32 slot = keccak256(
            abi.encode(spender, keccak256(abi.encode(owner_, uint256(2))))
        );
        vm.store(address(buck), slot, bytes32(amount));
    }

    function _aliceApproveRouter(uint256 amount) internal {
        _setBuckAllowance(alice, router, amount);
        vm.prank(alice);
        IERC20Like(usdc).approve(router, amount);
    }

    function _bobApproveRouter(uint256 amount) internal {
        _setBuckAllowance(bob, router, amount);
    }

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

    // ---- JSON helpers (mirrors Buck.t.sol) ---------------------------------

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
        reg.trustIssuer(ISSUER(), ipk);
    }

    function ISSUER() internal pure returns (address) { return address(0x1551E1); }

    function _registerAlice() internal {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        vm.prank(alice);
        reg.register(ISSUER(), pk, E, _ps("alice"), _regProof("alice"));
    }

    function _registerBob() internal {
        BN254.G1Point memory pk = _g1(".bob.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".bob.ciphertext");
        vm.prank(bob);
        reg.register(ISSUER(), pk, E, _ps("bob"), _regProof("bob"));
    }
}
