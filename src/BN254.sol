// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title BN254 — typed wrappers around Ethereum's alt_bn128 precompiles.
/// @notice Mirrors the encoding used by alberta_buck.wallet so JSON test vectors
///         emitted by the Python reference round-trip cleanly into Solidity.
///
///         G1 points are (X, Y) over Fp; G2 points are (X, Y) over Fp2 with
///         X = X.c0 + X.c1 * i.  The pairing precompile (0x08) consumes G2
///         coordinates in *imaginary-first* order (X.c1, X.c0, Y.c1, Y.c0) —
///         pairing() handles the swap so callers stay in (c0, c1) form.
library BN254 {

    // ---- field constants ----------------------------------------------------

    /// @notice Base field prime P = 21888242871839275222246405745257275088696311157297823662689037894645226208583.
    uint256 internal constant P =
        0x30644e72e131a029b85045b68181585d97816a916871ca8d3c208c16d87cfd47;

    /// @notice Subgroup order R (= curve_order) = 21888242871839275222246405745257275088548364400416034343698204186575808495617.
    uint256 internal constant R =
        0x30644e72e131a029b85045b68181585d2833e84879b9709143e1f593f0000001;

    // ---- types --------------------------------------------------------------

    struct G1Point {
        uint256 X;
        uint256 Y;
    }

    /// @notice G2 in (c0, c1) order — same shape the wallet emits.
    struct G2Point {
        uint256[2] X;   // [c0, c1]
        uint256[2] Y;   // [c0, c1]
    }

    // ---- generators (cached so Solidity callers don't have to re-encode) ----

    function g1() internal pure returns (G1Point memory) {
        return G1Point(1, 2);
    }

    function g2() internal pure returns (G2Point memory P_) {
        // py_ecc / EIP-197 canonical generator, in (c0, c1) order.
        P_.X[0] = 0x1800deef121f1e76426a00665e5c4479674322d4f75edadd46debd5cd992f6ed;
        P_.X[1] = 0x198e9393920d483a7260bfb731fb5d25f1aa493335a9e71297e485b7aef312c2;
        P_.Y[0] = 0x12c85ea5db8c6deb4aab71808dcb408fe3d1e7690c43d37b4ce6cc0166fa7daa;
        P_.Y[1] = 0x090689d0585ff075ec9e99ad690c3395bc4b313370b38ef355acdadcd122975b;
    }

    function zeroG1() internal pure returns (G1Point memory) {
        return G1Point(0, 0);
    }

    function isInfinity(G1Point memory pt) internal pure returns (bool) {
        return pt.X == 0 && pt.Y == 0;
    }

    function eq(G1Point memory a, G1Point memory b) internal pure returns (bool) {
        return a.X == b.X && a.Y == b.Y;
    }

    // ---- primitives ---------------------------------------------------------

    /// @notice -P = (X, P - Y) for non-infinity P.
    function neg(G1Point memory pt) internal pure returns (G1Point memory) {
        if (isInfinity(pt)) return pt;
        return G1Point(pt.X, P - (pt.Y % P));
    }

    /// @notice ecAdd via 0x06.  Reverts if the precompile rejects either input.
    function add(G1Point memory a, G1Point memory b) internal view returns (G1Point memory r_) {
        uint256[4] memory input = [a.X, a.Y, b.X, b.Y];
        bool ok;
        assembly {
            ok := staticcall(gas(), 0x06, input, 0x80, r_, 0x40)
        }
        require(ok, "BN254: ecAdd failed");
    }

    /// @notice ecMul via 0x07.  Scalar is taken mod R upstream (the precompile
    ///         itself accepts any uint256 and reduces internally).
    function mul(G1Point memory pt, uint256 s) internal view returns (G1Point memory r_) {
        uint256[3] memory input = [pt.X, pt.Y, s];
        bool ok;
        assembly {
            ok := staticcall(gas(), 0x07, input, 0x60, r_, 0x40)
        }
        require(ok, "BN254: ecMul failed");
    }

    /// @notice ecPairing via 0x08.  Returns true iff prod_i e(a_i, b_i) == 1.
    /// @dev    Each G2 coordinate is fed imaginary-first, per EIP-197.
    function pairingCheck(G1Point[] memory a, G2Point[] memory b) internal view returns (bool) {
        require(a.length == b.length, "BN254: pairing length mismatch");
        uint256 n = a.length;
        uint256[] memory input = new uint256[](n * 6);
        for (uint256 i = 0; i < n; ++i) {
            uint256 j = i * 6;
            input[j + 0] = a[i].X;
            input[j + 1] = a[i].Y;
            input[j + 2] = b[i].X[1];   // imaginary first
            input[j + 3] = b[i].X[0];
            input[j + 4] = b[i].Y[1];
            input[j + 5] = b[i].Y[0];
        }
        uint256[1] memory out;
        bool ok;
        assembly {
            let len := mul(mload(input), 0x20)
            ok := staticcall(gas(), 0x08, add(input, 0x20), len, out, 0x20)
        }
        require(ok, "BN254: ecPairing failed");
        return out[0] == 1;
    }

    // ---- Fiat-Shamir transcripts -------------------------------------------
    //
    // Both transcripts must byte-for-byte match alberta_buck.wallet.transcript:
    // each input is encoded as a 32-byte big-endian word, concatenated, then
    // keccak256'd, then reduced modulo R.  abi.encodePacked(uint256, ...) does
    // exactly that.

    /// @notice Pack a sequence of G1 points followed by extra scalars and hash
    ///         keccak256-mod-R, identical to alberta_buck.wallet.transcript.keccak_scalar.
    /// @dev    Callers (registration / approve verifiers) pass each protocol
    ///         input in the documented order; this avoids stack-depth limits.
    function fsChallenge(G1Point[] memory points, uint256[] memory scalars)
        internal pure returns (uint256)
    {
        uint256 nPts = points.length;
        uint256 nScl = scalars.length;
        uint256[] memory buf = new uint256[](nPts * 2 + nScl);
        for (uint256 i = 0; i < nPts; ++i) {
            buf[i * 2]     = points[i].X;
            buf[i * 2 + 1] = points[i].Y;
        }
        for (uint256 j = 0; j < nScl; ++j) {
            buf[nPts * 2 + j] = scalars[j];
        }
        bytes32 h;
        assembly {
            h := keccak256(add(buf, 0x20), mul(mload(buf), 0x20))
        }
        return uint256(h) % R;
    }
}
