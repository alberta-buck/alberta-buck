// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IMintVerifierA2} from "./IMintVerifierA2.sol";

/// @title MintVerifierA2Adapter -- IMintVerifierA2 across the per-N
///         mint_batch_a2 Groth16 verifiers.
/// @notice Mirror of MintVerifierAdapter for the private-issuer (A2) mint
///         family.  Each pinned N has its own MintBatchA2N${N}Groth16Verifier
///         with a fixed-length pubSignals array of size 5N+4; this adapter
///         looks the verifier up by N == cms.length and dispatches the proof +
///         public inputs via a hand-crafted staticcall (snarkjs generates one
///         verifier per pubSignals arity).
///
/// @dev    Public-signal layout (per circuits/mint_batch_a2.circom): the A2
///         circuit's only OUTPUT is eIss[N][4], so it leads, followed by the
///         public inputs in declaration order:
///           pub[0..4N)       = eIss row-major (R.x, R.y, C.x, C.y per leaf)
///           pub[4N]          = oldRoot
///           pub[4N+1]        = newRoot
///           pub[4N+2]        = nextLeafIndex
///           pub[4N+3]        = totalFace
///           pub[4N+4..5N+4)  = cm[0..N)
contract MintVerifierA2Adapter is IMintVerifierA2 {

    address public governance;

    /// @notice Per-batch-size Groth16 verifier registry.  Keyed by N == cms.length.
    mapping(uint256 => address) public verifiers;

    event GovernanceTransferred(address indexed previous, address indexed next);
    event VerifierRegistered(uint256 indexed n, address indexed verifier);

    constructor(address _governance) {
        require(_governance != address(0), "governance=0");
        governance = _governance;
        emit GovernanceTransferred(address(0), _governance);
    }

    function transferGovernance(address next) external {
        require(msg.sender == governance, "not governance");
        require(next != address(0),       "governance=0");
        emit GovernanceTransferred(governance, next);
        governance = next;
    }

    /// @notice Register (or replace) the A2 Groth16 verifier for batch size `n`.
    function registerVerifier(uint256 n, address verifier) external {
        require(msg.sender == governance, "not governance");
        require(n > 0,                    "n=0");
        require(verifier != address(0),   "verifier=0");
        verifiers[n] = verifier;
        emit VerifierRegistered(n, verifier);
    }

    /// @inheritdoc IMintVerifierA2
    function verifyMint(
        bytes calldata proof,
        uint256[4][] calldata eIss,
        uint256 oldRoot,
        uint256 newRoot,
        uint256 nextLeafIndex,
        uint256 totalFace,
        uint256[] calldata commitments
    ) external view returns (bool) {
        uint256 N = commitments.length;
        if (eIss.length != N) return false;
        address v = verifiers[N];
        if (v == address(0)) return false;
        if (proof.length != 256) return false;  // 8 * 32 (pA, pB, pC)

        (uint256[2] memory pA, uint256[2][2] memory pB, uint256[2] memory pC) =
            abi.decode(proof, (uint256[2], uint256[2][2], uint256[2]));

        // Build the public-signal array of length 5N+4: eIss (outputs) lead
        // row-major, then [oldRoot, newRoot, nextLeafIndex, totalFace], then cm[].
        uint256[] memory pub = new uint256[](5 * N + 4);
        for (uint256 i = 0; i < N; i++) {
            pub[4 * i + 0] = eIss[i][0];
            pub[4 * i + 1] = eIss[i][1];
            pub[4 * i + 2] = eIss[i][2];
            pub[4 * i + 3] = eIss[i][3];
        }
        pub[4 * N]     = oldRoot;
        pub[4 * N + 1] = newRoot;
        pub[4 * N + 2] = nextLeafIndex;
        pub[4 * N + 3] = totalFace;
        for (uint256 i = 0; i < N; i++) {
            pub[4 * N + 4 + i] = commitments[i];
        }

        return _verifyN(v, pA, pB, pC, pub);
    }

    /// @dev Hand-rolled calldata for `verifyProof(uint256[2], uint256[2][2],
    ///      uint256[2], uint256[K])` where K = pub.length == 5N+4.  Identical
    ///      dispatch shape to MintVerifierAdapter (every argument is fixed-size,
    ///      so the static ABI encoding is just the fields packed contiguously).
    function _verifyN(
        address v,
        uint256[2] memory pA,
        uint256[2][2] memory pB,
        uint256[2] memory pC,
        uint256[] memory pub
    ) internal view returns (bool) {
        uint256 K = pub.length;
        bytes memory sig = abi.encodePacked(
            "verifyProof(uint256[2],uint256[2][2],uint256[2],uint256[",
            _itoa(K),
            "])"
        );
        bytes4 selector = bytes4(keccak256(sig));

        bytes memory body = new bytes(32 * (8 + K));
        assembly {
            let dst := add(body, 0x20)
            mstore(add(dst, 0x00), mload(add(pA, 0x00)))
            mstore(add(dst, 0x20), mload(add(pA, 0x20)))
            let pB0 := mload(add(pB, 0x00))
            let pB1 := mload(add(pB, 0x20))
            mstore(add(dst, 0x40), mload(add(pB0, 0x00)))
            mstore(add(dst, 0x60), mload(add(pB0, 0x20)))
            mstore(add(dst, 0x80), mload(add(pB1, 0x00)))
            mstore(add(dst, 0xa0), mload(add(pB1, 0x20)))
            mstore(add(dst, 0xc0), mload(add(pC, 0x00)))
            mstore(add(dst, 0xe0), mload(add(pC, 0x20)))
        }
        for (uint256 i = 0; i < K; i++) {
            uint256 word = pub[i];
            uint256 off  = 32 * (8 + i);
            assembly {
                mstore(add(add(body, 0x20), off), word)
            }
        }

        bytes memory data = abi.encodePacked(selector, body);
        (bool ok, bytes memory ret) = v.staticcall(data);
        if (!ok || ret.length < 32) return false;
        return abi.decode(ret, (bool));
    }

    /// @dev Decimal stringification for the selector signature builder.
    function _itoa(uint256 n) internal pure returns (bytes memory) {
        if (n == 0) return bytes("0");
        uint256 len; uint256 tmp = n;
        while (tmp > 0) { len++; tmp /= 10; }
        bytes memory s = new bytes(len);
        uint256 i = len;
        while (n > 0) {
            i--;
            s[i] = bytes1(uint8(48 + (n % 10)));
            n /= 10;
        }
        return s;
    }
}
