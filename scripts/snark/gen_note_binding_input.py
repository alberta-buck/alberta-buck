"""Generate witness for note_binding.circom (ElGamal-structure optimization).

Public:  nullifier, eEncRx[4], eEncRy[4], eEncCx[4], eEncCy[4],
         piX[4], piY[4]                                          (25 signals)

Private: rho, idHash, eNote[4], eIss0[4], s[4], m_rec[4],
         sm[4], r[4], rm[4], b[4], MI[2][4],
         R0_limb[2][4], C0_limb[2][4]                            (58 signals)

Constraints:
  (1) nullifier = Poseidon3(rho, idHash, 4242)
  (2) idHash = Poseidon8(eNote, eIss0)
  (3a) eEnc.R = R0 + s*G      (re-encryption of R)
  (3b) eEnc.C = C0 + sm*G     (re-encryption of C, sm = s*m_rec)
  (4a) R0 = r*G               (ElGamal structure — randomness commitment)
  (4b) C0 = M_I + rm*G        (ElGamal structure — rm = r*m_rec)
  (4c) P_I = M_I + b*H        (committed/blinded identity)
"""

import json, random, sys

sys.path.insert(0, '/Users/perry/src/alberta-buck')
from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, mul, point_to_words, rand_scalar,
)
from alberta_buck.wallet.elgamal import elgamal_encrypt
from alberta_buck.wallet.issuer_reenc import H_POINT
from alberta_buck.wallet.poseidon import poseidon, F_R
from alberta_buck.wallet.notes import NULLIFIER_TAG_B


def to_limbs(val, n=4, bits=64):
    """Decompose integer *val* into *n* little-endian 64-bit limbs."""
    mask = (1 << bits) - 1
    return [(val >> (i * bits)) & mask for i in range(n)]


def to_limbs_signed(val, n=4, bits=64):
    """Decompose a value modulo field into 4 limbs."""
    val = val % (1 << (n * bits))
    return to_limbs(val, n, bits)


