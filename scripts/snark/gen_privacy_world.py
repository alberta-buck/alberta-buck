"""The privacy paper's fixture world: one chain, one identity tree, three Notes.

Where scripts/snark/gen_e2e_world.py builds one isolated world per flavour,
this builds the single world alberta-buck-privacy.org walks through:

    Bob (private) pays Carol (private), four ways:
      EOA   -- nothing to pre-build: the paper proves it live
      B1    -- Aspen Mutual Credit Union mints a batch of four bearer notes;
               Bob bought the third; Carol cashes it into her everyday account
      A1    -- Aspen Mutual mints a batch of four drafts; the third is payable
               to Carol; she deposits it into her everyday account
      A2    -- Bob writes his own private cheque to Carol; she deposits it
               into her savings account (a second account, same Identity)

One identity tree holds every association the gates prove, and one note tree
receives the three mints in order (leaves 0-3, 4-7, 8), so every spend proof
is against the root the chain actually holds after that mint.  The other
leaves in Aspen Mutual's batches are real notes for fictional customers.

Every draw a wallet operation makes is recorded ("draws"), so the executable
paper can replay the operation and rebuild the very note, delivery and
binding these Groth16 proofs were made over, rather than loading them.

Subcommands:
    world      emit build/snark/privacy/world.json + prover inputs
    assemble   merge the proofs -> alberta_buck/test/vectors/privacy/world.json

The driver is scripts/snark/gen_privacy_fixtures.sh.
"""

import argparse
import hashlib
import json
import os
import random
import sys

SCRIPT_DIR                      = os.path.dirname(os.path.abspath(__file__))
REPO                            = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, REPO)
sys.path.insert(0, SCRIPT_DIR)

from eth_account import Account

from gen_e2e_world import account, ct, pt, to_limbs, ct_words_mod_fr

from alberta_buck.sim.cast import ASPEN, BOB, CAROL, CHAINID, KYC, UNIT
from alberta_buck.wallet.bn254 import G1, ORDER, add, eq, mul, point_to_words, rand_scalar
from alberta_buck.wallet.elgamal import elgamal_encrypt
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.notes import (
    FLAVOR_A1, FLAVOR_B1, TAG_ID_HASH, NoteOpening, id_hash_b1, note_commitment, nullifier,
)
from alberta_buck.wallet.poseidon import F_R, poseidon
from alberta_buck.wallet.schnorr import batch_commitment, issuer_schnorr_sign
from alberta_buck.wallet.unilateral_a1 import mint_unilateral_a1
from alberta_buck.wallet.unilateral_a2 import mint_unilateral_a2
from alberta_buck.wallet.b1_binding import b1_bind_prove, b1_bind_verify
from alberta_buck.wallet.deposit_fold import deposit_fold_a1_witness, deposit_fold_a2_witness, deposit_fold_witness
from alberta_buck.wallet.delivery import deliver_a1, deliver_a2, open_a1, open_a2
from alberta_buck.wallet.nums import H_PEDERSEN
from alberta_buck.wallet.recvkey import prove_receiving_binding, receiving_key, verify_receiving_binding
from alberta_buck.wallet.salt import derive_salt
from alberta_buck.registry.merkle_service import rooted_registry
from alberta_buck.registry.tree import identity_leaf_salted, mailbox_leaf, receiving_leaf

SEED                            = 0xB0BCA201
OUT_DIR                         = os.path.join(REPO, "build", "snark", "privacy")
VEC_DIR                         = os.path.join(REPO, "alberta_buck", "test", "vectors", "privacy")
SUBTREE                         = "registry:kyc"

# Salt counters: one association per purpose, never sharing a salt.
RECEIVING, MAILBOX, NAMING      = 0, 1, 2

# Aspen Mutual's two batches: four notes each, the story's note third.
B1_BATCH                        = [20, 250, 100, 60]
A1_BATCH                        = [75, 120, 100, 40]
MINE                            = 2


class Tape:
    """An rng that records what it hands out, so an operation can be replayed."""

    def __init__(self, rng):
        self.rng = rng
        self.values = []

    def __call__(self):
        v = self.rng()
        self.values.append(v)
        return v


