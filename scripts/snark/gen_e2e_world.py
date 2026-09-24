"""End-to-end Notes fixture world builder (all three flavors).

Builds ONE mutually-consistent set of artifacts per flavor so that
test/NotesE2E.t.sol can drive the full real-verifier lifecycle:

    identity world -> registry binds, and the aggregator root the world posts
    wallet note    -> pinned-opening mint proof (mint_batch / mint_batch_a2)
    spend proof    -> against the replayed note tree (prove_spend.js)
    deposit sigma  -> pinned to (depositor address, chainid=1)
    g1tie proof    -> membership of the sigma's committed P point
    note binding   -> (A1/A2) the note<->eEnc re-encryption tie

Subcommands:
    world    --flavor {a1,a2,b1}   emit world.json + prover inputs
    assemble --flavor {a1,a2,b1}   merge all proofs -> alberta_buck/test/vectors/e2e/<flavor>.json

The driver is scripts/snark/gen_e2e_fixtures.sh.

The note tie is a RELATION of the folded gate, not a proof beside it: A2 opens
idHash = Poseidon10(eNote, eIss, T) inside circuits/deposit_fold_a2.circom, and
A1's idHash commits (eNote, m_issuer, sigma), so its tie runs through the
note's own value ciphertext with the face public.  B1 is bearer -- no tie.
"""

import argparse
import json
import os
import random
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, eq, mul, point_to_words, rand_scalar, words_to_point,
)
from alberta_buck.wallet.elgamal import ElGamalCiphertext, elgamal_encrypt
from alberta_buck.wallet.poseidon import F_R
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.notes import (
    FLAVOR_A1, FLAVOR_B1, NoteOpening, note_commitment, id_hash_b1, nullifier_b,
)
from alberta_buck.wallet.schnorr import batch_commitment, issuer_schnorr_sign
from alberta_buck.wallet.unilateral_a1 import mint_unilateral_a1
from alberta_buck.wallet.unilateral_a2 import mint_unilateral_a2
from alberta_buck.registry.merkle_service import rooted_registry
from alberta_buck.wallet.b1_binding import b1_bind_prove, b1_bind_verify
from alberta_buck.wallet.deposit_fold import (
    deposit_fold_a1_witness, deposit_fold_a2_witness, deposit_fold_witness,
)
from alberta_buck.wallet.nums import H_PEDERSEN
from alberta_buck.wallet.delivery import deliver_a1, deliver_a2, open_a1, open_a2
from alberta_buck.wallet.recvkey import (
    receiving_key, prove_receiving_binding, verify_receiving_binding,
)
from alberta_buck.wallet.salt import derive_salt
from alberta_buck.wallet.vectors import ALICE_FIELDS, BOB_FIELDS
from alberta_buck.registry.tree import (
    identity_leaf, identity_leaf_salted, receiving_leaf,
)

KYC = "kyc:ca-ab-2026"

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


def ct_of(d):
    """Parse a serialized ciphertext back.  The fold witness is built from the
    PAYLOAD, not from the generator's memory, so a payload that cannot be spent
    fails here instead of shipping as a fixture."""
    def p(o):
        return words_to_point(int(o["x"]), int(o["y"]))
    return ElGamalCiphertext(p(d["R"]), p(d["C"]))