def main():
    seed = 0xCAFE10AD
    rng = random.Random(seed)

    # ---- Identity keys ----
    m_rec = rand_scalar(lambda: rng.getrandbits(256)) % ORDER
    M_rec = mul(G1, m_rec)

    # ---- Issuer identity M_I and ElGamal encryption eIssCommitted = (R0, C0) ----
    m_iss = rand_scalar(lambda: rng.getrandbits(256)) % ORDER
    M_I = mul(G1, m_iss)
    r_iss = rand_scalar(lambda: rng.getrandbits(256)) % ORDER
    R0 = mul(G1, r_iss)                     # R0 = r * G
    C0 = add(M_I, mul(M_rec, r_iss))        # C0 = M_I + r * M_rec
    R0x, R0y = point_to_words(R0)
    C0x, C0y = point_to_words(C0)

    # ---- Note ciphertext eNote ----
    v_note = rand_scalar(lambda: rng.getrandbits(256)) % ORDER
    r_note = rand_scalar(lambda: rng.getrandbits(256)) % ORDER
    eNote_pt = elgamal_encrypt(mul(G1, v_note), M_rec, r_note)
    eNoteRx, eNoteRy = point_to_words(eNote_pt.R)
    eNoteCx, eNoteCy = point_to_words(eNote_pt.C)
    eNote = [eNoteRx % F_R, eNoteRy % F_R, eNoteCx % F_R, eNoteCy % F_R]
    eIss0_mod = [R0x % F_R, R0y % F_R, C0x % F_R, C0y % F_R]

    # ---- idHash = Poseidon8(eNote, eIss0) ----
    idHash = poseidon(eNote + eIss0_mod) % F_R

    # ---- Note randomness + nullifier ----
    rho = rand_scalar(lambda: rng.getrandbits(256)) % ORDER
    nullifier = poseidon([rho, idHash, NULLIFIER_TAG_B]) % F_R

    # ---- Re-encryption: eEnc = re-encrypt(eIssCommitted, s, M_rec) ----
    s = rand_scalar(lambda: rng.getrandbits(256)) % ORDER
    sm_val = (s * m_rec) % ORDER

    eEnc_R = add(R0, mul(G1, s))            # eEnc.R = R0 + s*G
    eEnc_C = add(C0, mul(G1, sm_val))       # eEnc.C = C0 + sm*G
    eEncRx, eEncRy = point_to_words(eEnc_R)
    eEncCx, eEncCy = point_to_words(eEnc_C)

    # ---- ElGamal structure verification values ----
    rm_val = (r_iss * m_rec) % ORDER        # r * m_rec

    # ---- P_I = M_I + b*H ----
    b = rand_scalar(lambda: rng.getrandbits(256)) % ORDER
    bH = mul(H_POINT, b)
    P_I = add(M_I, bH)
    piX, piY = point_to_words(P_I)

    MIx, MIy = point_to_words(M_I)

    # ---- Off-chain verification ----
    assert eEnc_R == add(R0, mul(G1, s)), "eEnc.R != R0 + s*G"
    assert eEnc_C == add(C0, mul(G1, sm_val)), "eEnc.C != C0 + sm*G"
    assert R0 == mul(G1, r_iss), "R0 != r*G"
    assert C0 == add(M_I, mul(M_rec, r_iss)), "C0 != M_I + r*M_rec"
    assert P_I == add(M_I, bH), "P_I != M_I + b*H"
    assert nullifier == poseidon([rho, idHash, NULLIFIER_TAG_B]) % F_R
    assert idHash == poseidon(eNote + eIss0_mod) % F_R

    # ---- Build witness JSON ----
    witness = {
        # Public (25)
        "nullifier": str(nullifier),
        "eEncRx": [str(v) for v in to_limbs(eEncRx)],
        "eEncRy": [str(v) for v in to_limbs(eEncRy)],
        "eEncCx": [str(v) for v in to_limbs(eEncCx)],
        "eEncCy": [str(v) for v in to_limbs(eEncCy)],
        "piX":    [str(v) for v in to_limbs(piX)],
        "piY":    [str(v) for v in to_limbs(piY)],

        # Private (58)
        "rho":       str(rho),
        "idHash":    str(idHash),
        "eNote":     [str(v) for v in eNote],
        "eIss0":     [str(v) for v in eIss0_mod],
        "s":         [str(v) for v in to_limbs_signed(s)],
        "m_rec":     [str(v) for v in to_limbs_signed(m_rec)],
        "sm":        [str(v) for v in to_limbs_signed(sm_val)],
        "r":         [str(v) for v in to_limbs_signed(r_iss)],
        "rm":        [str(v) for v in to_limbs_signed(rm_val)],
        "b":         [str(v) for v in to_limbs_signed(b)],
        "MI": [
            [str(v) for v in to_limbs(MIx)],
            [str(v) for v in to_limbs(MIy)],
        ],
        "R0_limb": [
            [str(v) for v in to_limbs(R0x)],
            [str(v) for v in to_limbs(R0y)],
        ],
        "C0_limb": [
            [str(v) for v in to_limbs(C0x)],
            [str(v) for v in to_limbs(C0y)],
        ],
    }

    sys.stderr.write(f"m_rec   = {hex(m_rec)}\n")
    sys.stderr.write(f"s       = {hex(s)}\n")
    sys.stderr.write(f"r       = {hex(r_iss)}\n")
    sys.stderr.write(f"b       = {hex(b)}\n")
    sys.stderr.write(f"P_I     = {hex(piX)}, {hex(piY)}\n")
    sys.stderr.write(f"nullifier = {hex(nullifier)}\n")
    sys.stderr.write(f"R1CS constraints: non-linear=2.4M, linear=3.5M\n")

    print(json.dumps(witness, indent=2))


if __name__ == "__main__":
    main()
