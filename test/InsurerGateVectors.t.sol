// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {BN254} from "../src/BN254.sol";
import {PoseidonT3Bytecode} from "../src/PoseidonT3Bytecode.sol";
import {PoseidonT4Bytecode} from "../src/PoseidonT4Bytecode.sol";

/// @notice The insurer gate against the Python reference
///         (scripts/gen_insurer_gate_vectors.py -> test/vectors/insurer_gate.json):
///         the regulator's subtrees, the tagged public leaf, the composed paths,
///         the identity-opening transcript, the band ladder and every refusal
///         reason, byte for byte.
contract InsurerGateVectorsTest is Test {
    address constant GOV    = address(0x6011);
    address constant CLIENT = address(0xC11E);

    string  vj;
    IdentityRegistryHarness reg;
    BuckCredit credit;
    address insurer;

    function _u(string memory k) internal view returns (uint256) { return vm.parseJsonUint(vj, k); }
    function _g1(string memory k) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point(_u(string.concat(k, ".x")), _u(string.concat(k, ".y")));
    }

    function setUp() public {
        vj = vm.readFile("test/vectors/insurer_gate.json");
        vm.chainId(_u(".chainid"));
        vm.warp(_u(".t0"));
        address regAddr = vm.parseJsonAddress(vj, ".registry");
        deployCodeTo("IdentityRegistryHarness.sol", abi.encode(GOV), regAddr);
        reg = IdentityRegistryHarness(regAddr);
        vm.startPrank(GOV);
        reg.setIdentityPoseidon(PoseidonT3Bytecode.deploy());
        reg.setIdentityPoseidonT4(PoseidonT4Bytecode.deploy());
        reg.setRootAuthority(GOV);
        reg.setAggregator(GOV);
        reg.enrollSubtree(bytes32(_u(".kyc.key")), uint32(_u(".kyc.slot")),
                          uint8(_u(".kyc.depth")), false, GOV, "");
        uint256 n = _count(".subtrees");
        for (uint256 i = 0; i < n; i++) {
            string memory k = string.concat(".subtrees[", vm.toString(i), "]");
            reg.enrollSubtree(bytes32(_u(string.concat(k, ".key"))),
                              uint32(_u(string.concat(k, ".slot"))),
                              uint8(_u(string.concat(k, ".depth"))), true, GOV, "");
        }
        reg.postIdentityRoot(_u(".root"), keccak256("the reference aggregator's leaf list"));
        vm.stopPrank();

        insurer = vm.parseJsonAddress(vj, ".insurer");
        vm.etch(insurer, hex"60006000fd");
        reg.bindContract(insurer, _g1(".pk"),
                         IdentityRegistry.ElGamalCT(_g1(".E.R"), _g1(".E.C")), true, false);

        credit = new BuckCredit();
        credit.configureInsurerGate(regAddr, vm.parseJsonString(vj, ".namespace"),
                                    uint32(_u(".period")));
        vm.prank(CLIENT);
        credit.setCreditIssuer(insurer, true);
    }

    function _count(string memory key) internal view returns (uint256 n) {
        while (vm.keyExistsJson(vj, string.concat(key, "[", vm.toString(n), "]"))) n++;
    }

    function _attest() internal {
        uint256 n = _count(".paths");
        BuckCredit.Path[] memory paths = new BuckCredit.Path[](n);
        for (uint256 i = 0; i < n; i++) {
            string memory k = string.concat(".paths[", vm.toString(i), "]");
            paths[i].sub = vm.parseJsonUintArray(vj, string.concat(k, ".sub"));
            paths[i].subIndex = _u(string.concat(k, ".subIndex"));
            paths[i].agg = vm.parseJsonUintArray(vj, string.concat(k, ".agg"));
        }
        BuckCredit.EnvelopeClaim memory c;
        c.faceBand = uint8(_u(".claim.faceBand"));
        c.depTypes = uint8(_u(".claim.depTypes"));
        c.maxDepRate = uint32(_u(".claim.maxDepRate"));
        c.maxPremiumRate = uint32(_u(".claim.maxPremiumRate"));
        c.general = vm.parseJsonBool(vj, ".claim.general");
        c.scopes = vm.parseJsonStringArray(vj, ".claim.scopes");
        BuckCredit.IdentityOpening memory op = BuckCredit.IdentityOpening(
            _u(".opening.e"), _u(".opening.s"), _g1(".opening.T1"), _g1(".opening.T2"));
        BN254.G1Point memory M = _g1(".M");
        uint256 root = _u(".root");
        vm.prank(insurer);
        credit.attestInsurer(M, op, c, root, paths);
    }

    function test_theReferenceLeafIsTheOnChainLeaf() public view {
        assertEq(reg.publicIdentityLeaf(_g1(".M")), _u(".leaf"));
    }

    function test_theReferenceEnvelopeAttests() public {
        _attest();
        (uint48 expiresAt, uint8 band, uint8 deps, , , uint32 epoch) = credit.envelopeOf(insurer);
        assertEq(expiresAt, _u(".t0") + _u(".period"));
        assertEq(band, 5);
        assertEq(deps, 3);
        assertEq(epoch, 1);
    }

    function test_everyIssuanceCaseDecidesAsTheReferenceDoes() public {
        _attest();
        uint256 n = _count(".cases");
        assertGt(n, 0);
        for (uint256 i = 0; i < n; i++) {
            string memory k = string.concat(".cases[", vm.toString(i), "]");
            vm.warp(_u(string.concat(k, ".at")));
            string memory expect = vm.parseJsonString(vj, string.concat(k, ".expect"));
            bytes32 scope = bytes32(_u(string.concat(k, ".scope")));
            uint256 face = _u(string.concat(k, ".face"));
            BuckCredit.DepreciationType dep = BuckCredit.DepreciationType(_u(string.concat(k, ".depType")));
            uint32 depRate = uint32(_u(string.concat(k, ".depRate")));
            uint32 prem = uint32(_u(string.concat(k, ".premium")));
            if (bytes(expect).length != 0) vm.expectRevert(bytes(expect));
            vm.prank(insurer);
            credit.createCredit(CLIENT, 0, face, 0, dep, depRate, uint48(block.timestamp), prem, scope);
        }
    }
}
