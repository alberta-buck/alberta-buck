"""Note<->eEnc tie — Python provers for INoteBindingVerifier (A2 and A1).

``make_note_binding_witness`` / ``prove_note_binding`` cover the A2 payload
layout: the proof shows the deposit-coupling ciphertext ``eEnc`` re-encrypts,
under ``M_rec``, the ciphertext ``eIssCommitted`` committed in the spent
note's ``idHash`` (circuits/note_binding.circom).

``make_note_binding_a1_witness`` / ``prove_note_binding_a1`` cover the A1
payload layout, whose ``idHash`` commits ``(eNote, m_issuer, sigma)`` instead
of a second ciphertext: the proof shows the spent note's ``eNote`` was
encrypted under the SAME recipient identity ``M_rec = m_rec*G`` that keys
``eEnc`` and opens ``P_I = M_rec + b*H`` — with the note face ``v`` public
(matched on chain to the spend proof's ``face``), which is what pins
``m_rec`` uniquely (circuits/note_binding_a1.circom).

The provers require the trusted-setup artifacts produced by
``scripts/snark/setup_note_binding.sh`` / ``setup_note_binding_a1.sh``
(``make nix-snark-note-binding`` / ``nix-snark-note-binding-a1``): the
compiled C++ witness generator and the phase-2 zkey.  Witness generation uses
the circom C++ calculator -- the WASM calculator cannot handle the ~5.9M-wire
circuits -- run with a 64 MB stack (the generated template frames hold the
G-powers table expansion).  Proving prefers the vendored rapidsnark binary
(``lib/rapidsnark-macOS-arm64-v0.0.8/bin/prover``; seconds at ~2.4-2.9M
constraints) and falls back to snarkjs.

Circuits: circuits/note_binding.circom, circuits/note_binding_a1.circom
Verifiers: src/NoteBindingGroth16Verifier.sol,
           src/NoteBindingA1Groth16Verifier.sol (auto-generated)
Adapter: src/NoteBindingVerifierAdapter.sol (both entry points)
"""

from typing import Tuple
import json, os, subprocess, sys

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, mul, point_to_words, rand_scalar,
)
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.poseidon import poseidon, F_R
from alberta_buck.wallet.notes import NULLIFIER_TAG_B, id_hash_a1
from alberta_buck.wallet.issuer_reenc import H_POINT

# Paths relative to the repo root.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
_BUILD = os.path.join(_REPO, "build", "snark", "note_binding")
_BUILD_A1 = os.path.join(_REPO, "build", "snark", "note_binding_a1")


def to_limbs(val: int, n: int = 4, bits: int = 64):
    """Decompose an integer into *n* little-endian 64-bit limbs."""
    mask = (1 << bits) - 1
    return [(val >> (i * bits)) & mask for i in range(n)]


