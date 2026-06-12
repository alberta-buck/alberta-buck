"""End-to-end Notes fixture world builder (all three flavors).

Builds ONE mutually-consistent set of artifacts per flavor so that
test/NotesE2E.t.sol can drive the full real-verifier lifecycle:

    identity world -> registry binds (+ incremental identityRoot)
    wallet note    -> pinned-opening mint proof (mint_batch / mint_batch_a2)
    spend proof    -> against the replayed note tree (prove_spend.js)
    deposit sigma  -> pinned to (depositor address, chainid=1)
    g1tie proof    -> membership of the sigma's committed P point
    note binding   -> (A1/A2) the note<->eEnc re-encryption tie

Subcommands:
    world    --flavor {a1,a2,b1}   emit world.json + prover inputs
    assemble --flavor {a1,a2,b1}   merge all proofs -> alberta_buck/test/vectors/e2e/<flavor>.json

The driver is scripts/snark/gen_e2e_fixtures.sh.

The note binding uses the layout-matched circuit per flavor: A2 opens
idHash = Poseidon8(eNote, eIss) (circuits/note_binding.circom); A1's idHash
commits (eNote, m_issuer, sigma), so its tie is through the note's own value
ciphertext with the face public (circuits/note_binding_a1.circom,
make_note_binding_a1_witness).  B1 is bearer -- no tie.
"""

import argparse
import json
import os
import random
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from alberta_buck.wallet.bn254 import G1, ORDER, add, mul, point_to_words, rand_scalar
from alberta_buck.wallet.elgamal import ElGamalCiphertext, elgamal_encrypt
from alberta_buck.wallet.poseidon import F_R
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.issuer_reenc import H_POINT
from alberta_buck.wallet.notes import (
    FLAVOR_A1, FLAVOR_B1, NoteOpening, note_commitment, id_hash_b1, nullifier_b,
)
from alberta_buck.wallet.schnorr import batch_commitment, issuer_schnorr_sign
from alberta_buck.wallet.unilateral_a1 import mint_unilateral_a1
from alberta_buck.wallet.unilateral_a2 import (
    IdentityTree, mint_unilateral_a2, deposit_couple_prove, deposit_couple_verify,
)
from alberta_buck.wallet.b1_binding import b1_bind_prove, b1_bind_verify
from alberta_buck.wallet.note_binding import (
    make_note_binding_witness, make_note_binding_a1_witness,
)
from alberta_buck.wallet.vectors import ALICE_FIELDS, BOB_FIELDS
from alberta_buck.registry.tree import identity_leaf

CHAINID  = 1
FACE     = 250_000000                                # 250.000000 BUCK (6 dp)
ISSUER   = 0xA11CE00000000000000000000000000000A11CE
DEPOSIT  = 0xDE9051700000000000000000000000000DE90517
PAYOUT   = DEPOSIT       # the depositor's own registered account receives the
                         # payout in every flavor (B1 requires it -- the
                         # depositor binding is verified against `recipient` --
                         # and using it for A1/A2 keeps the receipt's payee
                         # account, the sigma's account, and the event's
                         # recipient one and the same)

E2E_DIR  = os.path.join(REPO, "build", "snark", "e2e")
# The assembled fixtures live in the Python package tree (shipped as
# alberta_buck package data); test/NotesE2E.t.sol reads the same files.
VEC_DIR  = os.path.join(REPO, "alberta_buck", "test", "vectors", "e2e")

SEEDS = {"a1": 0xE2EA1, "a2": 0xE2EA2, "b1": 0xE2EB1}


def to_limbs(val, n=4, bits=64):
    mask = (1 << bits) - 1
    return [(val >> (i * bits)) & mask for i in range(n)]


def pt(P):
    x, y = point_to_words(P)
    return {"x": str(x), "y": str(y)}


def ct(c: ElGamalCiphertext):
    return {"R": pt(c.R), "C": pt(c.C)}


def account(m, rng):
    """One registered Fountain account bound to identity scalar m."""
    sk = rand_scalar(rng)
    pk = mul(G1, sk)
    M = mul(G1, m)
    E = elgamal_encrypt(M, pk, rand_scalar(rng))
    return dict(m=m, M=M, sk=sk, pk=pk, E=E)