def account(m, rng):
    """One registered Fountain account bound to identity scalar m."""
    sk = rand_scalar(rng)
    pk = mul(G1, sk)
    M = mul(G1, m)
    r_E = rand_scalar(rng)
    E = elgamal_encrypt(M, pk, r_E)
    # r_E is kept because the folded gate PROVES the credential relation
    # E.C = (m + sk*r_E)*G rather than accepting a sigma over it, and that
    # needs the registration randomness as a witness.
    return dict(m=m, M=M, sk=sk, pk=pk, E=E, r_E=r_E)


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

    # Each party holds a MAILBOX separate from its Identity.  An addressed note
    # is keyed to the receiving key; authority stays with the Identity.  The
    # two are different secrets, which is why the spend gate is folded rather
    # than a sigma beside a membership proof (doc/review/notes-receiving-key.org).
    seed_ctr = rand_scalar(rng)
    k_ctr, pk_ctr = receiving_key(seed_ctr)
    salt_ctr = derive_salt(seed_ctr, KYC)

    seed_iss = rand_scalar(rng)
    k_iss, pk_iss_recv = receiving_key(seed_iss)
    # A2's issuer keeps a SECOND association -- an ordinary salted identity
    # leaf -- whose salt it ships so the recipient can prove at spend that the
    # Identity it decrypted is registered.  Distinct salts, so the one that
    # names it says nothing about the one that reads its mail.
    salt_iss_named = derive_salt(seed_iss, KYC, 1)

    # The addressed recipient keeps a THIRD association: the mailbox leaf, over
    # the two POINTS, which is the payer's and the receipt verifier's view of
    # the same fact the gate's scalar leaf proves.  Its own salt again, so the
    # one it hands a payer does not locate the one it spends under.
    salt_mbx = derive_salt(seed_ctr, KYC, 1)

    # Leaves in bind order, and which leaf each party contributes depends
    # on what the flavour's gate must prove about it:
    #   A1  issuer: unused by the gate    depositor: its receiving leaf
    #   A2  issuer: its NAMED leaf        depositor: its receiving leaf
    #   B1  issuer: unused by the gate    depositor: its salted identity leaf
    # One identity registry's private subtree, under the aggregator: every path
    # runs 32 levels, subtree then aggregator, and the world posts only the
    # aggregator root, as a deployment's aggregator does.
    tree = rooted_registry()
    if flavor == "a2":
        iss_leaf = identity_leaf_salted(issuer_acct["M"], salt_iss_named)
    else:
        iss_leaf = identity_leaf_salted(issuer_acct["M"], derive_salt(seed_iss, KYC, 2))
    dep_leaf = (identity_leaf_salted(M_ctr, salt_ctr) if flavor == "b1"
                else receiving_leaf(m_ctr, k_ctr, salt_ctr))
    tree.insert_leaf(iss_leaf)
    tree.insert_leaf(dep_leaf)
    if flavor != "b1":
        tree.insert_mailbox(M_ctr, pk_ctr, salt_mbx)

    binds = [
        {
            "addr": f"0x{ISSUER:040x}",
            "pk": pt(issuer_acct["pk"]), "E": ct(issuer_acct["E"]),
            "isPublic": flavor != "a2",
        },
        {
            "addr": f"0x{DEPOSIT:040x}",
            "pk": pt(dep_acct["pk"]), "E": ct(dep_acct["E"]),
            "isPublic": False,
        },
    ]

    # ---- The wallet note ----------------------------------------------------
    rho = rand_scalar(rng)
    if flavor == "a2":
        note = mint_unilateral_a2(issuer_acct["sk"], issuer_acct["E"], pk_ctr,
                                  v=FACE, rho=rho, issuer=ISSUER,
                                  chainid=CHAINID, salt_iss=salt_iss_named,
                                  rng=rng)
        opening = note.opening
        eCommitted = note.eIss                   # committed in idHash
        M_named = note.M_I                       # the membership target (issuer)
        r_committed = note.r_prime
        eNote = note.eNote
        # The delivery: what the minter hands the channel.  Every secret
        # scalar in it -- rho, the face, r', gamma, the issuer's naming salt --
        # is wrapped to the mailbox key, so the channel learns neither the issuer
        # nor when the note is spent (wallet/delivery.py).
        note_payload = deliver_a2(note, pk_ctr)
    elif flavor == "a1":
        # The in-payload (sigma_R, sigma_s) is the issuer's identity-binding
        # signature over the delivery payload (synthetic domain here, as in
        # test_unilateral_a1).
        k = rand_scalar(rng)
        sigma_R = mul(G1, k)
        sigma_s = (k + rand_scalar(rng) * rand_scalar(rng)) % ORDER
        note = mint_unilateral_a1(M_ctr, pk_ctr, v=FACE, rho=rho,
                                  m_issuer=m_iss, sigma_R=sigma_R,
                                  sigma_s=sigma_s, rng=rng)
        opening = note.opening
        eCommitted = note.eRec                   # the sigma's ciphertext (NOT in idHash)
        M_named = M_ctr                          # membership target (recipient)
        r_committed = note.r_prime
        eNote = note.eNote
        # A1's delivery carries eNote's randomness, because the fold pins eNote
        # against the public face, and no issuer salt: relation (2) already
        # proves the decrypted Identity is the payout account's, so A1 asserts
        # nothing about a third party (doc/review/notes-receiving-key.org 4.4).
        note_payload = deliver_a1(note, pk_ctr, sigma_R, sigma_s)
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

    # ---- Deposit-side proof ---------------------------------------------
    #
    # A1 and A2 spend through the FOLDED gate: one proof over one witness,
    # replacing the coupling sigma, the P-bound membership proof and the
    # note<->eEnc tie.  Those three shared the public point P_I, and inferring
    # an equality across proofs that merely share a point is what review
    # finding 5 caught -- here it would be an equality between two DIFFERENT
    # secrets, the Identity and the receiving key, which a payload thief
    # satisfies with one of each.  There is no P_I in this world at all.
    #
    # B1 keeps its sigma, because its two facts rest on ONE secret and the
    # shared nonce is therefore a genuine tie.  What it needs instead is an
    # honest hiding generator, so its P_dep is built on H_PEDERSEN and its
    # membership proof is the repaired circuit.
    b = rand_scalar(rng)

    if flavor in ("a1", "a2"):
        s_rand = rand_scalar(rng)                 # eEnc re-randomization
        t_total = (note.r_prime + s_rand) % ORDER
        eEnc = elgamal_encrypt(M_named, pk_ctr, t_total)

        w = deposit_fold_witness(
            m_rec=m_ctr, k=k_ctr, sk_dep=dep_acct["sk"], salt=salt_ctr,
            E_dep=dep_acct["E"], note_ct=eEnc, tree=tree,
        )
        # Everything below comes from the payload the channel carried, the
        # opening, and the recipient's own secrets (k, m_rec, sk_dep, its salt).
        # Nothing comes from the minter's memory.
        # Opened with k: the recipient is the only party that can, which is
        # what the wrap is for.  The opened note must recompute to the very
        # commitment the minter folded into the tree.
        if flavor == "a1":
            opened = open_a1(note_payload, k_ctr, m_iss)
            assert opened.cm == note_commitment(opening), "delivery opens to another note"
            fold_input = deposit_fold_a1_witness(
                witness=w, rho=opened.opening.rho, id_hash=opened.opening.id_hash,
                e_note=opened.eNote, v=opened.opening.v, m_issuer=m_iss,
                sigma_R=opened.sigma_R, sigma_s=opened.sigma_s,
                r_note=opened.r_note, t=t_total,
                r_E=dep_acct["r_E"], e_dep=dep_acct["E"],
                pk_dep=dep_acct["pk"], e_enc=eEnc,
                identity_root=tree.root(),
            )
        else:
            opened = open_a2(note_payload, k_ctr)
            assert opened.cm == note_commitment(opening), "delivery opens to another note"
            iss_path = tree.path(tree.leaves.index(
                identity_leaf_salted(opened.M_I, opened.salt_iss)))
            fold_input = deposit_fold_a2_witness(
                witness=w, rho=opened.opening.rho, id_hash=opened.opening.id_hash,
                e_note=opened.eNote, e_iss=opened.eIss, r_prime=opened.r_prime,
                t=t_total, r_E=dep_acct["r_E"], e_dep=dep_acct["E"],
                pk_dep=dep_acct["pk"], e_enc=eEnc,
                salt_iss=opened.salt_iss, iss_path=iss_path,
                T=opened.T, gamma=opened.gamma, identity_root=tree.root(),
            )
        with open(os.path.join(out_dir, "fold_input.json"), "w") as f:
            json.dump(fold_input, f, indent=2)

        sigma_json = {"kind": "depositFold", "eEnc": ct(eEnc)}
        membership_input = None
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
        # P_dep opens as M_dep + b*H_PEDERSEN, and the repaired membership
        # circuit PROVES the blind rather than witnessing its point.
        assert eq(binding.P_dep, add(M_ctr, mul(H_PEDERSEN, b)))
        Mx, My = point_to_words(M_ctr)
        PIx, PIy = point_to_words(binding.P_dep)
        proof_path = tree.path(1)                 # the depositor's leaf
        membership_input = {
            "identityRoot": str(tree.root()),
            "PI_x": [str(v) for v in to_limbs(PIx)],
            "PI_y": [str(v) for v in to_limbs(PIy)],
            "Mx": [str(v) for v in to_limbs(Mx)],
            "My": [str(v) for v in to_limbs(My)],
            "b": [str(v) for v in to_limbs(b)],
            "salt": str(salt_ctr),
            "pathElements": [str(x) for x in proof_path.siblings],
            "pathIndices": [str(x) for x in proof_path.index_bits],
        }
        with open(os.path.join(out_dir, "b1_membership_input.json"), "w") as f:
            json.dump(membership_input, f, indent=2)

    # ---- mint prover pin args -------------------------------------------------
    if flavor == "a2":
        en = ct_words_mod_fr(eNote)
        ei = ct_words_mod_fr(eCommitted)
        tw = [w % F_R for w in point_to_words(note.binding.T)]
        mint_args = [
            "--name=e2e_a2", "--n=1", f"--live-leaves={FACE}",
            f"--rho=0:{opening.rho}",
            "--enote=0:" + ",".join(str(w) for w in en),
            "--eiss=0:" + ",".join(str(w) for w in ei),
            "--t=0:" + ",".join(str(w) for w in tw),
        ]
        # The wallet idHash must equal Poseidon10 over these words.
        from alberta_buck.wallet.poseidon import poseidon
        assert poseidon(en + ei + tw) == opening.id_hash, "idHash layout mismatch"
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
        # the AB-RCPT/2 receipt layer can be exercised over this exact
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
                # The mailbox: an independent secret, derived from the wallet
                # seed and never from the Identity, with the salt of the leaf
                # that binds the two.  Present so a test can show the shipped
                # payload SUFFICES -- that the delivery plus these three
                # secrets rebuild the gate.  For B1 the pair is unused: a
                # bearer note is addressed to nobody.
                "kRecv": str(k_ctr), "pkRecv": pt(pk_ctr),
                "salt": str(salt_ctr), "saltMailbox": str(salt_mbx),
            },
        },
        # The Identity-M note payload (the idHash preimage material) that
        # travels off chain with the note -- exactly what an AB-RCPT/2
        # receipt's `note` record conveys.  (B1's eDepForIss is spend-side
        # material and lives in sigma.eDepForIss.)
        "notePayload": note_payload,
    }
    if flavor in ("a1", "a2"):
        # What the MINTER retains.  It never travels: an issuer-side receipt
        # discloses it deliberately, to a reader who is being told both parties
        # anyway, and a payload that carried it would tell the channel.
        world["issuerSecrets"] = {
            "rNote": str(note.r_note),
            "rPrime": str(note.r_prime),
        }
        if flavor == "a2":
            # The binding's blind, which opens T: the recipient unwraps its own
            # copy from the delivery, and the issuer keeps this one.
            world["issuerSecrets"]["gamma"] = str(note.gamma)
    if flavor != "b1":
        # The payer's evidence, as a receipt carries it: a hash and a path over
        # the two points, disclosing no secret.  A payer checks this BEFORE
        # paying, which is why it cannot be the scalar leaf the spend proves.
        mbx = prove_receiving_binding(M_ctr, pk_ctr, salt_mbx, tree)
        world["mailboxBinding"] = {
            "salt": str(mbx.salt), "leaf": str(mbx.path.leaf),
            "siblings": [str(x) for x in mbx.path.siblings],
            "indexBits": list(mbx.path.index_bits),
            "root": str(mbx.path.root),
        }
        assert verify_receiving_binding(M_ctr, mbx, tree.root())
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
        # Seeded apart from the world's stream, so a regenerated fixture reproduces exactly.
        sign_state = random.Random(SEEDS[flavor] + 1)
        sig = issuer_schnorr_sign(int(world["parties"]["issuer"]["sk"]), h_batch,
                                  int(world["issuer"], 16), CHAINID,
                                  rng=lambda: sign_state.getrandbits(256))
        world["issuerSchnorr"] = {
            "e": str(sig.e), "s": str(sig.s), "R": pt(sig.R),
            "hBatch": str(h_batch),
        }

    # Spend fixture (from prove_spend.js).
    spend = json.load(open(os.path.join(out_dir, "spend.json")))
    assert int(spend["spend"]["public"]["nullifier"]) == int(world["opening"]["nullifier"]), \
        "spend/world nullifier mismatch"

    def _proof_bytes(pr):
        """abi-packed Groth16 triple, pi_b pre-swapped to EIP-197 order.

        The on-chain verifiers are stock snarkjs exports, which expect each
        pi_b pair in the opposite order to the one snarkjs writes -- the swap
        its own `zkey export soliditycalldata` performs.  Doing it here keeps
        the committed verifiers byte-for-byte as exported.
        """
        return "0x" + "".join(
            int(x).to_bytes(32, "big").hex() for x in [
                pr["pi_a"][0], pr["pi_a"][1],
                pr["pi_b"][0][1], pr["pi_b"][0][0],
                pr["pi_b"][1][1], pr["pi_b"][1][0],
                pr["pi_c"][0], pr["pi_c"][1],
            ])

    if flavor in ("a1", "a2"):
        # ONE proof carrying every relation.  There is no membership proof and
        # no note-binding proof beside it, because the fold subsumed both --
        # three checks sharing the public point P_I inferred an equality
        # between two different secrets, and one witness states it instead.
        fold_proof = json.load(open(os.path.join(out_dir, "fold_proof.json")))
        fold_public = json.load(open(os.path.join(out_dir, "fold_public.json")))
        world["depositFold"] = {
            "proofBytes": _proof_bytes(fold_proof),
            "public": fold_public,
        }
    else:
        # B1 keeps its sigma -- its two facts rest on ONE secret, so the shared
        # nonce is a genuine tie -- and pairs it with the REPAIRED membership
        # circuit, whose blind is proven rather than witnessed and whose
        # generator has no known logarithm.
        b1_proof = json.load(open(os.path.join(out_dir, "b1_membership_proof.json")))
        b1_public = json.load(open(os.path.join(out_dir, "b1_membership_public.json")))
        world["membership"] = {
            "proofBytes": _proof_bytes(b1_proof),
            "public": b1_public,
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
