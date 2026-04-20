// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import "../src/BuckCredit.sol";
import "../src/Buck.sol";
import "../src/BuckKController.sol";
import "../src/IdentityRegistry.sol";
import "../src/Notes.sol";
import "../src/StubMintVerifier.sol";

/// @notice Deploy all three core contracts to a local or test network.
contract Deploy is Script {
    function run() external {
        uint256 deployerPrivateKey = vm.envOr("PRIVATE_KEY", uint256(0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80)); // Anvil default key #0
        vm.startBroadcast(deployerPrivateKey);

        // 1. Deploy BuckCredit (ERC-721)
        BuckCredit buckCredit = new BuckCredit();
        console.log("BuckCredit deployed at:", address(buckCredit));

        // 2. Deploy BuckKController (PID)
        //    Conservative initial gains; governance = deployer for now
        address governance = vm.addr(deployerPrivateKey);
        BuckKController buckK = new BuckKController(
            0.1e18,     // Kp: gentle proportional
            0.01e18,    // Ki: very conservative integral
            0.05e18,    // Kd: moderate damping
            3600,       // dT: 1 hour minimum between PID updates
            0.50e18,    // buckKMin: 50% floor
            0.95e18,    // buckKMax: 95% ceiling
            1.0e18,     // buckK: initial value (neutral)
            address(0), // buckUsdcPool: not yet deployed
            1800,       // twapInterval: 30 minutes
            governance
        );
        console.log("BuckKController deployed at:", address(buckK));

        // 3. Deploy IdentityRegistry (Phase-2 identity layer)
        IdentityRegistry identity = new IdentityRegistry(governance);
        console.log("IdentityRegistry deployed at:", address(identity));

        // 4. Deploy Buck (ERC-20)
        //    Insurance pool = deployer address for now (replace with InsurancePool contract)
        address insurancePool = governance;
        Buck buck = new Buck(
            address(buckCredit), address(buckK), address(identity), insurancePool
        );
        console.log("Buck deployed at:", address(buck));

        // 5. Deploy Notes (Phase 1: stub mint verifier)
        StubMintVerifier mintVerifier = new StubMintVerifier(governance);
        console.log("StubMintVerifier deployed at:", address(mintVerifier));

        Notes notes = new Notes(address(buck), address(mintVerifier), governance);
        console.log("Notes deployed at:", address(notes));

        // The Notes pool is a system account: flag it public so identity-bound
        // BUCK transfers from issuers can land at the pool address.
        identity.setSystemPublic(address(notes), true);

        vm.stopBroadcast();
    }
}