def eth_account(label: str):
    """A deterministic Ethereum key for one story account."""
    key = hashlib.sha256(f"alberta-buck/privacy-world/{label}".encode()).digest()
    return Account.from_key(key)


def contract_address(label: str) -> int:
    return int(hashlib.sha256(f"alberta-buck/privacy-world/{label}".encode()).hexdigest()[:40], 16)


def acct_json(owner, addr, acct, is_public, kind, eth_key=None):
    d = {
        "owner": owner, "kind": kind, "isPublic": is_public,
        "addr": f"0x{addr:040x}",
        "sk": str(acct["sk"]), "pk": pt(acct["pk"]),
        "r": str(acct["r_E"]), "E": ct(acct["E"]),
    }
    if eth_key is not None:
        d["ethKey"] = eth_key
    return d


def build_world():
    state                       = random.Random(SEED)
    rng                         = lambda: state.getrandbits(256)
    os.makedirs(OUT_DIR, exist_ok=True)

    # ---- People: core records, identity scalars, wallet seeds ----------------------------------
    people = {}
    for name, fields in (("bob", BOB), ("carol", CAROL), ("aspen", ASPEN)):
        canonical               = canonical_identity_data(fields)
        m                       = identity_scalar(canonical)
        people[name] = {"identity": canonical, "m": m, "M": mul(G1, m)}
    for name in ("bob", "carol"):
        p = people[name]
        p["seed"] = rand_scalar(rng)
        p["k"], p["pkRecv"] = receiving_key(p["seed"])
        p["salts"] = {purpose: derive_salt(p["seed"], KYC, counter)
                      for purpose, counter in (("receiving", RECEIVING), ("mailbox", MAILBOX),
                                               ("naming", NAMING))}
    bob, carol, aspen = people["bob"], people["carol"], people["aspen"]

    # ---- Accounts: Ethereum addresses and registered identity keys -----------------------------
    accts = {}
    for label, owner in (("bob", "bob"), ("carol", "carol"), ("carolSavings", "carol")):
        ea = eth_account(label)
        accts[label] = dict(account(people[owner]["m"], rng), owner=owner,
                            addr=int(ea.address, 16), ethKey=ea.key.hex(), kind="eoa",
                            isPublic=False)
    accts["aspen"] = dict(account(aspen["m"], rng), owner="aspen", addr=contract_address("aspen-notes"),
                          ethKey=None, kind="contract", isPublic=True)

    # ---- The identity tree: every association a gate proves ------------------------------------
    tree                        = rooted_registry(SUBTREE)
    leaves                      = []

    def enrol(owner, kind, leaf):
        tree.insert_leaf(leaf)
        leaves.append({"owner": owner, "kind": kind, "leaf": str(leaf)})

    enrol("bob", "receiving", receiving_leaf(bob["m"], bob["k"], bob["salts"]["receiving"]))
    enrol("bob", "naming", identity_leaf_salted(bob["M"], bob["salts"]["naming"]))
    enrol("carol", "receiving", receiving_leaf(carol["m"], carol["k"], carol["salts"]["receiving"]))
    enrol("carol", "mailbox", mailbox_leaf(carol["M"], carol["pkRecv"], carol["salts"]["mailbox"]))
    enrol("carol", "naming", identity_leaf_salted(carol["M"], carol["salts"]["naming"]))
    identity_root = tree.root()

    # What a payer checks before addressing Carol: her mailbox association.
    mbx = prove_receiving_binding(carol["M"], carol["pkRecv"], carol["salts"]["mailbox"], tree)
    assert verify_receiving_binding(carol["M"], mbx, identity_root)

    notes                       = {}
    all_cms                     = []

    # The batch signatures draw from their own stream, so the world reproduces exactly without
    # moving any draw the notes and proofs were built from.
    sign_state                  = random.Random(SEED + 1)
    sign_rng                    = lambda: sign_state.getrandbits(256)

    def schnorr(cms, issuer_acct):
        sig = issuer_schnorr_sign(issuer_acct["sk"], batch_commitment(cms), issuer_acct["addr"], CHAINID,
                                  rng=sign_rng)
        return {"e": str(sig.e), "s": str(sig.s), "R": pt(sig.R),
                "hBatch": str(batch_commitment(cms))}

    # ---- B1: Aspen Mutual's bearer notes -------------------------------------------------------
    b1_batch = []
    for face in B1_BATCH:
        rho                     = rand_scalar(rng)
        b1_batch.append(NoteOpening(FLAVOR_B1, face * UNIT, rho, id_hash_b1(aspen["m"]), 0))
    b1_cms = [note_commitment(o) for o in b1_batch]
    all_cms += b1_cms
    b1_note = b1_batch[MINE]

    # Carol cashes it: her depositor binding names her to Aspen Mutual only.
    tape                        = Tape(rng)
    b                           = rand_scalar(tape)
    binding, eDepForIss         = b1_bind_prove(carol["m"], accts["carol"]["sk"], accts["carol"]["E"],
                                                accts["aspen"]["pk"], account=accts["carol"]["addr"],
                                                chainid=CHAINID, b=b, rng=tape)
    assert b1_bind_verify(accts["carol"]["pk"], accts["carol"]["E"], accts["aspen"]["pk"],
                          eDepForIss, binding, account=accts["carol"]["addr"], chainid=CHAINID)
    assert eq(binding.P_dep, add(carol["M"], mul(H_PEDERSEN, b)))
    named_idx                   = tree.index_of_leaf(identity_leaf_salted(carol["M"], carol["salts"]["naming"]))
    path                        = tree.path(named_idx)
    Mx, My                      = point_to_words(carol["M"])
    PIx, PIy                    = point_to_words(binding.P_dep)
    b1_membership_input         = {
                "identityRoot": str(identity_root),
                "PI_x": [str(v) for v in to_limbs(PIx)], "PI_y": [str(v) for v in to_limbs(PIy)],
                "Mx": [str(v) for v in to_limbs(Mx)], "My": [str(v) for v in to_limbs(My)],
                "b": [str(v) for v in to_limbs(b)],
                "salt": str(carol["salts"]["naming"]),
                "pathElements": [str(x) for x in path.siblings],
                "pathIndices": [str(x) for x in path.index_bits],
            }
    notes["b1"] = {
        "flavor": FLAVOR_B1, "issuer": "aspen", "payout": "carol",
        "firstLeaf": 0, "index": MINE, "leafIndex": MINE,
        "batch": [opening_json(o) for o in b1_batch], "cms": [str(c) for c in b1_cms],
        "issuerSchnorr": schnorr(b1_cms, accts["aspen"]),
        "nullifier": str(nullifier(b1_note.rho, b1_note.id_hash)),
        "depositor": {
            "eDepForIss": ct(eDepForIss),
            "db": {
                "e": str(binding.e), "s_m": str(binding.s_m), "s_s": str(binding.s_s),
                "s_r": str(binding.s_r), "s_b": str(binding.s_b),
                "A2": pt(binding.A2), "A4": pt(binding.A4), "B1": pt(binding.B1),
                "B2": pt(binding.B2), "A_p": pt(binding.A_p), "P_dep": pt(binding.P_dep),
            },
            "b": str(b),
        },
        "draws": {"depositorBinding": [str(v) for v in tape.values]},
    }
    write(os.path.join(OUT_DIR, "b1_membership_input.json"), b1_membership_input)

    # ---- A1: Aspen Mutual's drafts, one payable to Carol ---------------------------------------
    a1_batch, a1_minted = [], None
    for i, face in enumerate(A1_BATCH):
        rho                     = rand_scalar(rng)
        if i == MINE:
            tape                = Tape(rng)
            minted              = mint_unilateral_a1(carol["M"], carol["pkRecv"], v=face * UNIT, rho=rho,
                                                     m_issuer=aspen["m"], rng=tape)
            a1_minted, a1_tape  = minted, tape
        else:
            # Another customer's payee: a fictional Identity and mailbox.
            M_x, pk_x           = mul(G1, rand_scalar(rng)), mul(G1, rand_scalar(rng))
            minted              = mint_unilateral_a1(M_x, pk_x, v=face * UNIT, rho=rho, m_issuer=aspen["m"],
                                                     rng=rng)
        a1_batch.append(minted.opening)
    a1_cms = [note_commitment(o) for o in a1_batch]
    all_cms += a1_cms
    a1_delivery                 = deliver_a1(a1_minted, carol["pkRecv"])
    a1_fold, a1_eEnc, a1_s      = fold_input("a1", a1_delivery, carol, accts["carol"], tree, aspen["m"],
                                             rng)
    notes["a1"] = {
        "flavor": FLAVOR_A1, "issuer": "aspen", "payout": "carol",
        "firstLeaf": len(b1_cms), "index": MINE, "leafIndex": len(b1_cms) + MINE,
        "batch": [opening_json(o) for o in a1_batch], "cms": [str(c) for c in a1_cms],
        "issuerSchnorr": schnorr(a1_cms, accts["aspen"]),
        "nullifier": str(nullifier(a1_minted.opening.rho, a1_minted.opening.id_hash)),
        "delivery": a1_delivery,
        "issuerSecrets": {"rNote": str(a1_minted.r_note), "rPrime": str(a1_minted.r_prime)},
        "eEnc": ct(a1_eEnc), "s": str(a1_s),
        "draws": {"mint": [str(v) for v in a1_tape.values]},
    }
    write(os.path.join(OUT_DIR, "a1_fold_input.json"), a1_fold)

    # ---- A2: Bob's private cheque --------------------------------------------------------------
    tape                        = Tape(rng)
    rho                         = rand_scalar(rng)
    a2_minted                   = mint_unilateral_a2(accts["bob"]["sk"], accts["bob"]["E"], carol["pkRecv"],
                                                     v=100 * UNIT, rho=rho, issuer=accts["bob"]["addr"],
                                                     chainid=CHAINID, salt_iss=bob["salts"]["naming"], rng=tape)
    a2_cms = [note_commitment(a2_minted.opening)]
    all_cms += a2_cms
    a2_delivery                 = deliver_a2(a2_minted, carol["pkRecv"])
    a2_fold, a2_eEnc, a2_s      = fold_input("a2", a2_delivery, carol, accts["carolSavings"], tree,
                                             None, rng)
    en, ei                      = ct_words_mod_fr(a2_minted.eNote), ct_words_mod_fr(a2_minted.eIss)
    tw                          = [w % F_R for w in point_to_words(a2_minted.binding.T)]
    assert poseidon([TAG_ID_HASH] + en + ei + tw) == a2_minted.opening.id_hash, "idHash layout mismatch"
    bd = a2_minted.binding
    notes["a2"] = {
        "flavor": a2_minted.opening.flavor, "issuer": "bob", "payout": "carolSavings",
        "firstLeaf": len(b1_cms) + len(a1_cms), "index": 0,
        "leafIndex": len(b1_cms) + len(a1_cms),
        "batch": [opening_json(a2_minted.opening)], "cms": [str(c) for c in a2_cms],
        "nullifier": str(nullifier(a2_minted.opening.rho, a2_minted.opening.id_hash)),
        "delivery": a2_delivery,
        "a2Binding": {
            "eIss": ct(a2_minted.eIss),
            "proof": {
                "e": str(bd.e), "s_r": str(bd.s_r), "s_b": str(bd.s_b), "s_s": str(bd.s_s),
                "s_g": str(bd.s_g), "A1": pt(bd.A1), "A2": pt(bd.A2), "A3": pt(bd.A3),
                "A4": pt(bd.A4), "A5": pt(bd.A5), "Q": pt(bd.Q), "U": pt(bd.U), "T": pt(bd.T),
            },
        },
        "issuerSecrets": {"rNote": str(a2_minted.r_note), "rPrime": str(a2_minted.r_prime),
                          "gamma": str(a2_minted.gamma)},
        "eEnc": ct(a2_eEnc), "s": str(a2_s),
        "draws": {"mint": [str(v) for v in tape.values]},
        "mintWords": {"eNote": [str(w) for w in en], "eIss": [str(w) for w in ei],
                      "T": [str(w) for w in tw]},
    }
    write(os.path.join(OUT_DIR, "a2_fold_input.json"), a2_fold)

    # ---- Prover arguments ----------------------------------------------------------------------
    def pins(batch):
        return [f"--pin={i}:{o.flavor},{o.v},{o.rho},{o.id_hash}" for i, o in enumerate(batch)]

    state = lambda name: os.path.join("build", "snark", "mint_batch_n4", "fixtures",
                                      f"{name}-state.json")
    notes["b1"]["mintArgs"] = ["--name=privacy_b1", f"--n={len(b1_batch)}", *pins(b1_batch)]
    notes["a1"]["mintArgs"] = ["--name=privacy_a1", f"--n={len(a1_batch)}", *pins(a1_batch),
                               f"--initial-state={state('privacy_b1')}"]
    notes["a2"]["mintArgs"] = [
        "--name=privacy_a2", "--n=1", f"--live-leaves={a2_minted.opening.v}",
        f"--rho=0:{a2_minted.opening.rho}",
        "--enote=0:" + ",".join(str(w) for w in en),
        "--eiss=0:" + ",".join(str(w) for w in ei),
        "--t=0:" + ",".join(str(w) for w in tw),
        f"--initial-state={state('privacy_a1')}",
    ]
    # Each spend proves membership in the tree as it stands after its own mint.
    for flavor, upto in (("b1", len(b1_cms)), ("a1", len(b1_cms) + len(a1_cms)),
                         ("a2", len(all_cms))):
        write(os.path.join(OUT_DIR, f"{flavor}_leaves.json"), [str(c) for c in all_cms[:upto]])
        notes[flavor]["payoutAddr"] = f"0x{accts[notes[flavor]['payout']]['addr']:040x}"

    world = {
        "chainid": CHAINID, "kyc": KYC, "subtree": SUBTREE, "seed": hex(SEED), "unit": UNIT,
        "people": {
            name: {
                "identity": p["identity"], "m": str(p["m"]), "M": pt(p["M"]),
                **({"seed": str(p["seed"]), "kRecv": str(p["k"]), "pkRecv": pt(p["pkRecv"]),
                    "salts": {k: str(v) for k, v in p["salts"].items()}} if "seed" in p else {}),
            }
            for name, p in people.items()
        },
        "accounts": {label: acct_json(a["owner"], a["addr"], a, a["isPublic"], a["kind"],
                                      a["ethKey"])
                     for label, a in accts.items()},
        "identityTree": {"leaves": leaves, "root": str(identity_root)},
        "mailboxBinding": {
            "salt": str(mbx.salt), "leaf": str(mbx.path.leaf),
            "siblings": [str(x) for x in mbx.path.siblings],
            "indexBits": list(mbx.path.index_bits), "root": str(mbx.path.root),
        },
        "notes": notes,
    }
    write(os.path.join(OUT_DIR, "world.json"), world)
    print(f"[privacy] identityRoot={hex(identity_root)[:18]}...  note leaves={len(all_cms)}")
    for flavor, n in notes.items():
        print(f"[privacy] {flavor}: leaf {n['leafIndex']}  mint {' '.join(n['mintArgs'][:2])}")


