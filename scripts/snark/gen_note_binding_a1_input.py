"""Generate witness input for note_binding_a1.circom (A1 payload layout).

Public:  nullifier, v, eEncRx[4], eEncRy[4], eEncCx[4], eEncCy[4],
         piX[4], piY[4]                                          (26 signals)

Private: rho, idHash, eNote[4], mIss, sigR[2], sigS,
         rn[4], m_rec[4], u[4], t[4], tm[4], b[4]

Constraints:
  (1) nullifier = Poseidon3(rho, idHash, 4242)
  (2) idHash = Poseidon8(eNote, mIss, sigR, sigS)   (the A1 layout)
  (3) eNote.R = rn*G            (mod-F_R words match eNote[0..1])
  (4) eNote.C = u*G,  u = v + rn*m_rec   (words match eNote[2..3])
  (5) eEnc.R = t*G
  (6) eEnc.C = m_rec*G + tm*G,  tm = t*m_rec
  (7) P_I    = m_rec*G + b*H

Delegates to alberta_buck.wallet.note_binding.make_note_binding_a1_witness
(the production witness builder), so the setup smoke test exercises the
same code path the e2e fixtures use.
"""

import json, os, random, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from alberta_buck.wallet.bn254 import G1, ORDER, mul, rand_scalar
from alberta_buck.wallet.elgamal import elgamal_encrypt
from alberta_buck.wallet.note_binding import make_note_binding_a1_witness


def main():
    seed = 0xCAFE10A1
    rng_state = random.Random(seed)
    rng = lambda: rng_state.getrandbits(256)

    # ---- Recipient identity ----
    m_rec = rand_scalar(rng)
    M_rec = mul(G1, m_rec)

    # ---- Public issuer identity + Schnorr material (opaque payload words) ----
    m_iss = rand_scalar(rng)
    k = rand_scalar(rng)
    sigma_R = mul(G1, k)
    sigma_s = (k + rand_scalar(rng) * rand_scalar(rng)) % ORDER

    # ---- The note: value v encrypted under M_rec ----
    v = 100 * 10**18
    r_note = rand_scalar(rng)
    eNote = elgamal_encrypt(mul(G1, v), M_rec, r_note)

    # ---- Spend-side values ----
    rho = rand_scalar(rng)
    t = rand_scalar(rng)            # eEnc total randomness (r' + s)
    b = rand_scalar(rng)            # P_I blind

    witness = make_note_binding_a1_witness(
        rho=rho, eNote=eNote, v=v, m_issuer=m_iss,
        sigma_R=sigma_R, sigma_s=sigma_s,
        r_note=r_note, m_rec=m_rec, t=t, b=b,
    )

    sys.stderr.write(f"m_rec   = {hex(m_rec)}\n")
    sys.stderr.write(f"v       = {v}\n")
    sys.stderr.write(f"rn      = {hex(r_note)}\n")
    sys.stderr.write(f"t       = {hex(t)}\n")
    sys.stderr.write(f"b       = {hex(b)}\n")
    sys.stderr.write(f"nullifier = {hex(int(witness['nullifier']))}\n")

    print(json.dumps(witness, indent=2))


if __name__ == "__main__":
    main()
