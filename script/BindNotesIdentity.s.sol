// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import "../src/BN254.sol";
import "../src/IdentityRegistry.sol";
import "../src/Notes.sol";

/// @notice Complete the post-deployment Notes identity ceremony.
/// @dev BINDING_JSON contains only the public target-bound credential. The
///      holder secret and fresh binding nonce are supplied separately through
///      IDENTITY_SECRET_KEY and BINDING_NONCE and are never written on-chain.
contract BindNotesIdentity is Script {
    uint256 internal constant CURVE_ORDER =
        21888242871839275222246405745257275088548364400416034343698204186575808495617;

    function run() external {
        uint256 broadcasterKey = vm.envUint("PRIVATE_KEY");
        uint256 identitySecret = vm.envUint("IDENTITY_SECRET_KEY");
        uint256 bindingNonce = vm.envUint("BINDING_NONCE");
        require(bindingNonce > 0 && bindingNonce < CURVE_ORDER, "bad binding nonce");

        IdentityRegistry registry = IdentityRegistry(vm.envAddress("IDENTITY_REGISTRY"));
        Notes notes = Notes(vm.envAddress("NOTES"));
        address issuer = vm.envAddress("IDENTITY_ISSUER");
        address binder = vm.addr(broadcasterKey);
        string memory data = vm.readFile(vm.envString("BINDING_JSON"));

        BN254.G1Point memory pk = _point(data, ".pk");
        require(BN254.eq(pk, BN254.mul(BN254.g1(), identitySecret)), "identity sk mismatch");
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT({
            R: _point(data, ".ciphertext.R"),
            C: _point(data, ".ciphertext.C")
        });
        IdentityRegistry.PSSig memory sigma = IdentityRegistry.PSSig({
            sigma_1: _point(data, ".ps_sig_rerand.sigma_1"),
            sigma_2: _point(data, ".ps_sig_rerand.sigma_2")
        });
        IdentityRegistry.RegistrationProof memory registration = _registration(data);

        IdentityRegistry.ContractBindingProof memory authorization;
        authorization.T = BN254.mul(BN254.g1(), bindingNonce);
        authorization.e = registry.contractBindingChallenge(
            address(notes), binder, pk, authorization.T, true, true
        );
        authorization.s = addmod(
            bindingNonce,
            mulmod(authorization.e, identitySecret, CURVE_ORDER),
            CURVE_ORDER
        );

        vm.startBroadcast(broadcasterKey);
        notes.authorizeIdentityBinding(
            address(registry), binder, pk, E, true, true
        );
        registry.bindContract(
            address(notes), issuer, pk, E, sigma, registration,
            authorization, true, true
        );
        vm.stopBroadcast();
    }

    function _point(string memory data, string memory path)
        internal view returns (BN254.G1Point memory)
    {
        return BN254.G1Point({
            X: vm.parseJsonUint(data, string.concat(path, ".x")),
            Y: vm.parseJsonUint(data, string.concat(path, ".y"))
        });
    }

    function _registration(string memory data)
        internal view returns (IdentityRegistry.RegistrationProof memory proof)
    {
        string memory path = ".registration_proof";
        proof.e = vm.parseJsonUint(data, string.concat(path, ".e"));
        proof.s_m = vm.parseJsonUint(data, string.concat(path, ".s_m"));
        proof.s_r = vm.parseJsonUint(data, string.concat(path, ".s_r"));
        proof.s_sk = vm.parseJsonUint(data, string.concat(path, ".s_sk"));
        proof.A_ps = _point(data, string.concat(path, ".A_ps"));
        proof.T_C = _point(data, string.concat(path, ".T_C"));
        proof.T_R = _point(data, string.concat(path, ".T_R"));
        proof.T_key = _point(data, string.concat(path, ".T_key"));
    }
}
