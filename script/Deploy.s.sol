// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import "../src/BN254.sol";
import "../src/BuckCredit.sol";
import "../src/Buck.sol";
import "../src/BuckKController.sol";
import "../src/IdentityRegistry.sol";
import "../src/Notes.sol";
import "../src/StubMintVerifier.sol";
import "../src/StubSpendVerifier.sol";

/// @notice Deploy all core contracts to a local or test network.
///
/// @dev Notes (Phase 7-bis) no longer needs the on-chain PoseidonT3
///      precompile -- per-leaf Merkle insertion happens in the mint SNARK.
///      A real deployment registers per-N MintBatchN${N}Groth16Verifier(s)
///      against the MintVerifierAdapter; this script wires the StubMintVerifier
///      so a fresh deployment is functional pending real circuit setup.
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

        // 5. Deploy Notes (Phase 7-bis: stub mint verifier; production will
        //    deploy MintVerifierAdapter + per-N MintBatchN${N}Groth16Verifier
        //    and call adapter.registerVerifier(N, addr) for each pinned N).
        StubMintVerifier  mintVerifier  = new StubMintVerifier(governance);
        console.log("StubMintVerifier deployed at:", address(mintVerifier));
        StubSpendVerifier spendVerifier = new StubSpendVerifier(governance);
        console.log("StubSpendVerifier deployed at:", address(spendVerifier));

        Notes notes = new Notes(
            address(buck),
            address(mintVerifier),
            address(spendVerifier),
            governance
        );
        console.log("Notes deployed at:", address(notes));

        // Bind a Public Identity to the Notes pool address.  The pool is a
        // BUCK-aware contract operated by governance; its plaintext identity m
        // is publicly disclosed off-chain (no cryptographic privacy of who
        // operates the pool), and approve receipts are decryptable by the
        // governance-held sk for subpoena response.
        //
        // TODO(production): 5-arg bindContract is the certified-operator
        // exception -- the broadcaster must already be registered and (pk, E)
        // must match that identity.  Prefer the credential overload with a
        // PS signature + NIZK Fiat-Shamir-bound to address(notes).  Placeholder
        // G1 values below will revert until that is wired.
        BN254.G1Point memory pk_notes_placeholder = BN254.g1();
        IdentityRegistry.ElGamalCT memory E_notes_placeholder =
            IdentityRegistry.ElGamalCT({ R: BN254.g1(), C: BN254.g1() });
        identity.bindContract(
            address(notes),
            pk_notes_placeholder,
            E_notes_placeholder,
            true, // isPublicIdentity
            true  // isCarrying -- Notes pool deploys carried-age BUCK to spenders
        );

        vm.stopBroadcast();
    }
}