def reenc(c: ElGamalCiphertext, key_point, s):
    """Fresh re-encryption of c under key_point with randomness s."""
    return ElGamalCiphertext(
        R=add(c.R, mul(G1, s)),
        C=add(c.C, mul(key_point, s)),
    )


def ct_words_mod_fr(c: ElGamalCiphertext):
    """The 4 mod-F_R words (Rx, Ry, Cx, Cy) used in Poseidon payloads."""
    rx, ry = point_to_words(c.R)
    cx, cy = point_to_words(c.C)
    return [rx % F_R, ry % F_R, cx % F_R, cy % F_R]


def build_world(flavor: str):
    rng_state = random.Random(SEEDS[flavor])
    rng = lambda: rng_state.getrandbits(256)
    out_dir = os.path.join(E2E_DIR, flavor)
    os.makedirs(out_dir, exist_ok=True)

    # ---- Identities and registered accounts --------------------------------
    # REAL named identities (the canonical Alice/Bob KYC data the wallet's
    # identity vectors use), so the receipts the fixture supports can name
    # humans via the point->human bridge: m = keccak(canonical_identity_data).
    # Bob (a Corporate Registration) is the natural Note issuer; Alice is the
    # addressed recipient (A1/A2) / bearer depositor (B1).
    iss_canonical = canonical_identity_data(BOB_FIELDS)
    ctr_canonical = canonical_identity_data(ALICE_FIELDS)
    m_iss = identity_scalar(iss_canonical)
    m_ctr = identity_scalar(ctr_canonical)

    issuer_acct = account(m_iss, rng)
    dep_acct = account(m_ctr, rng)               # the depositing account
    M_ctr = mul(G1, m_ctr)

    # One leaf per identity, inserted in bind order (issuer, then depositor):
    # the on-chain incremental accumulator must replay to the same root.
    tree = IdentityTree()
    tree.insert(issuer_acct["M"])
    tree.insert(M_ctr)

    binds = [
        {
            "addr": f"0x{ISSUER:040x}",
            "pk": pt(issuer_acct["pk"]), "E": ct(issuer_acct["E"]),
            "isPublic": flavor != "a2",
            "identityLeaf": str(identity_leaf(issuer_acct["M"])),
        },
        {
            "addr": f"0x{DEPOSIT:040x}",
            "pk": pt(dep_acct["pk"]), "E": ct(dep_acct["E"]),
            "isPublic": False,
            "identityLeaf": str(identity_leaf(M_ctr)),
        },
    ]

    # ---- The wallet note ----------------------------------------------------
    rho = rand_scalar(rng)
    if flavor == "a2":
        note = mint_unilateral_a2(issuer_acct["sk"], issuer_acct["E"], M_ctr,
                                  v=FACE, rho=rho, issuer=ISSUER,
                                  chainid=CHAINID, rng=rng)
        opening = note.opening
        eCommitted = note.eIss                   # committed in idHash
        M_named = note.M_I                       # the membership target (issuer)
        r_committed = note.r_prime
        eNote = note.eNote
        note_payload = {"eNote": ct(eNote), "eIss": ct(eCommitted)}
    elif flavor == "a1":
        # The in-payload (sigma_R, sigma_s) is the issuer's identity-binding
        # signature over the delivery payload (synthetic domain here, as in
        # test_unilateral_a1).
        k = rand_scalar(rng)
        sigma_R = mul(G1, k)
        sigma_s = (k + rand_scalar(rng) * rand_scalar(rng)) % ORDER
        note = mint_unilateral_a1(M_ctr, v=FACE, rho=rho, m_issuer=m_iss,
                                  sigma_R=sigma_R, sigma_s=sigma_s, rng=rng)
        opening = note.opening
        eCommitted = note.eRec                   # the sigma's ciphertext (NOT in idHash)
        M_named = M_ctr                          # membership target (recipient)
        r_committed = note.r_prime
        eNote = note.eNote
        note_payload = {"eNote": ct(eNote), "eRec": ct(note.eRec),
                        "sigma_R": pt(sigma_R), "sigma_s": str(sigma_s)}
    else:  # b1
        k = rand_scalar(rng)
        sigma_R = mul(G1, k)
        sigma_s = (k + rand_scalar(rng) * rand_scalar(rng)) % ORDER
        idh = id_hash_b1(m_iss, sigma_R, sigma_s)
        opening = NoteOpening(FLAVOR_B1, FACE, rho, idh, 0)
        note = None
        eCommitted = None
        M_named = M_ctr                          # membership target (depositor)
        r_committed = None
        eNote = None
        note_payload = {"sigma_R": pt(sigma_R), "sigma_s": str(sigma_s)}

    cm = note_commitment(opening)
    nf = nullifier_b(opening.rho, opening.id_hash)

    # ---- Deposit-side proofs -------------------------------------------------
    b = rand_scalar(rng)                          # the P blind, shared everywhere

    if flavor in ("a1", "a2"):
        s = rand_scalar(rng)                      # eEnc re-randomization
        eEnc = reenc(eCommitted, M_ctr, s)
        dc = deposit_couple_prove(m_ctr, dep_acct["sk"], dep_acct["E"], eEnc,
                                  account=DEPOSIT, chainid=CHAINID, b=b, rng=rng)
        assert deposit_couple_verify(dep_acct["pk"], dep_acct["E"], eEnc, dc,
                                     account=DEPOSIT, chainid=CHAINID)
        sigma_json = {
            "kind": "depositCoupling",
            "eEnc": ct(eEnc),
            "dc": {
                "e": str(dc.e), "s_m": str(dc.s_m), "s_s": str(dc.s_s),
                "s_b": str(dc.s_b),
                "A2": pt(dc.A2), "A3": pt(dc.A3), "A4": pt(dc.A4),
                "P_I": pt(dc.P_I),
            },
        }
        P_committed = dc.P_I
    else:
        binding, eDepForIss = b1_bind_prove(m_ctr, dep_acct["sk"], dep_acct["E"],
                                            issuer_acct["pk"], account=DEPOSIT,
                                            chainid=CHAINID, b=b, rng=rng)
        assert b1_bind_verify(dep_acct["pk"], dep_acct["E"], issuer_acct["pk"],
                              eDepForIss, binding, account=DEPOSIT, chainid=CHAINID)
        sigma_json = {
            "kind": "depositorBinding",
            "eDepForIss": ct(eDepForIss),
            "db": {
                "e": str(binding.e), "s_m": str(binding.s_m),
                "s_s": str(binding.s_s), "s_r": str(binding.s_r),
                "s_b": str(binding.s_b),
                "A2": pt(binding.A2), "A4": pt(binding.A4),
                "B1": pt(binding.B1), "B2": pt(binding.B2),
                "A_p": pt(binding.A_p), "P_dep": pt(binding.P_dep),
            },
        }
        P_committed = binding.P_dep

    # Sanity: the committed point opens as M_named + b*H everywhere.
    assert P_committed == add(M_named, mul(H_POINT, b))

    # ---- g1tie membership input ----------------------------------------------
    Mx, My = point_to_words(M_named)
    T = mul(H_POINT, b)
    Tx, Ty = point_to_words(T)
    PIx, PIy = point_to_words(P_committed)
    leaf_index = 0 if flavor == "a2" else 1       # issuer leaf vs counterparty leaf
    proof_path = tree.path(leaf_index)
    g1tie_input = {
        "identityRoot": str(tree.root()),
        "PI_x": [str(v) for v in to_limbs(PIx)],
        "PI_y": [str(v) for v in to_limbs(PIy)],
        "Mx": [str(v) for v in to_limbs(Mx)],
        "My": [str(v) for v in to_limbs(My)],
        "Mx_mod": str(Mx % F_R),
        "My_mod": str(My % F_R),
        "Tx": [str(v) for v in to_limbs(Tx)],
        "Ty": [str(v) for v in to_limbs(Ty)],
        "pathElements": [str(x) for x in proof_path.siblings],
        "pathIndices": [str(x) for x in proof_path.index_bits],
    }
    with open(os.path.join(out_dir, "g1tie_input.json"), "w") as f:
        json.dump(g1tie_input, f, indent=2)

    # ---- note-binding input (layout-matched circuit per addressed flavor) ----
    if flavor in ("a1", "a2"):
        if flavor == "a2":
            nb = make_note_binding_witness(
                rho=opening.rho, eNote=eNote, eIssCommitted=eCommitted,
                s=s, m_rec=m_ctr, b=b, M_I=M_named, r_iss=r_committed,
            )
        else:
            # A1: the tie is through eNote itself (face public); eEnc's total
            # randomness is the mint r' plus the deposit re-randomization s.
            nb = make_note_binding_a1_witness(
                rho=opening.rho, eNote=eNote, v=FACE, m_issuer=m_iss,
                sigma_R=sigma_R, sigma_s=sigma_s, r_note=note.r_note,
                m_rec=m_ctr, t=(note.r_prime + s) % ORDER, b=b,
            )
        assert int(nb["nullifier"]) == nf, "binding/spend nullifier mismatch"
        # The builder's public eEnc / P_I must be the exact on-chain points the
        # sigma carries (the contract derives the proof's publics from them).
        recompose = lambda limbs: sum(int(w) << (64 * i) for i, w in enumerate(limbs))
        assert (recompose(nb["eEncRx"]), recompose(nb["eEncRy"])) == point_to_words(eEnc.R)
        assert (recompose(nb["eEncCx"]), recompose(nb["eEncCy"])) == point_to_words(eEnc.C)
        assert (recompose(nb["piX"]), recompose(nb["piY"])) == point_to_words(P_committed)
        with open(os.path.join(out_dir, "note_binding_input.json"), "w") as f:
            json.dump(nb, f, indent=2)

    # ---- mint prover pin args -------------------------------------------------
    if flavor == "a2":
        en = ct_words_mod_fr(eNote)
        ei = ct_words_mod_fr(eCommitted)
        mint_args = [
            "--name=e2e_a2", "--n=1", f"--live-leaves={FACE}",
            f"--rho=0:{opening.rho}",
            "--enote=0:" + ",".join(str(w) for w in en),
            "--eiss=0:" + ",".join(str(w) for w in ei),
        ]
        # The wallet idHash must equal Poseidon8 over these words.
        from alberta_buck.wallet.poseidon import poseidon
        assert poseidon(en + ei) == opening.id_hash, "idHash layout mismatch"
    else:
        mint_args = [
            f"--name=e2e_{flavor}", "--n=1",
            f"--pin=0:{opening.flavor},{opening.v},{opening.rho},{opening.id_hash}",
        ]

    # ---- world manifest ---------------------------------------------------------
    payout = PAYOUT
    world = {
        "flavor": flavor,
        "chainid": CHAINID,
        "issuer": f"0x{ISSUER:040x}",
        "depositor": f"0x{DEPOSIT:040x}",
        "payout": f"0x{payout:040x}",
        "face": str(FACE),
        "binds": binds,
        "identityRoot": str(tree.root()),
        "opening": {
            "flavor": opening.flavor, "v": str(opening.v),
            "rho": str(opening.rho), "idHash": str(opening.id_hash),
            "cm": str(cm), "nullifier": str(nf),
        },
        "sigma": sigma_json,
        "mint_args": mint_args,
        # The two wallets, in full -- canonical KYC preimages, identity
        # scalars, and account keys.  TEST identities (the same published
        # Alice/Bob the canonical identity.json vectors commit), retained so
        # the AB-RCPT/1 receipt layer can be exercised over this exact
        # real-proof world (test_receipt_e2e.py and the executable receipt
        # document) -- the receipts' point->human bridge needs the preimages,
        # and the role-dependent self-naming proofs need the account secrets.
        "parties": {
            "issuer": {
                "addr": f"0x{ISSUER:040x}",
                "identity": iss_canonical, "m": str(m_iss),
                "M": pt(issuer_acct["M"]), "pk": pt(issuer_acct["pk"]),
                "sk": str(issuer_acct["sk"]), "E": ct(issuer_acct["E"]),
            },
            "depositor": {
                "addr": f"0x{DEPOSIT:040x}",
                "identity": ctr_canonical, "m": str(m_ctr),
                "M": pt(dep_acct["M"]), "pk": pt(dep_acct["pk"]),
                "sk": str(dep_acct["sk"]), "E": ct(dep_acct["E"]),
            },
        },
        # The Identity-M note payload (the idHash preimage material) that
        # travels off chain with the note -- exactly what an AB-RCPT/1
        # receipt's `note` record conveys.  (B1's eDepForIss is spend-side
        # material and lives in sigma.eDepForIss.)
        "notePayload": note_payload,
    }
    if flavor == "a2":
        world["a2Binding"] = {
            "eIss": ct(eCommitted),
            "proof": {
                "e": str(note.binding.e),
                "s_r": str(note.binding.s_r), "s_b": str(note.binding.s_b),
                "s_s": str(note.binding.s_s), "s_g": str(note.binding.s_g),
                "A1": pt(note.binding.A1), "A2": pt(note.binding.A2),
                "A3": pt(note.binding.A3), "A4": pt(note.binding.A4),
                "A5": pt(note.binding.A5),
                "Q": pt(note.binding.Q), "U": pt(note.binding.U),
                "T": pt(note.binding.T),
            },
        }
    with open(os.path.join(out_dir, "world.json"), "w") as f:
        json.dump(world, f, indent=2)

    print(f"[world:{flavor}] cm={hex(cm)[:18]}... nf={hex(nf)[:18]}... "
          f"identityRoot={hex(tree.root())[:18]}...")
    print(f"[world:{flavor}] mint args: {' '.join(mint_args)}")


