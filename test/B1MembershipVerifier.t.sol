// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {IdentityMembershipB1Verifier} from "../src/IdentityMembershipB1Verifier.sol";

/// @notice On-chain verification of B1's repaired P-bound membership circuit
///         (circuits/identity_membership_b1.circom), the successor to the
///         G1-tie circuit.
///
///         What changed, and why it is not a cosmetic replacement.  The old
///         circuit took the blind's curve point T as a WITNESSED input and
///         never proved it was any multiple of H, so a prover could choose T
///         freely and satisfy P_dep = M + T for any M it liked.  Here T is
///         computed from the blind by ScalarMulHP, against a generator hashed
///         to the curve rather than multiplied out of G -- so there is no
///         known log to shift the blind along, which is the attack that let an
///         UNREGISTERED depositor borrow a registered Identity for the
///         membership half while its sigma spoke about its own.
///         See alberta_buck/wallet/nums.py and doc/review/notes-receiving-key.org.
contract IdentityMembershipB1VerifierTest is Test {
    IdentityMembershipB1Verifier internal verifier;
    string internal vj;

    function setUp() public {
        vj = vm.readFile("test/vectors/b1_membership/proof.json");
        verifier = new IdentityMembershipB1Verifier();
    }

    function _arr(string memory key) internal view returns (uint256[] memory) {
        return vm.parseJsonUintArray(vj, key);
    }

    function _proof()
        internal
        view
        returns (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[9] memory pub)
    {
        uint256[] memory av = _arr(".a");
        uint256[] memory bv = _arr(".b");
        uint256[] memory cv = _arr(".c");
        uint256[] memory pv = _arr(".pub");
        a = [av[0], av[1]];
        b = [[bv[0], bv[1]], [bv[2], bv[3]]];
        c = [cv[0], cv[1]];
        for (uint256 i = 0; i < 9; i++) {
            pub[i] = pv[i];
        }
    }

    function test_proof_verifies() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[9] memory pub) = _proof();
        assertTrue(verifier.verifyProof(a, b, c, pub),
                   "B1 membership Groth16 proof must verify on-chain");
    }

    function test_tampered_root_rejected() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[9] memory pub) = _proof();
        pub[0] = pub[0] ^ 1;
        assertFalse(verifier.verifyProof(a, b, c, pub));
    }

    /// @notice P_dep is the point this proof shares with the depositor-binding
    ///         sigma.  Moving it must break the proof, or the two halves could
    ///         speak about different commitments -- which is the whole reason
    ///         this circuit exists.
    function test_tampered_P_dep_rejected() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[9] memory pub) = _proof();
        pub[1] = pub[1] ^ 1;
        assertFalse(verifier.verifyProof(a, b, c, pub));
    }
}
