// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import { Test } from "forge-std/Test.sol";
import { BN254 } from "../src/BN254.sol";
import { IdentityRegistry } from "../src/IdentityRegistry.sol";
import { UniswapV2BindingAdapter } from "../src/adapters/UniswapV2BindingAdapter.sol";
import { UniswapV3BindingAdapter } from "../src/adapters/UniswapV3BindingAdapter.sol";

contract MockUniswapPool { }

interface ILiveUniswapV2Factory {
    function getPair(address tokenA, address tokenB) external view returns (address);
}

interface ILiveUniswapV3Factory {
    function setOwner(address next) external;
    function getPool(address tokenA, address tokenB, uint24 fee) external view returns (address);
}

contract MockUniswapV2Factory {
    address public feeToSetter;
    mapping(bytes32 => address) internal _pair;

    constructor(address feeToSetter_) {
        feeToSetter = feeToSetter_;
    }

    function setFeeToSetter(address next) external {
        feeToSetter = next;
    }

    function getPair(address tokenA, address tokenB) external view returns (address) {
        return _pair[_key(tokenA, tokenB)];
    }

    function createPair(address tokenA, address tokenB) external returns (address pair) {
        require(tokenA != tokenB && tokenA != address(0) && tokenB != address(0), "bad tokens");
        bytes32 key = _key(tokenA, tokenB);
        require(_pair[key] == address(0), "exists");
        pair = address(new MockUniswapPool());
        _pair[key] = pair;
    }

    function _key(address tokenA, address tokenB) internal pure returns (bytes32) {
        (address token0, address token1) = tokenA < tokenB ? (tokenA, tokenB) : (tokenB, tokenA);
        return keccak256(abi.encode(token0, token1));
    }
}

contract MockUniswapV3Factory {
    address public owner;
    mapping(bytes32 => address) internal _pool;

    constructor(address owner_) {
        owner = owner_;
    }

    function setOwner(address next) external {
        owner = next;
    }

    function getPool(address tokenA, address tokenB, uint24 fee) external view returns (address) {
        return _pool[_key(tokenA, tokenB, fee)];
    }

    function createPool(address tokenA, address tokenB, uint24 fee) external returns (address pool) {
        require(tokenA != tokenB && tokenA != address(0) && tokenB != address(0), "bad tokens");
        bytes32 key = _key(tokenA, tokenB, fee);
        require(_pool[key] == address(0), "exists");
        pool = address(new MockUniswapPool());
        _pool[key] = pool;
    }

    function _key(address tokenA, address tokenB, uint24 fee) internal pure returns (bytes32) {
        (address token0, address token1) = tokenA < tokenB ? (tokenA, tokenB) : (tokenB, tokenA);
        return keccak256(abi.encode(token0, token1, fee));
    }
}

