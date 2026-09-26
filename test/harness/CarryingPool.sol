// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Vm} from "forge-std/Vm.sol";

import {BN254} from "../../src/BN254.sol";
import {IdentityRegistry} from "../../src/IdentityRegistry.sol";

Vm constant CARRYING_POOL_VM = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

/// @notice Make `pool` what Buck's insurance pool is meant to be: a Carrying
///         contract account, bound public through the (harness) registry --
///         it holds premium deposits on its members' behalf, so the demurrage
///         they accrue travels with them rather than eroding the reserve.
///         Tests name the pool by a bare address; this plants code there
///         first, since only a contract can be bound.
function bindCarryingPool(IdentityRegistry reg, address pool) {
    if (pool.code.length == 0) CARRYING_POOL_VM.etch(pool, hex"60006000fd");
    reg.bindContract(pool, BN254.g1(),
        IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, true);
}
