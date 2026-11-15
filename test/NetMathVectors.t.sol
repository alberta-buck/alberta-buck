// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {NetDemurrageMath} from "../src/lib/NetDemurrageMath.sol";
import {CarryRatio} from "../src/lib/CarryRatio.sol";
import {UniswapV3OracleLib} from "../src/lib/UniswapV3OracleLib.sol";

/// Python's arbitrary-precision oracle supplies expected values, including
/// full 512-bit products and quotient-overflow cases. This calls the actual
/// primitives used by BuckCash / BuckCredit, rather than a token substitute.
contract NetMathVectorsTest is Test {
    function evaluate(string calldata op, uint256[] calldata a, bool roundUp) external pure returns (uint256) {
        bytes32 kind = keccak256(bytes(op));
        if (kind == keccak256("decay")) return NetDemurrageMath.decay(a[0], a[1]);
        if (kind == keccak256("fundNetAfter")) return NetDemurrageMath.fundNetAfter(a[0], a[1], a[2]);
        if (kind == keccak256("targetRelief")) return NetDemurrageMath.targetRelief(a[0], a[1], a[2]);
        if (kind == keccak256("carryToNet")) return CarryRatio.toNet(a[0], a[1], a[2]);
        if (kind == keccak256("carryToNetScaled")) return CarryRatio.toNetScaled(a[0], a[1], a[2]);
        if (kind == keccak256("carryToNominal")) return CarryRatio.toNominal(a[0], a[1], a[2]);
        if (kind == keccak256("quoteAtTick")) {
            // A tick travels as tick + 2^23; the base token is token0 when its address is the lower.
            (address base, address quote) = a[2] == 1 ? (address(1), address(2)) : (address(2), address(1));
            return UniswapV3OracleLib.getQuoteAtTick(int24(int256(a[0]) - 2 ** 23), uint128(a[1]), base, quote);
        }
        require(kind == keccak256("mulDiv"), "unknown vector operation");
        return Math.mulDiv(a[0], a[1], a[2], roundUp ? Math.Rounding.Ceil : Math.Rounding.Floor);
    }

    function kind(string memory op) private pure returns (bytes32) { return keccak256(bytes(op)); }

    function test_sharedCanonicalIntegerVectors() public {
        string memory json = vm.readFile("test/vectors/net-math-vectors.json");
        assertEq(vm.parseJsonUint(json, ".format"), 1);
        assertEq(vm.parseJsonString(json, ".model"), "Buck/compounded-cash/per-facility-jubilee/v1");
        uint256 count = vm.parseJsonUint(json, ".count");
        assertGe(count, 300);
        for (uint256 i; i < count; ++i) {
            string memory path = string.concat(".cases[", vm.toString(i), "]");
            string memory op = vm.parseJsonString(json, string.concat(path, ".op"));
            string[] memory encoded = vm.parseJsonStringArray(json, string.concat(path, ".args"));
            uint256[] memory args = new uint256[](encoded.length);
            for (uint256 j; j < args.length; ++j) args[j] = vm.parseUint(encoded[j]);
            bool roundUp = vm.parseJsonBool(json, string.concat(path, ".roundUp"));
            bytes32 error = keccak256(bytes(vm.parseJsonString(json, string.concat(path, ".error"))));
            if (error != keccak256("")) {
                if (error == keccak256("fund exceeds issuance")) {
                    vm.expectRevert(NetDemurrageMath.FundExceedsIssuance.selector);
                } else if (error == keccak256("invalid principal")) {
                    vm.expectRevert(NetDemurrageMath.InvalidPrincipal.selector);
                } else if (error == keccak256("no net value")) {
                    vm.expectRevert(CarryRatio.NoNetValue.selector);
                } else if (error == keccak256("tick out of range")) {
                    vm.expectRevert(bytes("tick:OOB"));
                } else if (error == keccak256("uint256 overflow") && kind(op) == keccak256("quoteAtTick")) {
                    vm.expectRevert(bytes("muldiv:overflow"));
                } else if (error == keccak256("division by zero")) {
                    vm.expectRevert(abi.encodeWithSignature("Panic(uint256)", 0x12));
                } else {
                    assertEq(error, keccak256("uint256 overflow"));
                    vm.expectRevert(abi.encodeWithSignature("Panic(uint256)", 0x11));
                }
                this.evaluate(op, args, roundUp);
            } else {
                uint256 expected = vm.parseUint(vm.parseJsonString(json, string.concat(path, ".value")));
                assertEq(this.evaluate(op, args, roundUp), expected,
                         vm.parseJsonString(json, string.concat(path, ".name")));
            }
        }
    }
}