def assemble(flavor: str):
    out_dir = os.path.join(E2E_DIR, flavor)
    os.makedirs(VEC_DIR, exist_ok=True)
    world = json.load(open(os.path.join(out_dir, "world.json")))

    # Mint fixture (from prove_mint_batch[_a2].js).
    nd = "mint_batch_a2_n1" if flavor == "a2" else "mint_batch_n1"
    mint = json.load(open(os.path.join(
        REPO, "build", "snark", nd, "fixtures", f"e2e_{flavor}.json")))

    # The batch Schnorr (public-issuer mints only; the issuer's account key is
    # carried in the world's `parties` record).
    if flavor != "a2":
        cms = [int(c) for c in mint["public"]["cm"]]
        h_batch = batch_commitment(cms)
        sig = issuer_schnorr_sign(int(world["parties"]["issuer"]["sk"]), h_batch,
                                  int(world["issuer"], 16), CHAINID)
        world["issuerSchnorr"] = {
            "e": str(sig.e), "s": str(sig.s), "R": pt(sig.R),
            "hBatch": str(h_batch),
        }

    # Spend fixture (from prove_spend.js).
    spend = json.load(open(os.path.join(out_dir, "spend.json")))
    assert int(spend["spend"]["public"]["nullifier"]) == int(world["opening"]["nullifier"]), \
        "spend/world nullifier mismatch"

    # g1tie membership proof (snarkjs CLI output).
    g1_proof = json.load(open(os.path.join(out_dir, "g1tie_proof.json")))
    g1_public = json.load(open(os.path.join(out_dir, "g1tie_public.json")))
    world["membership"] = {
        "proofBytes": "0x" + "".join(
            int(x).to_bytes(32, "big").hex() for x in [
                g1_proof["pi_a"][0], g1_proof["pi_a"][1],
                g1_proof["pi_b"][0][0], g1_proof["pi_b"][0][1],
                g1_proof["pi_b"][1][0], g1_proof["pi_b"][1][1],
                g1_proof["pi_c"][0], g1_proof["pi_c"][1],
            ]),
        "public": g1_public,
    }

    # Note-binding proof (rapidsnark output; layout-matched circuit per flavor).
    if flavor in ("a1", "a2"):
        nb_proof = json.load(open(os.path.join(out_dir, "note_binding_proof.json")))
        world["noteBinding"] = {
            "proofBytes": "0x" + "".join(
                int(x).to_bytes(32, "big").hex() for x in [
                    nb_proof["pi_a"][0], nb_proof["pi_a"][1],
                    nb_proof["pi_b"][0][0], nb_proof["pi_b"][0][1],
                    nb_proof["pi_b"][1][0], nb_proof["pi_b"][1][1],
                    nb_proof["pi_c"][0], nb_proof["pi_c"][1],
                ]),
        }

    world["mint"] = {
        "proofBytes": mint["proofBytes"],
        "public": mint["public"],
    }
    world["spend"] = spend["spend"]

    # Prover wall times (gen_e2e_fixtures.sh records them around each prover).
    timings_path = os.path.join(out_dir, "timings.json")
    if os.path.exists(timings_path):
        world["timings"] = json.load(open(timings_path))

    out = os.path.join(VEC_DIR, f"{flavor}.json")
    with open(out, "w") as f:
        json.dump(world, f, indent=2)
    print(f"[assemble:{flavor}] -> {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["world", "assemble"])
    ap.add_argument("--flavor", required=True, choices=["a1", "a2", "b1"])
    args = ap.parse_args()
    if args.cmd == "world":
        build_world(args.flavor)
    else:
        assemble(args.flavor)


if __name__ == "__main__":
    main()
