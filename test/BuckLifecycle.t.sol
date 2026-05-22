// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";

/// @dev Stand-in for USDC.  Name differs from MockUSDC in UniswapV2Integration
///      so both files can coexist in the same Forge project without name collisions.
contract LifecycleUSDC is ERC20 {
    constructor() ERC20("Mock USDC", "USDC") {
        _mint(msg.sender, 10_000_000e6);
    }
}

// Minimal Uniswap V2 interfaces (identical to UniswapV2Integration.t.sol).
interface IUniswapV2Factory {
    function createPair(address, address) external returns (address);
    function getPair(address, address) external view returns (address);
}
interface IUniswapV2Router02 {
    function addLiquidity(address, address, uint, uint, uint, uint, address, uint)
        external returns (uint, uint, uint);
    function removeLiquidity(address, address, uint, uint, uint, address, uint)
        external returns (uint, uint);
    function swapExactTokensForTokens(uint, uint, address[] calldata, address, uint)
        external returns (uint[] memory);
}
interface IERC20Like {
    function approve(address, uint256) external returns (bool);
    function transfer(address, uint256) external returns (bool);
    function transferFrom(address, address, uint256) external returns (bool);
    function balanceOf(address) external view returns (uint256);
    function totalSupply() external view returns (uint256);
}
interface IUniswapV2Pair {
    function token0() external view returns (address);
    function getReserves() external view returns (uint112, uint112, uint32);
}