def opening_json(o: NoteOpening):
    return {"flavor": o.flavor, "v": str(o.v), "rho": str(o.rho), "idHash": str(o.id_hash),
            "cm": str(note_commitment(o))}


def fold_input(flavor, delivery, carol, dep, tree, m_issuer, rng):
    """Carol's folded-gate witness, built only from what the delivery carries and her own
    secrets -- never from the minter's memory.

    Her spend publishes a FRESH encryption eEnc under her own receiving key: of her own
    Identity for A1 (any randomness t she likes), and for A2 a re-randomisation of the issuer
    ciphertext she was delivered, so t = r' + s with r' unwrapped from the delivery."""
    s = rand_scalar(rng)
    if flavor == "a1":
        opened                  = open_a1(delivery, carol["k"], m_issuer)
        M_named, t_total        = carol["M"], s
    else:
        opened                  = open_a2(delivery, carol["k"])
        M_named, t_total        = opened.M_I, (opened.r_prime + s) % ORDER
    eEnc                        = elgamal_encrypt(M_named, carol["pkRecv"], t_total)
    w                           = deposit_fold_witness(m_rec=carol["m"], k=carol["k"], sk_dep=dep["sk"],
                                                       salt=carol["salts"]["receiving"], E_dep=dep["E"], note_ct=eEnc,
                                                       tree=tree)
    if flavor == "a1":
        fold = deposit_fold_a1_witness(
            witness=w, rho=opened.opening.rho, id_hash=opened.opening.id_hash,
            e_note=opened.eNote, v=opened.opening.v, m_issuer=m_issuer,
            r_note=opened.r_note, t=t_total,
            r_E=dep["r_E"], e_dep=dep["E"], pk_dep=dep["pk"], e_enc=eEnc,
            identity_root=tree.root())
    else:
        iss_path                = tree.path(tree.index_of_leaf(identity_leaf_salted(opened.M_I, opened.salt_iss)))
        fold                    = deposit_fold_a2_witness(
                               witness=w, rho=opened.opening.rho, id_hash=opened.opening.id_hash,
                               e_note=opened.eNote, e_iss=opened.eIss, r_prime=opened.r_prime, t=t_total,
                               r_E=dep["r_E"], e_dep=dep["E"], pk_dep=dep["pk"], e_enc=eEnc,
                               salt_iss=opened.salt_iss, iss_path=iss_path, T=opened.T, gamma=opened.gamma,
                               identity_root=tree.root())
    return fold, eEnc, s