def make_note_binding_witness(
    rho: int,
    eNote: ElGamalCiphertext,
    eIssCommitted: ElGamalCiphertext,
    s: int,
    m_rec: int,
    b: int,
    M_I,            # decrypted issuer identity point
    r_iss: int,     # ElGamal randomness for eIssCommitted
) -> dict:
    """Build the witness JSON for ``note_binding.circom``.

    The caller supplies ALL private values; this function computes the
    public outputs (nullifier, eEnc, P_I) and returns the complete
    witness dictionary ready for ``snarkjs wtns calculate``.
    """
    M_rec = mul(G1, m_rec)

    # ---- eNote / eIss0 coordinates reduced mod F_R for Poseidon ----
    eNoteRx, eNoteRy = point_to_words(eNote.R)
    eNoteCx, eNoteCy = point_to_words(eNote.C)
    eNote_coords = [eNoteRx % F_R, eNoteRy % F_R, eNoteCx % F_R, eNoteCy % F_R]

    R0x, R0y = point_to_words(eIssCommitted.R)
    C0x, C0y = point_to_words(eIssCommitted.C)
    eIss0_mod = [R0x % F_R, R0y % F_R, C0x % F_R, C0y % F_R]

    # ---- idHash and nullifier ----
    idHash = poseidon(eNote_coords + eIss0_mod) % F_R
    nullifier = poseidon([rho, idHash, NULLIFIER_TAG_B]) % F_R

    # ---- Re-encryption ----
    sm_val = (s * m_rec) % ORDER
    eEnc_R = add(eIssCommitted.R, mul(G1, s))
    eEnc_C = add(eIssCommitted.C, mul(G1, sm_val))
    eEncRx, eEncRy = point_to_words(eEnc_R)
    eEncCx, eEncCy = point_to_words(eEnc_C)

    # ---- ElGamal structure ----
    rm_val = (r_iss * m_rec) % ORDER

    # ---- P_I = M_I + b*H ----
    P_I = add(M_I, mul(H_POINT, b))
    piX, piY = point_to_words(P_I)

    # ---- Verify off-chain ----
    assert eEnc_R == add(eIssCommitted.R, mul(G1, s))
    assert eEnc_C == add(eIssCommitted.C, mul(G1, sm_val))
    assert eIssCommitted.R == mul(G1, r_iss)
    assert eIssCommitted.C == add(M_I, mul(M_rec, r_iss))
    assert P_I == add(M_I, mul(H_POINT, b))
    assert nullifier == poseidon([rho, idHash, NULLIFIER_TAG_B]) % F_R
    assert idHash == poseidon(eNote_coords + eIss0_mod) % F_R

    MIx, MIy = point_to_words(M_I)

    return {
        "nullifier": str(nullifier),
        "eEncRx": [str(v) for v in to_limbs(eEncRx)],
        "eEncRy": [str(v) for v in to_limbs(eEncRy)],
        "eEncCx": [str(v) for v in to_limbs(eEncCx)],
        "eEncCy": [str(v) for v in to_limbs(eEncCy)],
        "piX": [str(v) for v in to_limbs(piX)],
        "piY": [str(v) for v in to_limbs(piY)],
        "rho": str(rho),
        "idHash": str(idHash),
        "eNote": [str(v) for v in eNote_coords],
        "eIss0": [str(v) for v in eIss0_mod],
        "s": [str(v) for v in to_limbs(s % ORDER)],
        "m_rec": [str(v) for v in to_limbs(m_rec % ORDER)],
        "sm": [str(v) for v in to_limbs(sm_val)],
        "r": [str(v) for v in to_limbs(r_iss % ORDER)],
        "rm": [str(v) for v in to_limbs(rm_val)],
        "b": [str(v) for v in to_limbs(b % ORDER)],
        "MI": [[str(v) for v in to_limbs(MIx)], [str(v) for v in to_limbs(MIy)]],
        "R0_limb": [[str(v) for v in to_limbs(R0x)], [str(v) for v in to_limbs(R0y)]],
        "C0_limb": [[str(v) for v in to_limbs(C0x)], [str(v) for v in to_limbs(C0y)]],
    }


def prove_note_binding(
    rho: int,
    eNote: ElGamalCiphertext,
    eIssCommitted: ElGamalCiphertext,
    s: int,
    m_rec: int,
    b: int,
    M_I,
    r_iss: int,
) -> bytes:
    """Generate a Groth16 note-binding proof.

    Requires the trusted-setup artifacts (the compiled C++ witness generator
    and the zkey) produced by ``scripts/snark/setup_note_binding.sh``.  Raises
    ``FileNotFoundError`` until that setup has been run.

    Returns the proof as 256 bytes (abi-packed Groth16 triple: a[2],
    b[2][2], c[2] = 8 words) suitable for ``NoteBindingVerifierAdapter``.
    """
    witness_gen = os.path.join(_BUILD, "note_binding_cpp", "note_binding")
    zkey = os.path.join(_BUILD, "note_binding_0001.zkey")
    if not os.path.exists(witness_gen):
        raise FileNotFoundError(
            f"C++ witness generator not found at {witness_gen}; "
            "run scripts/snark/setup_note_binding.sh"
        )
    if not os.path.exists(zkey):
        raise FileNotFoundError(
            f"zkey not found at {zkey}; run scripts/snark/setup_note_binding.sh"
        )

    witness = make_note_binding_witness(rho, eNote, eIssCommitted, s, m_rec, b, M_I, r_iss)
    return _groth16_prove(_BUILD, witness_gen, zkey, witness)