/// @title BuckLifecycleTest
/// @notice End-to-end lifecycle: vehicle insurance BUCK_CREDIT → mint BUCK →
///         seed Uniswap V2 pool → 12 monthly USDC→BUCK swaps by Bob →
///         Alice withdraws LP → burns BUCK.
///         Emits test/vectors/lifecycle.json for the org-mode Python visualisation.
contract BuckLifecycleTest is Test {

    Buck                  internal buck;
    BuckCredit            internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;

    address internal usdc;
    address internal weth;
    address internal factory;
    address internal router;
    address internal pair;

    address internal alice;
    address internal bob;

    address internal constant GOV  = address(0xA0);
    address internal constant POOL = address(0xBA51C);

    uint256 internal tokenId;
    bool    internal creditExists;
    string  internal vj;

    // ── snapshot storage ──────────────────────────────────────────────────────
    string[]  internal s_labels;
    uint256[] internal s_t;
    uint256[] internal s_alice_buck;    // balanceOf (spendable)
    uint256[] internal s_alice_raw;     // rawBalanceOf
    uint256[] internal s_alice_usdc;
    uint256[] internal s_alice_lp;
    uint256[] internal s_pool_buck;     // pair BUCK reserve
    uint256[] internal s_pool_usdc;     // pair USDC reserve
    uint256[] internal s_jubilee;       // jubileeActual (raw)
    uint256[] internal s_supply;        // totalSupply
    uint256[] internal s_credit_val;    // currentValue(tokenId)
    uint256[] internal s_bob_buck;      // balanceOf (spendable)
    uint256[] internal s_bob_usdc;

    // ── setUp ─────────────────────────────────────────────────────────────────

    function setUp() public {
        vm.chainId(1);
        vj = vm.readFile("test/vectors/identity.json");

        // Identity layer.
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

        // Mock USDC.
        usdc = address(new LifecycleUSDC());

        // Uniswap V2 stack (same artifacts as UniswapV2Integration.t.sol).
        weth    = deployCode("out/WETH9.sol/WETH9.json");
        factory = deployCode("out/UniswapV2Factory.sol/UniswapV2Factory.json",
                             abi.encode(address(this)));
        router  = deployCode("out/UniswapV2Router02.sol/UniswapV2Router02.json",
                             abi.encode(factory, weth));

        pair = IUniswapV2Factory(factory).createPair(address(buck), usdc);

        // Bind pair and router as Public-Identity / Carrying entities.
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT({
            R: BN254.g1(), C: BN254.g1()
        });
        reg.bindContract(pair,   BN254.g1(), E, /*isPublicIdentity=*/true, /*isCarrying=*/true);
        reg.bindContract(router, BN254.g1(), E, true, true);

        // Mutual decryptability: private EOAs must CP-approve the public
        // router/pair so the operator can decrypt identities from receipts.
        bytes32 fragSlot =
            keccak256(abi.encode(pair, keccak256(abi.encode(alice, uint256(5)))));
        vm.store(address(buck), fragSlot, bytes32(uint256(1)));
        fragSlot =
            keccak256(abi.encode(pair, keccak256(abi.encode(bob, uint256(5)))));
        vm.store(address(buck), fragSlot, bytes32(uint256(1)));
        fragSlot =
            keccak256(abi.encode(router, keccak256(abi.encode(alice, uint256(5)))));
        vm.store(address(buck), fragSlot, bytes32(uint256(1)));
        fragSlot =
            keccak256(abi.encode(router, keccak256(abi.encode(bob, uint256(5)))));
        vm.store(address(buck), fragSlot, bytes32(uint256(1)));

        // Fund Alice with USDC for pool seeding and Bob with USDC for swaps.
        IERC20Like(usdc).transfer(alice, 10_000e6);   // 5k for pool + 5k spare
        IERC20Like(usdc).transfer(bob,    6_000e6);   // 12 × 500 USDC swaps
    }

    // ── lifecycle scenario ────────────────────────────────────────────────────

    /// @notice Full lifecycle: insurance → mint → AMM LP → 12 monthly swaps →
    ///         remove LP → burn.  Emits test/vectors/lifecycle.json.
    function test_lifecycle() public {
        _snap("pre-credit");

        // ── 1. Alice insures her $10,000 vehicle ─────────────────────────────
        // Declining-balance 15 %/yr, 5 % salvage floor, 1.5 % annual premium.
        tokenId = credit.createCredit(
            alice,
            0,                                              // assetClass
            10_000e6,                                       // faceValue
            500e6,                                          // depreciationFloor (5 %)
            BuckCredit.DepreciationType.DECLINING_BALANCE,
            1500,                                           // 15 % per year
            uint48(block.timestamp),                        // starts now
            150                                             // 1.5 % annual premium
        );
        creditExists = true;
        vm.prank(alice);
        credit.activate(tokenId, 10_000e6);
        _snap("credit-created");

        // ── 2. Alice mints 5,000 BUCK against the credit ─────────────────────
        vm.prank(alice);
        buck.mint(5_000e6);
        _snap("buck-minted");

        // ── 3. Alice seeds the BUCK/USDC V2 pool 1:1 ────────────────────────
        _setBuckAllowance(alice, router, 5_000e6);
        vm.prank(alice);
        IERC20Like(usdc).approve(router, 5_000e6);
        vm.prank(alice);
        IUniswapV2Router02(router).addLiquidity(
            address(buck), usdc,
            5_000e6, 5_000e6,
            0, 0, alice,
            block.timestamp + 1
        );
        _snap("pool-seeded");

        // ── 4. Bob makes 12 monthly USDC → BUCK swaps ────────────────────────
        address[] memory path = new address[](2);
        path[0] = usdc;
        path[1] = address(buck);

        for (uint256 i = 0; i < 12; i++) {
            vm.warp(block.timestamp + 30 days);
            vm.prank(bob);
            IERC20Like(usdc).approve(router, 500e6);
            vm.prank(bob);
            IUniswapV2Router02(router).swapExactTokensForTokens(
                500e6, 0, path, bob, block.timestamp + 1
            );
            _snap(string.concat("swap-", vm.toString(i + 1)));
        }

        // ── 5. Alice removes all liquidity (year end) ────────────────────────
        vm.warp(block.timestamp + 5 days);   // ~365 days total
        uint256 aliceLp = IERC20Like(pair).balanceOf(alice);
        vm.prank(alice);
        IERC20Like(pair).approve(router, aliceLp);
        vm.prank(alice);
        IUniswapV2Router02(router).removeLiquidity(
            address(buck), usdc,
            aliceLp, 0, 0, alice,
            block.timestamp + 1
        );
        _snap("liquidity-removed");

        // ── 6. Alice burns her spendable BUCK, releasing coverage ────────────
        uint256 burnAmt = buck.balanceOf(alice);
        if (burnAmt > 0) {
            vm.prank(alice);
            buck.burn(burnAmt);
        }
        _snap("buck-burned");

        _writeJson();
    }

    // ── snapshot helper ───────────────────────────────────────────────────────

    function _snap(string memory label) internal {
        s_labels.push(label);
        s_t.push(block.timestamp);
        s_alice_buck.push(buck.balanceOf(alice));
        s_alice_raw.push(buck.rawBalanceOf(alice));
        s_alice_usdc.push(IERC20Like(usdc).balanceOf(alice));
        s_alice_lp.push(IERC20Like(pair).balanceOf(alice));

        (uint112 r0, uint112 r1,) = IUniswapV2Pair(pair).getReserves();
        bool buckFirst = IUniswapV2Pair(pair).token0() == address(buck);
        s_pool_buck.push(buckFirst ? r0 : r1);
        s_pool_usdc.push(buckFirst ? r1 : r0);

        s_jubilee.push(buck.jubileeActual());
        s_supply.push(buck.totalSupply());
        s_credit_val.push(creditExists ? credit.currentValue(tokenId) : 0);
        s_bob_buck.push(buck.balanceOf(bob));
        s_bob_usdc.push(IERC20Like(usdc).balanceOf(bob));
    }

    // ── JSON output ───────────────────────────────────────────────────────────

    function _jUint(string memory key, uint256[] storage arr) internal view
        returns (string memory)
    {
        string memory s = string.concat('"', key, '":[');
        for (uint256 i = 0; i < arr.length; i++) {
            if (i > 0) s = string.concat(s, ",");
            s = string.concat(s, vm.toString(arr[i]));
        }
        return string.concat(s, "]");
    }

    function _jStr(string memory key, string[] storage arr) internal view
        returns (string memory)
    {
        string memory s = string.concat('"', key, '":[');
        for (uint256 i = 0; i < arr.length; i++) {
            if (i > 0) s = string.concat(s, ",");
            s = string.concat(s, '"', arr[i], '"');
        }
        return string.concat(s, "]");
    }

    function _writeJson() internal {
        string memory j = "{";
        j = string.concat(j, _jStr( "labels",    s_labels),    ",");
        j = string.concat(j, _jUint("t",         s_t),         ",");
        j = string.concat(j, _jUint("alice_buck", s_alice_buck),",");
        j = string.concat(j, _jUint("alice_raw",  s_alice_raw), ",");
        j = string.concat(j, _jUint("alice_usdc", s_alice_usdc),",");
        j = string.concat(j, _jUint("alice_lp",   s_alice_lp),  ",");
        j = string.concat(j, _jUint("pool_buck",  s_pool_buck), ",");
        j = string.concat(j, _jUint("pool_usdc",  s_pool_usdc), ",");
        j = string.concat(j, _jUint("jubilee",    s_jubilee),   ",");
        j = string.concat(j, _jUint("supply",     s_supply),    ",");
        j = string.concat(j, _jUint("credit_val", s_credit_val),",");
        j = string.concat(j, _jUint("bob_buck",   s_bob_buck),  ",");
        j = string.concat(j, _jUint("bob_usdc",   s_bob_usdc));
        j = string.concat(j, "}");
        vm.writeFile("test/vectors/lifecycle.json", j);
    }

    // ── allowance helper (bypasses CP approve, same as UniswapV2Integration) ──

    function _setBuckAllowance(address owner_, address spender, uint256 amount) internal {
        // _allowances lives at slot 2 in Buck's packed layout.
        bytes32 slot = keccak256(
            abi.encode(spender, keccak256(abi.encode(owner_, uint256(2))))
        );
        vm.store(address(buck), slot, bytes32(amount));
    }

    // ── identity JSON helpers (mirrors UniswapV2Integration.t.sol) ────────────

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
    function _regProof(string memory who) internal view
        returns (IdentityRegistry.RegistrationProof memory p)
    {
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
        ipk.X.X[0] = _u(".issuer.pk_X.x[0]"); ipk.X.X[1] = _u(".issuer.pk_X.x[1]");
        ipk.X.Y[0] = _u(".issuer.pk_X.y[0]"); ipk.X.Y[1] = _u(".issuer.pk_X.y[1]");
        ipk.Y.X[0] = _u(".issuer.pk_Y.x[0]"); ipk.Y.X[1] = _u(".issuer.pk_Y.x[1]");
        ipk.Y.Y[0] = _u(".issuer.pk_Y.y[0]"); ipk.Y.Y[1] = _u(".issuer.pk_Y.y[1]");
        vm.prank(GOV);
        reg.trustIssuer(address(0x1551E1), ipk);
    }
    function _registerAlice() internal {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        vm.prank(alice);
        reg.register(address(0x1551E1), pk, E, _ps("alice"), _regProof("alice"));
    }
    function _registerBob() internal {
        BN254.G1Point memory pk = _g1(".bob.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".bob.ciphertext");
        vm.prank(bob);
        reg.register(address(0x1551E1), pk, E, _ps("bob"), _regProof("bob"));
    }
}