def write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def proof_bytes(pr):
    """abi-packed Groth16 triple, pi_b pre-swapped to EIP-197 order (as gen_e2e_world)."""
    return "0x" + "".join(
        int(x).to_bytes(32, "big").hex() for x in [
            pr["pi_a"][0], pr["pi_a"][1],
            pr["pi_b"][0][1], pr["pi_b"][0][0],
            pr["pi_b"][1][1], pr["pi_b"][1][0],
            pr["pi_c"][0], pr["pi_c"][1],
        ])


def assemble():
    world = json.load(open(os.path.join(OUT_DIR, "world.json")))
    os.makedirs(VEC_DIR, exist_ok=True)
    timings                     = {}
    tpath                       = os.path.join(OUT_DIR, "timings.json")
    if os.path.exists(tpath):
        timings = json.load(open(tpath))
    for flavor, n in world["notes"].items():
        nd                      = "mint_batch_a2_n1" if flavor == "a2" else f"mint_batch_n{len(n['batch'])}"
        mint                    = json.load(open(os.path.join(REPO, "build", "snark", nd, "fixtures",
                                                              f"privacy_{flavor}.json")))
        assert mint["public"]["cm"] == n["cms"], f"{flavor}: prover and wallet disagree on cms"
        n["mint"] = {"proofBytes": mint["proofBytes"], "public": mint["public"]}
        spend = json.load(open(os.path.join(REPO, "build", "snark", "spend", "fixtures",
                                            f"privacy_{flavor}.json")))
        assert spend["spend"]["public"]["nullifier"] == n["nullifier"], f"{flavor}: nullifier"
        n["spend"] = spend["spend"]
        if flavor == "b1":
            pr                  = json.load(open(os.path.join(OUT_DIR, "b1_membership_proof.json")))
            pub                 = json.load(open(os.path.join(OUT_DIR, "b1_membership_public.json")))
            n["gate"] = {"kind": "b1Membership", "proofBytes": proof_bytes(pr), "public": pub}
        else:
            pr                  = json.load(open(os.path.join(OUT_DIR, f"{flavor}_fold_proof.json")))
            pub                 = json.load(open(os.path.join(OUT_DIR, f"{flavor}_fold_public.json")))
            n["gate"] = {"kind": "depositFold", "proofBytes": proof_bytes(pr), "public": pub}
        if flavor in timings:
            n["timings"] = timings[flavor]
    out = os.path.join(VEC_DIR, "world.json")
    write(out, world)
    print(f"[assemble] -> {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["world", "assemble"])
    args = ap.parse_args()
    build_world() if args.cmd == "world" else assemble()


if __name__ == "__main__":
    main()
