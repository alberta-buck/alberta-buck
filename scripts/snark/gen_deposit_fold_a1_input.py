"""Witness for circuits/deposit_fold_a1.circom -- the folded deposit gate, A1.

Builds one honest A1 world and emits the circuit's JSON witness:

  the recipient holds an Identity m_rec and, separately, a receiving secret k;
  a private identity-registry subtree holds receiving_leaf(m_rec, k, salt);
  the issuer mints a note NAMING M_rec and KEYED to pk_recv;
  the spender re-randomizes eRec into eEnc and proves the four relations.

Also emits, with --bad, the witness a payload THIEF would submit: its own
Identity and account, the stolen receiving secret.  That witness must fail --
it is refused at relation (3), because no registered leaf pairs the thief's
Identity with the key it stole.

Run:  PYTHONPATH=.:core/python python scripts/snark/gen_deposit_fold_a1_input.py
Out:  the witness JSON on stdout, diagnostics on stderr
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alberta_buck.registry.merkle_service import rooted_registry
from alberta_buck.wallet.bn254 import G1, ORDER, add, mul, rand_scalar
from alberta_buck.wallet.deposit_fold import (
    DepositFoldRefused, deposit_fold_a1_witness, deposit_fold_witness,
)
from alberta_buck.wallet.elgamal import elgamal_encrypt
from alberta_buck.wallet.notes import id_hash_a1
from alberta_buck.wallet.recvkey import receiving_key
from alberta_buck.wallet.salt import derive_salt
from alberta_buck.wallet.unilateral_a1 import mint_unilateral_a1

KYC = "kyc:ca-ab-2026"
FACE = 100 * 10**18


def build(thief: bool = False):
    rng_state = random.Random(0xF01DA1)
    rng = lambda: rng_state.getrandbits(256)

    # ---- the recipient: an Identity, and separately a mailbox --------------
    m_rec = rand_scalar(rng)
    M_rec = mul(G1, m_rec)
    seed_rec = rand_scalar(rng)
    k_recv, pk_recv = receiving_key(seed_rec)
    salt = derive_salt(seed_rec, KYC)

    # ---- its deposit account, and the credential the registry holds --------
    sk_dep = rand_scalar(rng)
    pk_dep = mul(G1, sk_dep)
    r_E = rand_scalar(rng)
    E_dep = elgamal_encrypt(M_rec, pk_dep, r_E)

    # ---- the private identity-registry subtree ----------------------------
    # The recipient's identity registry, a subtree under the aggregator: its
    # paths run 32 levels, subtree then aggregator, as the circuit folds them.
    priv = rooted_registry()
    priv.insert_receiving(m_rec, k_recv, salt)

    # ---- the public issuer, and the note ----------------------------------
    m_iss = rand_scalar(rng)
    rho = rand_scalar(rng)
    r_prime = rand_scalar(rng)
    note = mint_unilateral_a1(M_rec, pk_recv, v=FACE, rho=rho, m_issuer=m_iss,
                              r_prime=r_prime, rng=rng)

    # ---- the spend: eEnc is eRec re-randomized, total randomness t ---------
    s = rand_scalar(rng)
    t = (note.r_prime + s) % ORDER
    eEnc = elgamal_encrypt(M_rec, pk_recv, t)

    spender = dict(m_rec=m_rec, k=k_recv, sk_dep=sk_dep, salt=salt,
                   E_dep=E_dep, pk_dep=pk_dep, r_E=r_E)

    if thief:
        # Its OWN Identity and account, genuinely registered.  The stolen key.
        m_t = rand_scalar(rng)
        M_t = mul(G1, m_t)
        seed_t = rand_scalar(rng)
        k_t, _ = receiving_key(seed_t)
        salt_t = derive_salt(seed_t, KYC)
        priv.insert_receiving(m_t, k_t, salt_t)
        sk_t = rand_scalar(rng)
        r_Et = rand_scalar(rng)
        spender = dict(m_rec=m_t, k=k_recv, sk_dep=sk_t, salt=salt_t,
                       E_dep=elgamal_encrypt(M_t, mul(G1, sk_t), r_Et),
                       pk_dep=mul(G1, sk_t), r_E=r_Et)

    w = deposit_fold_witness(
        m_rec=spender["m_rec"], k=spender["k"], sk_dep=spender["sk_dep"],
        salt=spender["salt"], E_dep=spender["E_dep"], note_ct=eEnc,
        tree=priv,
    )
    return deposit_fold_a1_witness(
        witness=w, rho=rho, id_hash=note.idHash, e_note=note.eNote, v=FACE,
        m_issuer=m_iss, r_note=note.r_note, t=t, r_E=spender["r_E"], e_dep=spender["E_dep"],
        pk_dep=spender["pk_dep"], e_enc=eEnc, identity_root=priv.root(),
    )


def main():
    thief = "--bad" in sys.argv
    try:
        witness = build(thief=thief)
    except DepositFoldRefused as exc:
        sys.stderr.write(f"REFUSED at relation {exc.relation}: {exc}\n")
        if thief:
            sys.stderr.write("which is the point: the thief has no witness.\n")
            return 0
        raise
    sys.stderr.write(f"nullifier = {witness['nullifier']}\n")
    sys.stderr.write(f"root      = {witness['identityRoot']}\n")
    print(json.dumps(witness, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
