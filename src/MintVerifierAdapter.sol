// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IMintVerifier} from "./IMintVerifier.sol";

/// @title IMintBatchGroth16 -- Generic Groth16 verifier surface used by the
///        per-N adapter via low-level staticcall.  Each N pin gets its own
///        compiled verifier with a fixed-length pubSignals array of size 4+N.
interface IMintBatchGroth16 {
    // Marker only; we never call this through the typed interface because
    // pubSignals is variable-length per N.  See _verifyN() below for the
    // raw-call dispatch.
}

/// @title MintVerifierAdapter -- IMintVerifier across the per-N mint_batch
///         Groth16 verifiers.
/// @notice Notes invokes `verifyMint(proof, oldRoot, newRoot, nextLeafIndex,
///         totalFace, cms)` with arbitrary `cms.length` (the batch size N).
///         This adapter looks up the registered Groth16 verifier for that N
///         and dispatches the proof + public inputs via a hand-crafted
///         staticcall (since each N has a different pubSignals arity in the
///         snarkjs-generated Solidity verifier).
///
/// @dev    Public signals for the mint_batch circuit (per circuits/mint_batch.circom).
///         circom emits the main component's OUTPUTS first, then the public
///         inputs in declaration order, so the snarkjs verifier's pubSignals is:
///           pub[0..N)        = issuerMode[0..N)      (circuit outputs)
///           pub[N]           = oldRoot
///           pub[N+1]         = newRoot
///           pub[N+2]         = nextLeafIndex
///           pub[N+3]         = totalFace
///           pub[N+4..2N+4)   = cm[0..N)
///         The snarkjs-generated verifier expects calldata in the order
///           verifyProof(uint256[2] a, uint256[2][2] b, uint256[2] c,
///                       uint256[2N+4] pubSignals) -> bool
///         so we encode arguments as a flat ABI tuple by ABI-spec layout.
contract MintVerifierAdapter is IMintVerifier {

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

    /// @notice Register (or replace) the Groth16 verifier for batch size `n`.
    function registerVerifier(uint256 n, address verifier) external {
        require(msg.sender == governance, "not governance");
        require(n > 0,                    "n=0");
        require(verifier != address(0),   "verifier=0");
        verifiers[n] = verifier;
        emit VerifierRegistered(n, verifier);
    }

    /// @inheritdoc IMintVerifier
    function verifyMint(
        bytes calldata proof,
        uint256[] calldata issuerMode,
        uint256 oldRoot,
        uint256 newRoot,
        uint256 nextLeafIndex,
        uint256 totalFace,
        uint256[] calldata commitments
    ) external view returns (bool) {
        uint256 N = commitments.length;
        if (issuerMode.length != N) return false;
        address v = verifiers[N];
        if (v == address(0)) return false;
        if (proof.length != 256) return false;  // 8 * 32 (pA, pB, pC)

        // Decode (pA, pB, pC) from `proof`.
        (uint256[2] memory pA, uint256[2][2] memory pB, uint256[2] memory pC) =
            abi.decode(proof, (uint256[2], uint256[2][2], uint256[2]));

        // Build the public-signal array of length 2N+4: issuerMode (outputs)
        // lead, then [oldRoot, newRoot, nextLeafIndex, totalFace], then cm[].
        uint256[] memory pub = new uint256[](2 * N + 4);
        for (uint256 i = 0; i < N; i++) {
            pub[i] = issuerMode[i];
        }
        pub[N]     = oldRoot;
        pub[N + 1] = newRoot;
        pub[N + 2] = nextLeafIndex;
        pub[N + 3] = totalFace;
        for (uint256 i = 0; i < N; i++) {
            pub[N + 4 + i] = commitments[i];
        }

        return _verifyN(v, pA, pB, pC, pub);
    }

    /// @dev Hand-rolled calldata for `verifyProof(uint256[2], uint256[2][2],
    ///      uint256[2], uint256[K])` where K = pub.length.  snarkjs generates
    ///      one verifier per K with a fixed-length pubSignals; the static
    ///      ABI encoding is the same as concatenating the tuple fields, so we
    ///      pack the full calldata manually with the verifier's selector
    ///      `bytes4(keccak256("verifyProof(uint256[2],uint256[2][2],uint256[2],uint256[K])"))`.
    function _verifyN(
        address v,
        uint256[2] memory pA,
        uint256[2][2] memory pB,
        uint256[2] memory pC,
        uint256[] memory pub
    ) internal view returns (bool) {
        uint256 K = pub.length;
        // Assemble the function selector for this K.
        // signature: "verifyProof(uint256[2],uint256[2][2],uint256[2],uint256[K])"
        bytes memory sig = abi.encodePacked(
            "verifyProof(uint256[2],uint256[2][2],uint256[2],uint256[",
            _itoa(K),
            "])"
        );
        bytes4 selector = bytes4(keccak256(sig));

        // The flat ABI body is just (pA, pB, pC, pub) packed contiguously --
        // every type here is fixed-size (no offsets/length prefixes).  Total
        // word count = 2 + 4 + 2 + K = 8 + K.
        bytes memory body = new bytes(32 * (8 + K));
        assembly {
            let dst := add(body, 0x20)
            // pA[0], pA[1]
            mstore(add(dst, 0x00), mload(add(pA, 0x00)))
            mstore(add(dst, 0x20), mload(add(pA, 0x20)))
            // pB[0][0], pB[0][1], pB[1][0], pB[1][1]
            // pB is uint256[2][2] in memory: pB -> [ptr0, ptr1] each pointing
            // to a uint256[2].
            let pB0 := mload(add(pB, 0x00))
            let pB1 := mload(add(pB, 0x20))
            mstore(add(dst, 0x40), mload(add(pB0, 0x00)))
            mstore(add(dst, 0x60), mload(add(pB0, 0x20)))
            mstore(add(dst, 0x80), mload(add(pB1, 0x00)))
            mstore(add(dst, 0xa0), mload(add(pB1, 0x20)))
            // pC[0], pC[1]
            mstore(add(dst, 0xc0), mload(add(pC, 0x00)))
            mstore(add(dst, 0xe0), mload(add(pC, 0x20)))
        }
        // Append pub[i] words.
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