def _groth16_prove(build: str, witness_gen: str, zkey: str, witness: dict) -> bytes:
    """Run the C++ witness calculator + rapidsnark/snarkjs over *witness*;
    return the abi-packed Groth16 triple (a[2], b[2][2], c[2] = 8 words)."""
    # Write witness input JSON
    input_path = os.path.join(build, "prove_input.json")
    with open(input_path, "w") as f:
        json.dump(witness, f)

    # Generate witness via the circom C++ calculator.  64 MB stack: the
    # generated template-run functions hold ~5.4 MB frames (G-powers table).
    wtns_path = os.path.join(build, "prove_witness.wtns")
    subprocess.run(
        ["bash", "-c",
         f"ulimit -s 65520 && '{witness_gen}' '{input_path}' '{wtns_path}'"],
        cwd=_REPO, check=True,
    )

    # Generate proof: rapidsnark when vendored (seconds), snarkjs fallback.
    proof_path = os.path.join(build, "proof.json")
    public_path = os.path.join(build, "public.json")
    rapidsnark = os.path.join(
        _REPO, "lib", "rapidsnark-macOS-arm64-v0.0.8", "bin", "prover")
    if os.path.exists(rapidsnark):
        subprocess.run(
            [rapidsnark, zkey, wtns_path, proof_path, public_path],
            cwd=_REPO, check=True,
        )
    else:
        subprocess.run(
            ["node", "node_modules/.bin/snarkjs", "groth16", "prove", zkey,
             wtns_path, proof_path, public_path],
            cwd=_REPO, check=True,
        )

    # Pack proof into 256 bytes (8 uint256 words)
    with open(proof_path) as f:
        proof = json.load(f)

    def w(x):
        return int(x).to_bytes(32, "big")

    return (
        w(proof["pi_a"][0]) + w(proof["pi_a"][1]) +
        w(proof["pi_b"][0][0]) + w(proof["pi_b"][0][1]) +
        w(proof["pi_b"][1][0]) + w(proof["pi_b"][1][1]) +
        w(proof["pi_c"][0]) + w(proof["pi_c"][1])
    )


# ======================= A1 payload layout ==================================

