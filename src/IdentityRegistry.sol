// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "./BN254.sol";

/// @title IdentityRegistry — on-chain registry of identity-bound public keys.
/// @notice Each Ethereum address binds to (pk, E_addr) where:
///         * pk     = ElGamal recipient public key (G1)
///         * E_addr = (R, C) = ElGamal ciphertext of the identity point M = m*G,
///                    encrypted under pk and witnessed by an issuer-signed
///                    Pointcheval-Sanders credential.
///         A registration NIZK proves -- without revealing m or r -- that the
///         credential is valid and that E_addr really encrypts m.
///         A Chaum-Pedersen NIZK proves a re-encryption sends the same M to a
///         second registered recipient (used by Buck.approve()).
contract IdentityRegistry {

    // ---- types --------------------------------------------------------------

    struct PSPubKey {
        BN254.G2Point X;
        BN254.G2Point Y;
    }

    struct ElGamalCT {
        BN254.G1Point R;
        BN254.G1Point C;
    }

    struct PSSig {
        BN254.G1Point sigma_1;
        BN254.G1Point sigma_2;
    }

    /// @notice 6-element registration NIZK proof (matches alberta_buck.wallet.nizk).
    struct RegistrationProof {
        uint256 e;
        uint256 s_m;
        uint256 s_r;
        BN254.G1Point A_ps;   // PS-side commitment: m_tilde * sigma'_1
        BN254.G1Point T_C;    // ElGamal C commitment: m_tilde*G + r_tilde*pk
        BN254.G1Point T_R;    // ElGamal R commitment: r_tilde * G
    }

    /// @notice 6-element Chaum-Pedersen proof (matches alberta_buck.wallet.chaum_pedersen).
    struct CPProof {
        uint256 e;
        uint256 s1;
        uint256 s2;
        BN254.G1Point T1;
        BN254.G1Point T2;
        BN254.G1Point T3;
    }

    // ---- storage ------------------------------------------------------------

    address public governance;

    // Trusted issuer registry: keyed by issuer's Ethereum address.
    mapping(address => PSPubKey)  internal _trustedIssuers;
    mapping(address => bool)      public  isTrustedIssuer;

    // Per-account identity record.
    mapping(address => BN254.G1Point) internal _pk;
    mapping(address => ElGamalCT)     internal _E_addr;
    mapping(address => bool)          public  isVerified;
    mapping(address => bool)          public  isPublic;
    mapping(address => address)       public  issuerOf;

    // ---- events -------------------------------------------------------------

    event GovernanceTransferred(address indexed previous, address indexed next);
    event IssuerTrusted(address indexed issuer);
    event IssuerRevoked(address indexed issuer);
    event Registered(address indexed account, address indexed issuer);
    event PublicSet(address indexed account, bool isPublic);

    // ---- constructor / governance ------------------------------------------

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

    function trustIssuer(address issuer, PSPubKey calldata pk) external {
        require(msg.sender == governance, "not governance");
        require(issuer != address(0),     "issuer=0");
        _trustedIssuers[issuer] = pk;
        isTrustedIssuer[issuer] = true;
        emit IssuerTrusted(issuer);
    }

    function revokeIssuer(address issuer) external {
        require(msg.sender == governance, "not governance");
        require(isTrustedIssuer[issuer],  "not trusted");
        isTrustedIssuer[issuer] = false;
        delete _trustedIssuers[issuer];
        emit IssuerRevoked(issuer);
    }

    // ---- views --------------------------------------------------------------

    function pkOf(address account) external view returns (BN254.G1Point memory) {
        return _pk[account];
    }

    function ciphertextOf(address account) external view returns (ElGamalCT memory) {
        return _E_addr[account];
    }

    function trustedIssuerKey(address issuer) external view returns (PSPubKey memory) {
        return _trustedIssuers[issuer];
    }

    // ---- registration ------------------------------------------------------

    /// @notice Register caller's identity binding under issuer-signed credential.
    ///         msg.sender is the registrant -- bound into the Fiat-Shamir
    ///         transcript so a proof valid for one address cannot be replayed
    ///         under another.
    function register(
        address issuer,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        PSSig calldata sigma,
        RegistrationProof calldata proof
    ) external {
        require(!isVerified[msg.sender],  "already registered");
        require(isTrustedIssuer[issuer],  "untrusted issuer");
        require(!BN254.isInfinity(sigma.sigma_1), "sigma_1=O");

        // (d) Fiat-Shamir
        require(proof.e == _fsRegister(sigma, E, pk, proof, msg.sender), "bad FS challenge");

        // (b) ElGamal C consistency: s_m*G + s_r*pk == e*C + T_C
        require(_checkElGamalC(proof.s_m, proof.s_r, pk, E.C, proof.T_C, proof.e), "bad NIZK C");

        // (c) ElGamal R consistency: s_r*G == e*R + T_R
        require(_checkElGamalR(proof.s_r, E.R, proof.T_R, proof.e), "bad NIZK R");

        // (a) PS pairing product
        require(_checkPSPairing(sigma, proof, _trustedIssuers[issuer]), "bad PS sig");

        _pk[msg.sender]      = pk;
        _E_addr[msg.sender]  = E;
        isVerified[msg.sender] = true;
        issuerOf[msg.sender]   = issuer;
        emit Registered(msg.sender, issuer);
    }

    /// @notice Toggle account's public-identity flag (skips approve-time CP for receives).
    function setPublic(bool _isPublic) external {
        require(isVerified[msg.sender], "not registered");
        isPublic[msg.sender] = _isPublic;
        emit PublicSet(msg.sender, _isPublic);
    }

    // ---- approve verification ----------------------------------------------

    /// @notice Verify Alice's Chaum-Pedersen proof of equal-plaintext re-encryption
    ///         that ``E_bob`` encrypts the same M as ``E_addr[sender]``.
    /// @dev    Reads E_alice from storage (caller cannot substitute), and binds
    ///         (sender, spender, chainid) into the transcript.
    function verifyApprove(
        address sender,
        address spender,
        ElGamalCT calldata E_bob,
        CPProof calldata pi
    ) external view returns (bool) {
        if (!isVerified[sender] || !isVerified[spender]) return false;

        ElGamalCT memory E_a = _E_addr[sender];
        BN254.G1Point memory pkA = _pk[sender];
        BN254.G1Point memory pkB = _pk[spender];

        // Check 1: s2*G == T3 + e*R_b
        if (!BN254.eq(
            BN254.mul(BN254.g1(), pi.s2),
            BN254.add(pi.T3, BN254.mul(E_bob.R, pi.e))
        )) return false;

        // Check 2: s1*R_a - s2*pk_b == (T1 - T2) + e*(C_a - C_b)
        BN254.G1Point memory lhs2 = BN254.add(
            BN254.mul(E_a.R, pi.s1),
            BN254.neg(BN254.mul(pkB, pi.s2))
        );
        BN254.G1Point memory rhs2 = BN254.add(
            BN254.add(pi.T1, BN254.neg(pi.T2)),
            BN254.mul(BN254.add(E_a.C, BN254.neg(E_bob.C)), pi.e)
        );
        if (!BN254.eq(lhs2, rhs2)) return false;

        // Check 3: Fiat-Shamir
        return pi.e == _fsApprove(E_a, E_bob, pkA, pkB, pi, sender, spender, block.chainid);
    }

    // ---- internal verifier helpers (factored to manage stack depth) ---------

    function _fsRegister(
        PSSig calldata sigma,
        ElGamalCT calldata E,
        BN254.G1Point calldata pk,
        RegistrationProof calldata proof,
        address registrant
    ) internal pure returns (uint256) {
        BN254.G1Point[] memory pts = new BN254.G1Point[](8);
        pts[0] = sigma.sigma_1;
        pts[1] = sigma.sigma_2;
        pts[2] = E.R;
        pts[3] = E.C;
        pts[4] = pk;
        pts[5] = proof.A_ps;
        pts[6] = proof.T_C;
        pts[7] = proof.T_R;
        uint256[] memory scl = new uint256[](1);
        scl[0] = uint256(uint160(registrant));
        return BN254.fsChallenge(pts, scl);
    }

    function _fsApprove(
        ElGamalCT memory E_a,
        ElGamalCT calldata E_b,
        BN254.G1Point memory pkA,
        BN254.G1Point memory pkB,
        CPProof calldata pi,
        address sender,
        address spender,
        uint256 chainid
    ) internal pure returns (uint256) {
        BN254.G1Point[] memory pts = new BN254.G1Point[](9);
        pts[0] = E_a.R;
        pts[1] = E_a.C;
        pts[2] = E_b.R;
        pts[3] = E_b.C;
        pts[4] = pkA;
        pts[5] = pkB;
        pts[6] = pi.T1;
        pts[7] = pi.T2;
        pts[8] = pi.T3;
        uint256[] memory scl = new uint256[](3);
        scl[0] = uint256(uint160(sender));
        scl[1] = uint256(uint160(spender));
        scl[2] = chainid;
        return BN254.fsChallenge(pts, scl);
    }

    function _checkElGamalC(
        uint256 s_m,
        uint256 s_r,
        BN254.G1Point calldata pk,
        BN254.G1Point calldata C,
        BN254.G1Point calldata T_C,
        uint256 e
    ) internal view returns (bool) {
        BN254.G1Point memory lhs = BN254.add(BN254.mul(BN254.g1(), s_m), BN254.mul(pk, s_r));
        BN254.G1Point memory rhs = BN254.add(BN254.mul(C, e), T_C);
        return BN254.eq(lhs, rhs);
    }

    function _checkElGamalR(
        uint256 s_r,
        BN254.G1Point calldata R,
        BN254.G1Point calldata T_R,
        uint256 e
    ) internal view returns (bool) {
        BN254.G1Point memory lhs = BN254.mul(BN254.g1(), s_r);
        BN254.G1Point memory rhs = BN254.add(BN254.mul(R, e), T_R);
        return BN254.eq(lhs, rhs);
    }

    /// @dev PS pairing product:
    ///   e(s_m*sigma_1, Y) * e(-A_ps, Y) * e(e*sigma_1, X) * e(-e*sigma_2, g_2) == 1
    function _checkPSPairing(
        PSSig calldata sigma,
        RegistrationProof calldata proof,
        PSPubKey storage ipk
    ) internal view returns (bool) {
        BN254.G1Point[] memory a = new BN254.G1Point[](4);
        BN254.G2Point[] memory b = new BN254.G2Point[](4);
        a[0] = BN254.mul(sigma.sigma_1, proof.s_m);
        b[0] = ipk.Y;
        a[1] = BN254.neg(proof.A_ps);
        b[1] = ipk.Y;
        a[2] = BN254.mul(sigma.sigma_1, proof.e);
        b[2] = ipk.X;
        a[3] = BN254.neg(BN254.mul(sigma.sigma_2, proof.e));
        b[3] = BN254.g2();
        return BN254.pairingCheck(a, b);
    }
}
