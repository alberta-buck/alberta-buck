// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}                  from "forge-std/Test.sol";
import {ERC20}                 from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20}                from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {BN254}                 from "../src/BN254.sol";
import {IdentityRegistry}      from "../src/IdentityRegistry.sol";
import {Buck}                  from "../src/Buck.sol";
import {BuckCredit}            from "../src/BuckCredit.sol";
import {BuckKControllerDirect} from "../src/BuckKControllerDirect.sol";
import {BuckBasket}            from "../src/BuckBasket.sol";
import {BuckBasketReceipt}     from "../src/BuckBasketReceipt.sol";

contract BBToken is ERC20 {
    uint8 immutable _dec;
    constructor(string memory n, string memory s, uint8 d) ERC20(n, s) { _dec = d; }
    function decimals() public view override returns (uint8) { return _dec; }
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

interface IV3Factory {
    function createPool(address, address, uint24) external returns (address);
    function getPool(address, address, uint24) external view returns (address);
    function feeAmountTickSpacing(uint24 fee) external view returns (int24);
}

/// @title BuckBasketTest -- Layers 4 + 5 of the direct-embodiment test plan.
///        Real Uniswap V3 pools, real Buck + identity stack.
contract BuckBasketTest is Test {

    address constant GOV    = address(0xA0);
    address constant POOL   = address(0xBA51C);
    address constant ISSUER = address(0x1551E1);

    Buck                   internal buck;
    BuckCredit             internal credit;
    BuckKControllerDirect  internal kCtrl;
    BuckBasket             internal basketC;
    BuckBasketReceipt      internal receipt;
    IdentityRegistry       internal reg;
    address                internal v3Factory;

    BBToken internal paxg;   // 18-dec mock RWA
    BBToken internal cbbtc;  // 8-dec mock RWA

    address internal alice;
    string  internal vj;

    uint256 constant PAXG_INITIAL_PRICE_BUCK  = 4000e18;   // 1 PAXG = 4000 BUCK
    uint256 constant CBBTC_INITIAL_PRICE_BUCK = 100000e18; // 1 cbBTC = 100K BUCK

    function setUp() public {
        vm.chainId(1);
        vj = vm.readFile("test/vectors/identity.json");

        reg = new IdentityRegistry(GOV);
        _trustIssuer();
        alice = address(uint160(_u(".alice.registrant")));
        _registerAlice();

        credit = new BuckCredit();

        kCtrl = new BuckKControllerDirect(
            0.1e18, 0.01e18, 0,
            60,                  // dT 60s
            0.50e18, 1.50e18,
            1.0e18,
            GOV
        );

        buck = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));

        // V3 factory (deployed from compiled artifact, same path as other tests).
        v3Factory = deployCode("out/UniswapV3Factory.sol/UniswapV3Factory.json");

        // Deploy basket and wire the system together.
        basketC = new BuckBasket(
            address(buck),
            address(kCtrl),
            v3Factory,
            GOV,
            500,                // 0.05% fee tier
            600,                // 10-min TWAP
            64,                 // observation cardinality
            50,                 // 0.5% slippage default
            1e3                 // minSeedLiquidity floor
        );
        receipt = basketC.receipt();

        vm.prank(POOL);
        buck.setBasket(address(basketC));

        vm.prank(GOV);
        kCtrl.setBasket(address(basketC));

        // Bind the basket so Buck.transfer can flow through it (Public + Carrying).
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT({
            R: BN254.g1(), C: BN254.g1()
        });
        vm.prank(address(this));
        reg.bindContract(address(basketC), BN254.g1(), E, true, true);

        // Mock RWA tokens.
        paxg  = new BBToken("Tether Gold (mock)",      "PAXG",  18);
        cbbtc = new BBToken("Coinbase Wrapped BTC",    "cbBTC",  8);

        // Mint balances to Alice.
        paxg .mint(alice, 1_000e18);
        cbbtc.mint(alice, 1_000e8);
    }

    // -------------------------------------------------------------------- //
    //  Configuration                                                         //
    // -------------------------------------------------------------------- //

    function test_addBasketToken_sets_constituent_and_creates_pool() public {
        vm.prank(GOV);
        address pool = basketC.addBasketToken(
            address(paxg), 18, PAXG_INITIAL_PRICE_BUCK, 10000, 500
        );
        assertTrue(pool != address(0));
        assertEq(basketC.constituentsLength(), 1);

        // V3 factory has the pool registered for (BUCK, PAXG, 500).
        address fromFactory = IV3Factory(v3Factory).getPool(
            address(buck), address(paxg), 500
        );
        assertEq(pool, fromFactory);

        // basketValueInBuck at init prices should equal 1e18 (within tick
        // rounding from the V3 spacing of 10 for the 0.05% fee tier).
        assertApproxEqRel(basketC.basketValueInBuck(), int256(1e18), 0.001e18);
    }

    function test_addBasketToken_only_governance() public {
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("Not governance");
        basketC.addBasketToken(
            address(paxg), 18, PAXG_INITIAL_PRICE_BUCK, 10000, 500
        );
    }

    function test_two_constituent_basket_value_at_init_is_unit() public {
        vm.startPrank(GOV);
        basketC.addBasketToken(address(paxg), 18,  PAXG_INITIAL_PRICE_BUCK,  5000, 500);
        basketC.addBasketToken(address(cbbtc), 8,  CBBTC_INITIAL_PRICE_BUCK, 5000, 500);
        vm.stopPrank();

        // Each pool reports its init price -> basket value = 0.5 + 0.5 = 1.0.
        assertApproxEqAbs(basketC.basketValueInBuck(), int256(1e18), 1e15);
    }

    // -------------------------------------------------------------------- //
    //  Direct mint -- single deposit lifecycle                              //
    // -------------------------------------------------------------------- //

    /// @dev Bind a V3 pool as PublicIdentity + Carrying so BUCK can transit
    ///      through it.  v1 BuckBasket doesn't do this automatically;
    ///      governance must bind every pool spawned by addBasketToken.
    function _bindPool(address pool) internal {
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT({
            R: BN254.g1(), C: BN254.g1()
        });
        reg.bindContract(pool, BN254.g1(), E, true, true);
    }

    function test_deposit_mints_BUCK_and_issues_receipt() public {
        vm.prank(GOV);
        address pool = basketC.addBasketToken(
            address(paxg), 18, PAXG_INITIAL_PRICE_BUCK, 10000, 500
        );
        _bindPool(pool);

        uint256 depositAmt = 1e18;          // 1 PAXG
        uint256 expectedBuck = 4000e18;     // 1 * 4000 BUCK at init price

        vm.prank(alice);
        paxg.approve(address(basketC), depositAmt);

        // Cold-pool deposit (no TWAP yet) -> pass maxDeviationBp=0 to skip guard.
        vm.prank(alice);
        uint256 receiptId = basketC.depositToken(address(paxg), depositAmt, 0);

        assertEq(receipt.ownerOf(receiptId), alice);

        (
            address token,
            uint256 principalT,
            uint256 principalB,
            uint128 L,
            /* uint64 */
        ) = basketC.deposits(receiptId);
        assertEq(token, address(paxg));
        assertEq(principalT, depositAmt);
        assertApproxEqRel(principalB, expectedBuck, 0.001e18);
        assertGt(L, 0);

        // totalSupply grew by the minted BUCK.
        assertEq(buck.totalSupply(), principalB);
    }

    function test_redeem_burns_principal_and_returns_token() public {
        vm.prank(GOV);
        address pool = basketC.addBasketToken(
            address(paxg), 18, PAXG_INITIAL_PRICE_BUCK, 10000, 500
        );
        _bindPool(pool);

        vm.prank(alice);
        paxg.approve(address(basketC), 1e18);
        vm.prank(alice);
        uint256 rid = basketC.depositToken(address(paxg), 1e18, 0);

        uint256 supplyBefore = buck.totalSupply();
        uint256 aliceTBefore = paxg.balanceOf(alice);

        // Redeem immediately.  No external swaps -> profit ~= 0.
        vm.prank(alice);
        basketC.redeem(rid, 0);

        vm.expectRevert();
        receipt.ownerOf(rid);

        // Principal BUCK burned (modulo tiny V3 rounding).
        assertLt(buck.totalSupply(), supplyBefore);
        assertLt(buck.totalSupply(), 1e15);   // ~all of the principalB burned

        // Alice got her PAXG back.
        assertApproxEqRel(paxg.balanceOf(alice), aliceTBefore + 1e18, 0.001e18);
    }

    // -------------------------------------------------------------------- //
    //  Identity helpers (mirrors BuckLifecycle.t.sol)                       //
    // -------------------------------------------------------------------- //

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
        ipk.X.X[0] = _u(".issuer.pk_X.x[0]"); ipk.X.X[1] = _u(".issuer.pk_X.x[1]");
        ipk.X.Y[0] = _u(".issuer.pk_X.y[0]"); ipk.X.Y[1] = _u(".issuer.pk_X.y[1]");
        ipk.Y.X[0] = _u(".issuer.pk_Y.x[0]"); ipk.Y.X[1] = _u(".issuer.pk_Y.x[1]");
        ipk.Y.Y[0] = _u(".issuer.pk_Y.y[0]"); ipk.Y.Y[1] = _u(".issuer.pk_Y.y[1]");
        vm.prank(GOV);
        reg.trustIssuer(ISSUER, ipk);
    }
    function _registerAlice() internal {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        vm.prank(alice);
        reg.register(ISSUER, pk, E, _ps("alice"), _regProof("alice"));
    }
}
