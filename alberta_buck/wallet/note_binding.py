"""Note<->eEnc re-encryption tie — Python prover for INoteBindingVerifier.

Generates the witness and Groth16 proof that the deposit-coupling ciphertext
``eEnc`` re-encrypts, under ``M_rec``, the ciphertext ``eIssCommitted``
committed in the spent note's ``idHash``.

RESERVED — the production trusted setup is not yet run
(``scripts/snark/setup_note_binding.sh``), so ``prove_note_binding`` raises
``FileNotFoundError`` until the zkey and WASM artifacts exist.  The witness
generation helper ``make_note_binding_witness`` works from the compiled
circuit (``circuits/note_binding.circom``).

Circuit: circuits/note_binding.circom
Verifier: src/NoteBindingGroth16Verifier.sol (auto-generated)
Adapter: src/NoteBindingVerifierAdapter.sol
"""

from typing import Tuple
import json, os, subprocess, sys

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, mul, point_to_words, rand_scalar,
)
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.poseidon import poseidon, F_R
from alberta_buck.wallet.notes import NULLIFIER_TAG_B
from alberta_buck.wallet.issuer_reenc import H_POINT

# Paths relative to the repo root.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
_BUILD = os.path.join(_REPO, "build", "snark", "note_binding")


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

    RESERVED — requires the trusted-setup artifacts (zkey, WASM) that are
    produced by ``scripts/snark/setup_note_binding.sh``.  Raises
    ``FileNotFoundError`` until that setup has been run.

    Returns the proof as 256 bytes (abi-packed Groth16 triple: a[2],
    b[2][2], c[2] = 8 words) suitable for ``NoteBindingVerifierAdapter``.
    """
    wasm = os.path.join(_BUILD, "note_binding_js", "note_binding.wasm")
    zkey = os.path.join(_BUILD, "note_binding_0001.zkey")
    if not os.path.exists(wasm):
        raise FileNotFoundError(
            f"WASM not found at {wasm}; run scripts/snark/setup_note_binding.sh"
        )
    if not os.path.exists(zkey):
        raise FileNotFoundError(
            f"zkey not found at {zkey}; run scripts/snark/setup_note_binding.sh"
        )

    witness = make_note_binding_witness(rho, eNote, eIssCommitted, s, m_rec, b, M_I, r_iss)

    # Write witness JSON
    witness_path = os.path.join(_BUILD, "witness.json")
    with open(witness_path, "w") as f:
        json.dump(witness, f)

    # Generate witness
    wtns_path = os.path.join(_BUILD, "witness.wtns")
    subprocess.run(
        ["node", "node_modules/.bin/snarkjs", "wtns", "calculate", wasm,
         witness_path, wtns_path],
        cwd=_REPO, check=True,
    )

    # Generate proof
    proof_path = os.path.join(_BUILD, "proof.json")
    public_path = os.path.join(_BUILD, "public.json")
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


__all__ = [
    "make_note_binding_witness",
    "prove_note_binding",
]