contract UniswapBindingAdaptersTest is Test {
    IdentityRegistry internal reg;
    MockUniswapV2Factory internal v2Factory;
    MockUniswapV3Factory internal v3Factory;
    UniswapV2BindingAdapter internal v2Adapter;
    UniswapV3BindingAdapter internal v3Adapter;

    address internal constant GOV = address(0xA0);
    address internal constant ISSUER = address(0x1551E1);
    address internal constant REGISTRY_ADDR = 0x1D1D1D1d1d1D1D1d1d1D1D1d1d1D1d1d1d1d1D1D;
    address internal constant TOKEN_A = address(0xA11);
    address internal constant TOKEN_B = address(0xB22);
    address internal constant TOKEN_C = address(0xC33);

    address internal alice;
    address internal bob;
    string internal vectors;

    function setUp() public {
        vm.chainId(1);
        vectors = vm.readFile("test/vectors/identity.json");
        deployCodeTo("IdentityRegistry.sol:IdentityRegistry", abi.encode(GOV), REGISTRY_ADDR);
        reg = IdentityRegistry(REGISTRY_ADDR);
        alice = address(uint160(_u(".alice.registrant")));
        bob = address(uint160(_u(".bob.registrant")));

        IdentityRegistry.PSPubKey memory issuerPk;
        issuerPk.X.X[0] = _u(".issuer.pk_X.x[0]");
        issuerPk.X.X[1] = _u(".issuer.pk_X.x[1]");
        issuerPk.X.Y[0] = _u(".issuer.pk_X.y[0]");
        issuerPk.X.Y[1] = _u(".issuer.pk_X.y[1]");
        issuerPk.Y.X[0] = _u(".issuer.pk_Y.x[0]");
        issuerPk.Y.X[1] = _u(".issuer.pk_Y.x[1]");
        issuerPk.Y.Y[0] = _u(".issuer.pk_Y.y[0]");
        issuerPk.Y.Y[1] = _u(".issuer.pk_Y.y[1]");
        vm.prank(GOV);
        reg.trustIssuer(ISSUER, issuerPk);
        _register("alice", alice);

        v2Factory = new MockUniswapV2Factory(alice);
        v3Factory = new MockUniswapV3Factory(alice);
        v2Adapter = new UniswapV2BindingAdapter(address(reg), address(v2Factory));
        v3Adapter = new UniswapV3BindingAdapter(address(reg), address(v3Factory));
        vm.startPrank(GOV);
        reg.setBindingAdapter(address(v2Adapter), true);
        reg.setBindingAdapter(address(v3Adapter), true);
        vm.stopPrank();
    }

    function _u(string memory key) internal view returns (uint256) {
        return vm.parseJsonUint(vectors, key);
    }

    function _g1(string memory key) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point(_u(string.concat(key, ".x")), _u(string.concat(key, ".y")));
    }

    function _ct(string memory who) internal view returns (IdentityRegistry.ElGamalCT memory value) {
        value.R = _g1(string.concat(".", who, ".ciphertext.R"));
        value.C = _g1(string.concat(".", who, ".ciphertext.C"));
    }

    function _sig(string memory who) internal view returns (IdentityRegistry.PSSig memory value) {
        value.sigma_1 = _g1(string.concat(".", who, ".ps_sig_rerand.sigma_1"));
        value.sigma_2 = _g1(string.concat(".", who, ".ps_sig_rerand.sigma_2"));
    }

    function _proof(string memory who) internal view returns (IdentityRegistry.RegistrationProof memory value) {
        string memory base = string.concat(".", who, ".registration_proof");
        value.e = _u(string.concat(base, ".e"));
        value.s_m = _u(string.concat(base, ".s_m"));
        value.s_r = _u(string.concat(base, ".s_r"));
        value.s_sk = _u(string.concat(base, ".s_sk"));
        value.A_ps = _g1(string.concat(base, ".A_ps"));
        value.T_C = _g1(string.concat(base, ".T_C"));
        value.T_R = _g1(string.concat(base, ".T_R"));
        value.T_key = _g1(string.concat(base, ".T_key"));
    }

    function _register(string memory who, address account) internal {
        vm.prank(account);
        reg.register(ISSUER, _g1(string.concat(".", who, ".elgamal_kp.pk")), _ct(who), _sig(who), _proof(who));
    }

    function _assertCopiedIdentity(address target, address operator) internal view {
        assertTrue(BN254.eq(reg.pkOf(target), reg.pkOf(operator)), "pk not copied");
        IdentityRegistry.ElGamalCT memory actual = reg.ciphertextOf(target);
        IdentityRegistry.ElGamalCT memory expected = reg.ciphertextOf(operator);
        assertTrue(BN254.eq(actual.R, expected.R), "ciphertext R not copied");
        assertTrue(BN254.eq(actual.C, expected.C), "ciphertext C not copied");
        assertEq(reg.issuerOf(target), reg.issuerOf(operator), "issuer not copied");
        assertEq(reg.binderOf(target), operator, "operator not recorded");
        assertTrue(reg.isPublicIdentity(target), "pool must be public identity");
        assertTrue(reg.isCarrying(target), "pool must be carrying");
    }

    function test_metadataPinsRegistryAndProvenance() public view {
        assertEq(v2Adapter.registry(), address(reg));
        assertEq(v2Adapter.provenance(), address(v2Factory));
        assertEq(v2Adapter.bindingAuthority(), alice);
        assertEq(v3Adapter.registry(), address(reg));
        assertEq(v3Adapter.provenance(), address(v3Factory));
        assertEq(v3Adapter.bindingAuthority(), alice);
    }

    function test_adapterConstructorsRejectInvalidRegistryOrFactory() public {
        vm.expectRevert(bytes("registry=0"));
        new UniswapV2BindingAdapter(address(0), address(v2Factory));

        vm.expectRevert(bytes("registry not a contract"));
        new UniswapV2BindingAdapter(address(0xBAD), address(v2Factory));

        vm.expectRevert(bytes("factory=0"));
        new UniswapV2BindingAdapter(address(reg), address(0));

        vm.expectRevert(bytes("factory not a contract"));
        new UniswapV3BindingAdapter(address(reg), address(0xBAD));
    }

    function test_registryRejectsDirectAdapterEntry() public {
        address target = address(new MockUniswapPool());
        vm.prank(alice);
        vm.expectRevert(bytes("not binding adapter"));
        reg.bindContractFromAdapter(target, alice, true, true);
    }

    function test_v2CreatePairAndBindIsAtomicAndCopiesAuthorityIdentity() public {
        vm.prank(alice);
        address pair = v2Adapter.createPairAndBind(TOKEN_A, TOKEN_B);

        assertEq(v2Factory.getPair(TOKEN_A, TOKEN_B), pair);
        assertEq(v2Factory.getPair(TOKEN_B, TOKEN_A), pair);
        _assertCopiedIdentity(pair, alice);
    }

    function test_v2WorksWithPinnedOfficialFactoryArtifact() public {
        address liveFactory = deployCode("out/UniswapV2Factory.sol/UniswapV2Factory.json", abi.encode(alice));
        UniswapV2BindingAdapter liveAdapter = new UniswapV2BindingAdapter(address(reg), liveFactory);
        vm.prank(GOV);
        reg.setBindingAdapter(address(liveAdapter), true);

        vm.prank(alice);
        address pair = liveAdapter.createPairAndBind(TOKEN_A, TOKEN_B);
        assertEq(ILiveUniswapV2Factory(liveFactory).getPair(TOKEN_B, TOKEN_A), pair);
        _assertCopiedIdentity(pair, alice);
    }

    function test_v2BindsExistingCanonicalPair() public {
        address pair = v2Factory.createPair(TOKEN_A, TOKEN_B);
        vm.prank(alice);
        assertEq(v2Adapter.bindExistingPair(TOKEN_B, TOKEN_A), pair);
        _assertCopiedIdentity(pair, alice);
    }

    function test_v2RejectsCallerWhoIsNotCurrentFactoryAuthority() public {
        vm.prank(bob);
        vm.expectRevert(bytes("not binding authority"));
        v2Adapter.createPairAndBind(TOKEN_A, TOKEN_B);
        assertEq(v2Factory.getPair(TOKEN_A, TOKEN_B), address(0));
    }

    function test_v2RejectsUnregisteredFactoryAuthority() public {
        address stranger = address(0x515151);
        v2Factory.setFeeToSetter(stranger);
        vm.prank(stranger);
        vm.expectRevert(bytes("authority not registered"));
        v2Adapter.createPairAndBind(TOKEN_A, TOKEN_B);
    }

    function test_v2UsesCurrentAuthorityAfterGovernanceRotation() public {
        _register("bob", bob);
        v2Factory.setFeeToSetter(bob);
        vm.prank(bob);
        address pair = v2Adapter.createPairAndBind(TOKEN_A, TOKEN_C);
        _assertCopiedIdentity(pair, bob);
    }

    function test_v2RevocationRollsBackPoolCreation() public {
        vm.prank(GOV);
        reg.setBindingAdapter(address(v2Adapter), false);

        vm.prank(alice);
        vm.expectRevert(bytes("not binding adapter"));
        v2Adapter.createPairAndBind(TOKEN_A, TOKEN_B);
        assertEq(v2Factory.getPair(TOKEN_A, TOKEN_B), address(0));
    }

    function test_v2CannotCreateOverExistingPair() public {
        v2Factory.createPair(TOKEN_A, TOKEN_B);
        vm.prank(alice);
        vm.expectRevert(bytes("pair already exists"));
        v2Adapter.createPairAndBind(TOKEN_A, TOKEN_B);
    }

    function test_v3CreatePoolAndBindIsAtomicAndCopiesAuthorityIdentity() public {
        uint24 fee = 3_000;
        vm.prank(alice);
        address pool = v3Adapter.createPoolAndBind(TOKEN_A, TOKEN_B, fee);

        assertEq(v3Factory.getPool(TOKEN_A, TOKEN_B, fee), pool);
        assertEq(v3Factory.getPool(TOKEN_B, TOKEN_A, fee), pool);
        _assertCopiedIdentity(pool, alice);
    }

    function test_v3WorksWithPinnedOfficialFactoryArtifact() public {
        address liveFactory = deployCode("out/UniswapV3Factory.sol/UniswapV3Factory.json");
        ILiveUniswapV3Factory(liveFactory).setOwner(alice);
        UniswapV3BindingAdapter liveAdapter = new UniswapV3BindingAdapter(address(reg), liveFactory);
        vm.prank(GOV);
        reg.setBindingAdapter(address(liveAdapter), true);

        vm.prank(alice);
        address pool = liveAdapter.createPoolAndBind(TOKEN_A, TOKEN_B, 3_000);
        assertEq(ILiveUniswapV3Factory(liveFactory).getPool(TOKEN_B, TOKEN_A, 3_000), pool);
        _assertCopiedIdentity(pool, alice);
    }

    function test_v3BindsExistingCanonicalPool() public {
        uint24 fee = 500;
        address pool = v3Factory.createPool(TOKEN_A, TOKEN_B, fee);
        vm.prank(alice);
        assertEq(v3Adapter.bindExistingPool(TOKEN_B, TOKEN_A, fee), pool);
        _assertCopiedIdentity(pool, alice);
    }

    function test_v3RejectsCallerWhoIsNotCurrentFactoryAuthority() public {
        vm.prank(bob);
        vm.expectRevert(bytes("not binding authority"));
        v3Adapter.createPoolAndBind(TOKEN_A, TOKEN_B, 3_000);
        assertEq(v3Factory.getPool(TOKEN_A, TOKEN_B, 3_000), address(0));
    }

    function test_v3RejectsUnregisteredFactoryAuthority() public {
        address stranger = address(0x515151);
        v3Factory.setOwner(stranger);
        vm.prank(stranger);
        vm.expectRevert(bytes("authority not registered"));
        v3Adapter.createPoolAndBind(TOKEN_A, TOKEN_B, 3_000);
    }

    function test_v3UsesCurrentAuthorityAfterGovernanceRotation() public {
        _register("bob", bob);
        v3Factory.setOwner(bob);
        vm.prank(bob);
        address pool = v3Adapter.createPoolAndBind(TOKEN_A, TOKEN_C, 100);
        _assertCopiedIdentity(pool, bob);
    }

    function test_v3CannotBindPoolTwice() public {
        uint24 fee = 3_000;
        vm.prank(alice);
        v3Adapter.createPoolAndBind(TOKEN_A, TOKEN_B, fee);

        vm.prank(alice);
        vm.expectRevert(bytes("already bound"));
        v3Adapter.bindExistingPool(TOKEN_A, TOKEN_B, fee);
    }
}
