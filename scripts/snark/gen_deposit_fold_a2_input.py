"""Witness for circuits/deposit_fold_a2.circom -- the folded deposit gate, A2.

The A1 generator's world plus the two things A2 needs: the note's committed
issuer ciphertext, and the issuer's own registry association so the recipient
can prove -- at spend -- that the Identity it decrypted is registered.  The
issuer ships only its salt; the path is rebuilt here from the published subtree,
because paths go stale as the subtree grows and salts do not.

With --bad, the witness a payload THIEF would submit: refused at relation (3).
With --bogus, a colluding issuer keying the note to a throwaway point: the
recipient decrypts garbage, which is registered nowhere, so relation (5)
refuses it.  That is the relation A1 does not need.

Run:  PYTHONPATH=.:core/python python scripts/snark/gen_deposit_fold_a2_input.py
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alberta_buck.registry.tree import IdentityMerkleTree, identity_leaf_salted
from alberta_buck.wallet.bn254 import G1, ORDER, mul, rand_scalar
from alberta_buck.wallet.deposit_fold import (
    DepositFoldRefused, deposit_fold_a2_witness, deposit_fold_witness,
)
from alberta_buck.wallet.elgamal import elgamal_encrypt
from alberta_buck.wallet.recvkey import receiving_key
from alberta_buck.wallet.salt import derive_salt
from alberta_buck.wallet.unilateral_a2 import mint_unilateral_a2

KYC = "kyc:ca-ab-2026"
DEPTH = 10
FACE = 100 * 10**18
ISSUER, CHAINID = 0xA11CE, 1


def build(thief: bool = False, bogus: bool = False):
    rng_state = random.Random(0xF01DA2)
    rng = lambda: rng_state.getrandbits(256)

    # ---- the recipient: an Identity, and separately a mailbox --------------
    m_rec = rand_scalar(rng)
    M_rec = mul(G1, m_rec)
    seed_rec = rand_scalar(rng)
    k_recv, pk_recv = receiving_key(seed_rec)
    salt_rec = derive_salt(seed_rec, KYC)

    sk_dep = rand_scalar(rng)
    pk_dep = mul(G1, sk_dep)
    r_E = rand_scalar(rng)
    E_dep = elgamal_encrypt(M_rec, pk_dep, r_E)

    # ---- the issuer: a PRIVATE identity, with two associations ------------
    # One is its mailbox (a receiving leaf); the other is the plain salted
    # identity leaf that lets a counterparty name it.  Distinct salts, so the
    # salt it ships says nothing about the key that reads its mail.
    m_iss = rand_scalar(rng)
    M_iss = mul(G1, m_iss)
    sk_iss = rand_scalar(rng)
    E_reg_iss = elgamal_encrypt(M_iss, mul(G1, sk_iss), rand_scalar(rng))
    seed_iss = rand_scalar(rng)
    k_iss, pk_iss_recv = receiving_key(seed_iss)
    salt_iss_mailbox = derive_salt(seed_iss, KYC)
    salt_iss_named = derive_salt(seed_iss, KYC, 1)      # the SHIPPED one

    priv = IdentityMerkleTree(depth=DEPTH, private=True)
    priv.insert_receiving(m_rec, k_recv, salt_rec)            # the recipient
    priv.insert_receiving(m_iss, k_iss, salt_iss_mailbox)     # the issuer's mailbox
    priv.insert_leaf(identity_leaf_salted(M_iss, salt_iss_named))  # names the issuer

    # ---- the mint ---------------------------------------------------------
    rho = rand_scalar(rng)
    r_prime = rand_scalar(rng)
    target = mul(G1, rand_scalar(rng)) if bogus else pk_recv
    note = mint_unilateral_a2(sk_iss, E_reg_iss, target, v=FACE, rho=rho,
                              issuer=ISSUER, chainid=CHAINID,
                              r_prime=r_prime, salt_iss=salt_iss_named, rng=rng)

    # ---- the spend: eEnc is eIss re-randomized, total randomness t --------
    s = rand_scalar(rng)
    t = (note.r_prime + s) % ORDER
    eEnc = elgamal_encrypt(note.M_I, target, t)

    spender = dict(m_rec=m_rec, k=k_recv, sk_dep=sk_dep, salt=salt_rec,
                   E_dep=E_dep, pk_dep=pk_dep, r_E=r_E)
    if thief:
        m_t = rand_scalar(rng)
        M_t = mul(G1, m_t)
        seed_t = rand_scalar(rng)
        k_t, _ = receiving_key(seed_t)
        salt_t = derive_salt(seed_t, KYC)
        priv.insert_receiving(m_t, k_t, salt_t)
        sk_t, r_Et = rand_scalar(rng), rand_scalar(rng)
        spender = dict(m_rec=m_t, k=k_recv, sk_dep=sk_t, salt=salt_t,
                       E_dep=elgamal_encrypt(M_t, mul(G1, sk_t), r_Et),
                       pk_dep=mul(G1, sk_t), r_E=r_Et)

    w = deposit_fold_witness(
        m_rec=spender["m_rec"], k=spender["k"], sk_dep=spender["sk_dep"],
        salt=spender["salt"], E_dep=spender["E_dep"], note_ct=eEnc,
        tree=priv, rng=rng,
    )

    # The issuer's path, rebuilt from the published subtree using the salt the
    # payload shipped -- never shipped itself, because paths go stale.
    iss_leaf = identity_leaf_salted(w.M, note.salt_iss)
    if iss_leaf not in priv.leaves:
        raise AssertionError(
            "relation (5): the decrypted Identity is registered nowhere -- "
            "a bogus eIss decrypts to a non-member, which is what (5) catches")
    iss_path = priv.path(priv.leaves.index(iss_leaf))

    return deposit_fold_a2_witness(
        witness=w, rho=rho, id_hash=note.idHash, e_note=note.eNote,
        e_iss=note.eIss, r_prime=note.r_prime, t=t, r_E=spender["r_E"],
        e_dep=spender["E_dep"], pk_dep=spender["pk_dep"], e_enc=eEnc,
        salt_iss=note.salt_iss, iss_path=iss_path, identity_root=priv.root(),
    )


def main():
    thief, bogus = "--bad" in sys.argv, "--bogus" in sys.argv
    try:
        witness = build(thief=thief, bogus=bogus)
    except DepositFoldRefused as exc:
        sys.stderr.write(f"REFUSED at relation {exc.relation}: {exc}\n")
        return 0 if thief else 1
    except AssertionError as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return 0 if bogus else 1
    if thief or bogus:
        sys.stderr.write("BUILT -- must not happen!\n")
        return 1
    sys.stderr.write(f"nullifier = {witness['nullifier']}\n")
    sys.stderr.write(f"root      = {witness['identityRoot']}\n")
    print(json.dumps(witness, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