def make_note_binding_a1_witness(
    rho: int,
    eNote: ElGamalCiphertext,
    v: int,
    m_issuer: int,
    sigma_R,        # issuer Schnorr signature nonce point
    sigma_s: int,
    r_note: int,    # eNote ElGamal randomness (travels with the opening)
    m_rec: int,     # the recipient's IDENTITY scalar (a base multiple)
    k_recv: int,    # the recipient's RECEIVING secret (a product factor)
    t: int,         # eEnc total randomness (r' + s for a re-encrypted eRec)
    b: int,
) -> dict:
    """Build the witness JSON for ``note_binding_a1.circom``.

    The caller supplies ALL private values; this function computes the
    public outputs (nullifier, eEnc, P_I) and returns the complete witness
    dictionary ready for the C++ witness calculator.  ``eEnc`` is the fresh
    encryption ``(t*G, M_rec + t*pk_recv)``: the recipient Identity NAMED in
    the plaintext, KEYED to that Identity's receiving key — exactly a
    re-encryption, with total randomness ``t``, of the note's ``eRec``.

    A1 needs BOTH scalars, unlike A2.  ``m_rec`` enters as a base multiple
    (``M_rec = m_rec*G`` in the eEnc and P_I relations) and ``k_recv`` only
    inside the witnessed products (``u = v + rn*k``, ``tm = t*k``).  That is
    why this circuit gained a private input where A2's merely renamed one.
    """
    M_rec = mul(G1, m_rec % ORDER)
    pk_recv = mul(G1, k_recv % ORDER)

    # ---- eNote coordinates reduced mod F_R for Poseidon ----
    eNoteRx, eNoteRy = point_to_words(eNote.R)
    eNoteCx, eNoteCy = point_to_words(eNote.C)
    eNote_coords = [eNoteRx % F_R, eNoteRy % F_R, eNoteCx % F_R, eNoteCy % F_R]

    # ---- idHash (A1 layout) and nullifier ----
    sigRx, sigRy = point_to_words(sigma_R)
    idHash = id_hash_a1(eNote, m_issuer, sigma_R, sigma_s)
    nullifier = poseidon([rho, idHash, NULLIFIER_TAG_B]) % F_R

    # ---- Witnessed scalars ----
    u_val = (v + r_note * k_recv) % ORDER         # eNote.C = u*G
    tm_val = (t * k_recv) % ORDER                 # eEnc.C = M_rec + tm*G

    # ---- eEnc = (t*G, M_rec + tm*G) ----
    eEnc_R = mul(G1, t % ORDER)
    eEnc_C = add(M_rec, mul(G1, tm_val))
    eEncRx, eEncRy = point_to_words(eEnc_R)
    eEncCx, eEncCy = point_to_words(eEnc_C)

    # ---- P_I = M_rec + b*H ----
    P_I = add(M_rec, mul(H_POINT, b))
    piX, piY = point_to_words(P_I)

    # ---- Verify off-chain ----
    assert eNote.R == mul(G1, r_note % ORDER), "eNote.R != rn*G"
    assert eNote.C == add(mul(G1, v % ORDER), mul(pk_recv, r_note)), \
        "eNote.C != v*G + rn*pk_recv"
    assert eNote.C == mul(G1, u_val), "eNote.C != u*G"
    assert eEnc_C == mul(G1, (m_rec + tm_val) % ORDER)
    assert nullifier == poseidon([rho, idHash, NULLIFIER_TAG_B]) % F_R

    return {
        "nullifier": str(nullifier),
        "v": str(v),
        "eEncRx": [str(x) for x in to_limbs(eEncRx)],
        "eEncRy": [str(x) for x in to_limbs(eEncRy)],
        "eEncCx": [str(x) for x in to_limbs(eEncCx)],
        "eEncCy": [str(x) for x in to_limbs(eEncCy)],
        "piX": [str(x) for x in to_limbs(piX)],
        "piY": [str(x) for x in to_limbs(piY)],
        "rho": str(rho),
        "idHash": str(idHash),
        "eNote": [str(x) for x in eNote_coords],
        "mIss": str(m_issuer % F_R),
        "sigR": [str(sigRx % F_R), str(sigRy % F_R)],
        "sigS": str(sigma_s % F_R),
        "rn": [str(x) for x in to_limbs(r_note % ORDER)],
        "m_rec": [str(x) for x in to_limbs(m_rec % ORDER)],
        "k_recv": [str(x) for x in to_limbs(k_recv % ORDER)],
        "u": [str(x) for x in to_limbs(u_val)],
        "t": [str(x) for x in to_limbs(t % ORDER)],
        "tm": [str(x) for x in to_limbs(tm_val)],
        "b": [str(x) for x in to_limbs(b % ORDER)],
    }


def prove_note_binding_a1(
    rho: int,
    eNote: ElGamalCiphertext,
    v: int,
    m_issuer: int,
    sigma_R,
    sigma_s: int,
    r_note: int,
    m_rec: int,
    k_recv: int,
    t: int,
    b: int,
) -> bytes:
    """Generate a Groth16 A1 note-binding proof (note_binding_a1.circom).

    Requires the trusted-setup artifacts produced by
    ``scripts/snark/setup_note_binding_a1.sh``; raises ``FileNotFoundError``
    until that setup has been run.  Returns the abi-packed Groth16 triple
    (256 bytes) suitable for ``NoteBindingVerifierAdapter.verifyNoteBindingA1``.
    """
    witness_gen = os.path.join(_BUILD_A1, "note_binding_a1_cpp", "note_binding_a1")
    zkey = os.path.join(_BUILD_A1, "note_binding_a1_0001.zkey")
    if not os.path.exists(witness_gen):
        raise FileNotFoundError(
            f"C++ witness generator not found at {witness_gen}; "
            "run scripts/snark/setup_note_binding_a1.sh"
        )
    if not os.path.exists(zkey):
        raise FileNotFoundError(
            f"zkey not found at {zkey}; run scripts/snark/setup_note_binding_a1.sh"
        )

    witness = make_note_binding_a1_witness(
        rho, eNote, v, m_issuer, sigma_R, sigma_s, r_note, m_rec, k_recv, t, b)
    return _groth16_prove(_BUILD_A1, witness_gen, zkey, witness)


__all__ = [
    "make_note_binding_witness",
    "prove_note_binding",
    "make_note_binding_a1_witness",
    "prove_note_binding_a1",
]
