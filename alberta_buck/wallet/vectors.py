"""Canonical JSON test-vector emission.

Produces a single JSON file containing all the data the Solidity tests need to
exercise the IdentityRegistry verifier paths against the Python reference:

* PS keypair (issuer)
* Identity records (Alice, Bob): canonical_data, m, ElGamal keypair, ciphertext
* PS signatures (raw) and the published A' presentations (A, B)
* Registration NIZK proofs (with negative variants the Solidity tests should reject)
* Chaum-Pedersen re-encryption proof (Alice -> Bob)

All scalars are 0x-prefixed 64-hex-char uint256s.  G1 points are
``{"x": "0x...", "y": "0x..."}``; G2 points use ``{"x": [c0, c1], "y": [c0, c1]}``
to match BN254.sol's struct layout.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any, Dict

from alberta_buck.wallet.bn254 import (
    G1, ORDER, mul, point_to_words, scalar_to_hex, rand_scalar,
)
from alberta_buck.wallet.poseidon import F_R
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.ps import ps_keygen, ps_sign, ps_present
from alberta_buck.wallet.elgamal import identity_keygen, elgamal_encrypt
from alberta_buck.wallet.nizk import registration_prove, RegistrationProof
from alberta_buck.wallet.chaum_pedersen import chaum_pedersen_prove
from alberta_buck.wallet.notes import (
    FLAVOR_A1, FLAVOR_A2, FLAVOR_B1, NoteOpening, note_commitment,
    nullifier_b, id_hash_a1, id_hash_a2, id_hash_b1,
)
from alberta_buck.wallet.schnorr import issuer_schnorr_sign, batch_commitment
from alberta_buck.wallet.verifiable_decrypt import verifiable_decrypt_prove
from alberta_buck.wallet.issuer_reenc import issuer_reenc_prove
from alberta_buck.wallet.recvkey import (
    receiving_key, prove_receiving_binding,
)
from alberta_buck.wallet.salt import derive_salt
from alberta_buck.registry.tree import IdentityMerkleTree
from alberta_buck.wallet.envelope import (
    serialize_core, envelope_text, receipt_id,
)
from alberta_buck.wallet.build_receipt import (
    build_eoa_pub, build_eoa_priv,
    build_note_b1, build_note_a1, build_note_a2,
)


def _u256(v: int) -> str:
    """Full-width uint256 hex, NO reduction mod ORDER.

    For raw keccak words (hBatch): the Schnorr transcript signs the full
    bytes32 exactly as IdentityRegistry._fsIssuerSchnorr packs it on-chain,
    so the fixture must store what the chain computes -- scalar_to_hex's
    `% ORDER` would silently corrupt any value >= ORDER (~81% of digests).
    """
    return f"0x{v:064x}"


def _g1(P) -> Dict[str, str]:
    x, y = point_to_words(P)
    return {"x": scalar_to_hex(x), "y": scalar_to_hex(y)}


def _g2(P) -> Dict[str, Any]:
    x_coeffs = P[0].coeffs
    y_coeffs = P[1].coeffs
    return {
        "x": [scalar_to_hex(int(x_coeffs[0])), scalar_to_hex(int(x_coeffs[1]))],
        "y": [scalar_to_hex(int(y_coeffs[0])), scalar_to_hex(int(y_coeffs[1]))],
    }


def _seeded_rng(seed: int):
    rnd = random.Random(seed)
    return lambda: rnd.getrandbits(256)


def _fork_rng(seed: int):
    """A second seeded stream for the draws A' added (the presentation
    blinding b and its nonce b_tilde).  The main stream's draw POSITIONS are
    pinned by committed SNARK fixtures (the a2b section feeds
    test/NotesA2Tie.t.sol), so new draws must not be inserted into it."""
    rnd = random.Random((seed << 8) ^ 0xA9)
    return lambda: rnd.getrandbits(256)


def _replay(vals):
    it = iter(vals)
    return lambda: next(it)


ALICE_FIELDS = {
    "given_name":    "Alice",
    "family_name":   "Johnson",
    "jurisdiction":  "Alberta, Canada",
    "id_type":       "Alberta Identity Card",
    "id_number":     "AIC-2026-4839201",
    "date_of_birth": "1992-03-15",
    "issuer_id":     "atb-financial-ca",
    "issued_at":     "2026-01-20T14:30:00Z",
    "epoch":         42,
}

BOB_FIELDS = {
    "given_name":    "Bob",
    "family_name":   "Smith",
    "jurisdiction":  "Alberta, Canada",
    "id_type":       "Corporate Registration",
    "id_number":     "AB-CORP-2026-00182",
    "date_of_birth": "1985-07-22",
    "issuer_id":     "atb-financial-ca",
    "issued_at":     "2026-02-01T09:00:00Z",
    "epoch":         42,
}

ALICE_ADDR = 0xa11ce00000000000000000000000000000a11ce
BOB_ADDR   = 0x0b0b000000000000000000000000000000000b0b
# Phase 8 V2 A-spend recipient: bound into the CP-DLEQ Fiat-Shamir transcript.
# Set to BOB_ADDR so the forge integration test sends to a registry-verified
# account (Buck.transferCarrying rejects unverified recipients).  The CP-DLEQ
# proof binds (recipient, chainid) so this choice does not relax replay
# protection -- a second proof for a different recipient would need its own
# Fiat-Shamir transcript.
SPEND_RECIPIENT = BOB_ADDR
CHAINID    = 1
REGISTRY_ADDR = int("1d" * 20, 16)

# Public-issuer Schnorr binding (Notes mutual-decryptability, Phase 1).  A
# distinct address so the Solidity parity test can bind it as an
# isPublicIdentity contract without colliding with the registered EOAs.
ISSUER_SCHNORR_ADDR = 0x155EC00000000000000000000000000000155EC0

# The unicode payer (eoa-pub-unicode receipt): a plausible franco-Albertan
# identity whose canonical form pins the raw-UTF-8 dialect -- Latin-1
# accents plus a CJK character -- through the identity scalar, the receipt
# envelope, and the rendered golden text.
UNICODE_FIELDS = {
    "given_name":    "Chloé",
    "family_name":   "Bélanger-李",
    "jurisdiction":  "Alberta, Canada",
    "id_type":       "Alberta Identity Card",
    "id_number":     "AIC-2026-0007744",
    "date_of_birth": "1994-11-02",
    "issuer_id":     "atb-financial-ca",
    "issued_at":     "2026-03-01T08:00:00Z",
    "epoch":         42,
}
UNICODE_ADDR = 0xC10E00000000000000000000000000000000C10E


@dataclass
class _Party:
    fields: Dict[str, Any]
    addr: int
    canonical: str
    m: int
    M: Any
    sigma: Any
    pres: Any
    a: int
    b: int
    kp: Any
    r: int
    E: Any
    proof: RegistrationProof


def _build_party(rng, fork, issuer, fields, addr) -> _Party:
    canonical = canonical_identity_data(fields)
    m = identity_scalar(canonical)
    # Main-stream draws, in the exact positions the pre-A' emitter used:
    # sign (1), the presentation scalar a (formerly the rerandomization t),
    # keygen (1), r (1), then the three nonces m_t, r_t, sk_t.  The A'
    # additions (b, b_t) come from the fork so nothing downstream moves.
    sigma = ps_sign(issuer, m, rng=rng)
    a = rand_scalar(rng)
    kp = identity_keygen(rng=rng)
    r = rand_scalar(rng)
    m_t, r_t, sk_t = rand_scalar(rng), rand_scalar(rng), rand_scalar(rng)
    b, b_t = rand_scalar(fork), rand_scalar(fork)
    M = mul(G1, m)
    E = elgamal_encrypt(M, kp.pk, r)
    pres, _, _ = ps_present(sigma, issuer.pk_Y1, a=a, b=b)
    proof = registration_prove(
        pres, b, m, r, kp.pk, E, addr, kp.sk, CHAINID,
        rng=_replay([m_t, b_t, r_t, sk_t]), registry=REGISTRY_ADDR,
    )
    return _Party(fields, addr, canonical, m, M, sigma, pres, a, b, kp, r, E, proof)


def _party_to_json(p: _Party) -> Dict[str, Any]:
    return {
        "fields": p.fields,
        "canonical_identity_data": p.canonical,
        "m": scalar_to_hex(p.m),
        "M": _g1(p.M),
        "ps_sig_raw":    {"sigma_1": _g1(p.sigma.sigma_1),   "sigma_2": _g1(p.sigma.sigma_2)},
        "ps_presentation": {"A": _g1(p.pres.A), "B": _g1(p.pres.B)},
        "a":             scalar_to_hex(p.a),
        "b":             scalar_to_hex(p.b),
        "elgamal_kp":    {"sk": scalar_to_hex(p.kp.sk), "pk": _g1(p.kp.pk)},
        "r":             scalar_to_hex(p.r),
        "ciphertext":    {"R": _g1(p.E.R), "C": _g1(p.E.C)},
        "registrant":    scalar_to_hex(p.addr),
        "registration_proof": {
            "e":     scalar_to_hex(p.proof.e),
            "s_m":   scalar_to_hex(p.proof.s_m),
            "s_b":   scalar_to_hex(p.proof.s_b),
            "s_r":   scalar_to_hex(p.proof.s_r),
            "s_sk":  scalar_to_hex(p.proof.s_sk),
            "C1":    _g1(p.proof.C1),
            "T_C":   _g1(p.proof.T_C),
            "T_R":   _g1(p.proof.T_R),
            "T_key": _g1(p.proof.T_key),
        },
    }


def build_vectors(seed: int = 0xa1bc_b0ca) -> Dict[str, Any]:
    """Deterministic vector set keyed by `seed`."""
    rng = _seeded_rng(seed)
    fork = _fork_rng(seed)

    issuer = ps_keygen(rng=rng)
    alice  = _build_party(rng, fork, issuer, ALICE_FIELDS, ALICE_ADDR)
    bob    = _build_party(rng, fork, issuer, BOB_FIELDS,   BOB_ADDR)

    # Approve flow: Alice re-encrypts her M for Bob.
    r_prime = rand_scalar(rng)
    E_for_bob = elgamal_encrypt(alice.M, bob.kp.pk, r_prime)
    cp = chaum_pedersen_prove(
        alice.E, E_for_bob, alice.kp.pk, bob.kp.pk,
        alice.kp.sk, r_prime,
        ALICE_ADDR, BOB_ADDR, CHAINID, rng=rng,
        registry=REGISTRY_ADDR,
    )

    # Stream-preservation: the legacy Phase-8 A-spend vectors (spend_cp +
    # spend_a_v2) were removed with the spend_a / spend_cp modules in the
    # Identity-M consolidation, but they consumed four rng scalars here
    # (r_note, the CP-DLEQ `t`, r_iss_mock, spendA_rho).  Reserve the same
    # four so every downstream vector (issuer_schnorr, receipt, issuer_reenc,
    # ...) -- and the committed fixtures / golden files pinned to them -- stays
    # byte-identical.
    for _ in range(4):
        rand_scalar(rng)

    # ---- public-issuer Schnorr binding (decryptability Phase 1) -----------
    #
    # A public issuer signs hBatch = keccak256(cms) with its registered
    # identity key; IdentityRegistry.verifyIssuerSchnorr checks
    # s*G == R + e*pk_iss against the stored pk.  A representative
    # two-commitment batch (each cm < F_R, as Notes requires).
    iss_sk      = rand_scalar(rng)
    iss_pk      = mul(G1, iss_sk)
    schnorr_cms = [rand_scalar(rng) % F_R, rand_scalar(rng) % F_R]
    h_batch     = batch_commitment(schnorr_cms)
    iss_sig     = issuer_schnorr_sign(iss_sk, h_batch, ISSUER_SCHNORR_ADDR, CHAINID, rng=rng)

    # ---- non-deniable receipt (decryptability Phase 1, RcptVerify) --------
    #
    # A full B1 (bearer, public-issuer) receipt: Bob -- a registered
    # *Corporate* Identity, the natural public issuer (cashier's cheque /
    # payroll) -- mints a bearer note that Alice later cashes.  Alice, the
    # payee, assembles a receipt naming Bob as the payer.  Bob's registered
    # identity key (bob.kp) doubles as the Schnorr key the registry checks
    # (pk_iss = _pk[bob] = bob.kp.pk), so the same record that registers Bob
    # authenticates the batch binding.
    #
    # The B1 idHash commits to Bob's identity material via id_hash_b1; the
    # per-leaf signature (sigma) is representative only -- Phase 1 binds the
    # issuer through the *batch* Schnorr over keccak(cms), not the per-leaf
    # sig (see alberta-buck-notes.org "The Non-Deniable-Receipt Invariant" -- B1 uses batch Schnorr for the issuer binding).
    rcpt_face    = 250
    rcpt_rho     = rand_scalar(rng)
    rcpt_sigma_R = mul(G1, rand_scalar(rng))
    rcpt_sigma_s = rand_scalar(rng)
    rcpt_idHash  = id_hash_b1(bob.m, rcpt_sigma_R, rcpt_sigma_s)
    rcpt_opening = NoteOpening(
        flavor=FLAVOR_B1, v=rcpt_face, rho=rcpt_rho,
        id_hash=rcpt_idHash, predicate=0,
    )
    rcpt_cm      = note_commitment(rcpt_opening)
    # cm sits inside a representative multi-leaf batch (other leaves random).
    rcpt_cms     = [rand_scalar(rng) % F_R, rcpt_cm, rand_scalar(rng) % F_R]
    rcpt_hBatch  = batch_commitment(rcpt_cms)
    rcpt_sig     = issuer_schnorr_sign(bob.kp.sk, rcpt_hBatch, BOB_ADDR, CHAINID, rng=rng)
    rcpt_nf      = nullifier_b(rcpt_rho, rcpt_idHash)

    # ---- EOA approve receipt (decryptability Phase 1, verifiable decryption) --
    #
    # Bob (spender/recipient) names Alice (sender) from the approve handshake
    # she published above: =E_for_bob= re-encrypts Alice's registered Identity
    # under Bob's key (the =cp= proof is the soundness half), and Bob proves --
    # verifiably, revealing =alice.M= -- that =E_for_bob= decrypts under his
    # registered key to that point.  The two compose into a third-party-checkable
    # receipt naming Alice with no secret disclosed.
    rcpt_vd = verifiable_decrypt_prove(E_for_bob, bob.kp.sk, alice.M, BOB_ADDR, CHAINID, rng=rng)

    SIMPLE_CONTRACTS = {
        "registry": "0x" + "1d" * 20,
        "buck":     "0x" + "b0" * 20,
        "notes":    "0x" + "70" * 20,
    }

    # Stream-preservation: the original (account-key) AB-RCPT/1 cores were
    # built here and consumed exactly 17 scalars (five payee-vd nonces, the
    # A1 cms/sig draws, and the A2 r/gamma/binding draws).  The Identity-M
    # receipt cores are now built AFTER the issuer_reenc (a2b) section below,
    # whose values are pinned by committed SNARK fixtures (the `tie` fixture
    # of test/NotesA2Tie.t.sol embeds .issuer_reenc.E_iss); reserve the same
    # 17 draws so every a2b value stays byte-identical.
    for _ in range(17):
        rand_scalar(rng)

    # ---- A2 issuer re-encryption binding (decryptability Phase 2) ----------
    #
    # Generated last so its rng draws do not perturb any earlier section.  Bob
    # (private issuer) mints an A2 note addressed to Alice: E_iss re-encrypts his
    # OWN registered Identity M under Alice's key.  The recipient-blinded binding
    # proves E_iss re-encrypts Bob's registered credential (bob.E) without
    # revealing pk_alice -- verifyApprove with pk_rec hidden via the
    # U = r'*H / Q = pk_rec + beta*H linearisation.  See issuer_reenc.py.
    a2b_r_prime = rand_scalar(rng)
    a2b_E_iss   = elgamal_encrypt(bob.M, alice.kp.pk, a2b_r_prime)
    a2b_proof   = issuer_reenc_prove(
        bob.kp.sk, a2b_r_prime, alice.kp.pk, bob.E, a2b_E_iss,
        BOB_ADDR, CHAINID, rng=rng,
    )

    # ---- AB-RCPT/1 Identity-M receipt cores ---------------------------------
    #
    # The receipt envelopes for all five kinds, plus the issuer-side ("I paid
    # X") variants of the three Note flavors -- both Note parties hold the
    # Identity-M note payload (the idHash preimage material created at mint
    # plus the SpentCoupled* event data), so both can build a receipt naming
    # both parties.  Bob is the issuer throughout (a public Corporate Identity
    # for B1/A1; a private Identity for A2); Alice is the addressed recipient/
    # depositor.  Bit-identical property: same inputs, same canonical bytes,
    # deterministic receipt_id.  Built after the pinned a2b section, so the
    # draws here are free to evolve.

    # Alice's MAILBOX key, and the accumulator leaf that ties it to her
    # Identity.  Derived from a fixed seed rather than the rng stream, so it
    # adds no draw and perturbs nothing: the addressed ciphertexts are keyed to
    # this, never to her Identity point, and a payer checks the tie with a
    # Poseidon and a path.
    alice_seed = 0xA11CE_5EED
    alice_k, alice_pk_recv = receiving_key(alice_seed)
    alice_mbx_salt = derive_salt(alice_seed, "mailbox")
    _mbx_tree = IdentityMerkleTree(depth=10, private=True)
    _mbx_tree.insert_mailbox(alice.M, alice_pk_recv, alice_mbx_salt)
    alice_mbx = prove_receiving_binding(alice.M, alice_pk_recv,
                                        alice_mbx_salt, _mbx_tree)

    RCPT_TIME = 1779999000

    # -- eoa-pub: Bob (payee, Private) receives from Alice as a Public Identity.
    eoa_pub_core = build_eoa_pub(
        chainid=CHAINID, contracts=SIMPLE_CONTRACTS,
        payer_addr=ALICE_ADDR, payer_identity=alice.canonical, payer_M=alice.M,
        payer_pk=alice.kp.pk,
        payee_addr=BOB_ADDR, payee_identity=bob.canonical, payee_M=bob.M,
        payee_pk=bob.kp.pk, payee_sk=bob.kp.sk, payee_E_addr=bob.E,
        value=500_000000, block_time=RCPT_TIME,
        txhash="0x" + "ea" * 32, block=1234567, logindex=2,
        rng=rng,
    )

    # -- eoa-priv: Bob (payee) names Alice (Private) via the approve handshake.
    eoa_priv_core = build_eoa_priv(
        chainid=CHAINID, contracts=SIMPLE_CONTRACTS,
        payer_addr=ALICE_ADDR, payer_identity=alice.canonical, payer_M=alice.M,
        payer_pk=alice.kp.pk, payer_E_addr=alice.E,
        E_for_payee=E_for_bob, cp_proof=cp,
        payee_addr=BOB_ADDR, payee_identity=bob.canonical, payee_M=bob.M,
        payee_pk=bob.kp.pk, payee_sk=bob.kp.sk, payee_E_addr=bob.E,
        value=500_000000, block_time=RCPT_TIME,
        txhash="0x" + "ee" * 32, block=1234567, logindex=2,
        rng=rng,
    )

    # -- note-b1: the `receipt` vector above IS the Identity-M B1 note (its
    # idHash = id_hash_b1(bob.m, rcpt_sigma_R, rcpt_sigma_s) binds Bob into
    # the leaf).  At spend, Alice (the depositor) published eDepForIss -- her
    # Identity re-encrypted under Bob's registered public-issuer key -- in the
    # SpentCoupledB1 event; Bob alone decrypts it to name her.
    b1_eDepForIss = elgamal_encrypt(alice.M, bob.kp.pk, rand_scalar(rng))

    _b1_common = dict(
        chainid=CHAINID, contracts=SIMPLE_CONTRACTS,
        issuer_addr=BOB_ADDR, issuer_identity=bob.canonical, issuer_M=bob.M,
        issuer_pk=bob.kp.pk,
        payee_addr=ALICE_ADDR, payee_identity=alice.canonical, payee_M=alice.M,
        payee_pk=alice.kp.pk, payee_E_addr=alice.E,
        opening=rcpt_opening, cms=rcpt_cms, issuer_sig=rcpt_sig,
        sigma_R=rcpt_sigma_R, sigma_s=rcpt_sigma_s,
        nullifier=rcpt_nf, face=rcpt_face,
        value=rcpt_face, block_time=RCPT_TIME,
        txhash="0x" + "b1" * 32, block=1234599, logindex=1,
        mint_txhash="0x" + "bb" * 32, mint_block=1234500,
        eDepForIss=b1_eDepForIss,
    )
    note_b1_core = build_note_b1(
        role="recipient", payee_sk=alice.kp.sk, rng=rng, **_b1_common)
    note_b1_iss_core = build_note_b1(
        role="issuer", issuer_sk=bob.kp.sk, rng=rng, **_b1_common)

    # -- note-a1: identity-targeted (unilateral A1).  Bob, the public issuer,
    # addresses the note to Alice's identity POINT M_rec: eNote encrypts the
    # face under M_rec, eRec the recipient identity under itself, and
    # idHash = id_hash_a1(eNote, m_iss, sigma) binds both parties into the leaf.
    a1m_r_note  = rand_scalar(rng)
    a1m_eNote   = elgamal_encrypt(mul(G1, rcpt_face), alice_pk_recv, a1m_r_note)
    a1m_r_rec   = rand_scalar(rng)
    a1m_eRec    = elgamal_encrypt(alice.M, alice_pk_recv, a1m_r_rec)
    a1m_idHash  = id_hash_a1(a1m_eNote, bob.m, rcpt_sigma_R, rcpt_sigma_s)
    a1m_opening = NoteOpening(flavor=FLAVOR_A1, v=rcpt_face, rho=rcpt_rho,
                              id_hash=a1m_idHash, predicate=0)
    a1m_cm      = note_commitment(a1m_opening)
    a1m_cms     = [rand_scalar(rng) % F_R, a1m_cm, rand_scalar(rng) % F_R]
    a1m_sig     = issuer_schnorr_sign(bob.kp.sk, batch_commitment(a1m_cms),
                                      BOB_ADDR, CHAINID, rng=rng)
    a1m_nf      = nullifier_b(rcpt_rho, a1m_idHash)

    _a1_common = dict(
        chainid=CHAINID, contracts=SIMPLE_CONTRACTS,
        issuer_addr=BOB_ADDR, issuer_identity=bob.canonical, issuer_M=bob.M,
        issuer_pk=bob.kp.pk,
        payee_addr=ALICE_ADDR, payee_identity=alice.canonical, payee_M=alice.M,
        payee_pk=alice.kp.pk, payee_E_addr=alice.E,
        opening=a1m_opening, cms=a1m_cms, issuer_sig=a1m_sig,
        eNote=a1m_eNote, eRec=a1m_eRec,
        pk_recv=alice_pk_recv, mailbox_binding=alice_mbx,
        sigma_R=rcpt_sigma_R, sigma_s=rcpt_sigma_s,
        nullifier=a1m_nf, face=rcpt_face,
        value=rcpt_face, block_time=RCPT_TIME,
        txhash="0x" + "a1" * 32, block=1234599, logindex=1,
        mint_txhash="0x" + "aa" * 32, mint_block=1234500,
    )
    note_a1_core = build_note_a1(
        role="recipient", payee_sk=alice.kp.sk, k_recv=alice_k, rng=rng,
        **_a1_common)
    note_a1_iss_core = build_note_a1(
        role="issuer", r_note=a1m_r_note, r_id=a1m_r_rec, rng=rng, **_a1_common)

    # -- note-a2: identity-targeted (unilateral A2).  Bob, the PRIVATE issuer,
    # encrypts his own registered Identity under Alice's identity point
    # (eIss) and the face under the same point (eNote); the blinded
    # re-encryption binding (verified at mint by Notes' A2 overload) makes the
    # recovered issuer provably the registered minter.
    a2m_r_note  = rand_scalar(rng)
    a2m_eNote   = elgamal_encrypt(mul(G1, rcpt_face), alice_pk_recv, a2m_r_note)
    a2m_r_prime = rand_scalar(rng)
    a2m_eIss    = elgamal_encrypt(bob.M, alice_pk_recv, a2m_r_prime)
    a2m_binding = issuer_reenc_prove(
        bob.kp.sk, a2m_r_prime, alice_pk_recv, bob.E, a2m_eIss,
        BOB_ADDR, CHAINID, rng=rng,
    )
    a2m_idHash  = id_hash_a2(a2m_eNote, a2m_eIss)
    a2m_opening = NoteOpening(flavor=FLAVOR_A2, v=rcpt_face, rho=rcpt_rho,
                              id_hash=a2m_idHash, predicate=0)
    a2m_cm      = note_commitment(a2m_opening)
    a2m_cms     = [rand_scalar(rng) % F_R, a2m_cm, rand_scalar(rng) % F_R]
    a2m_nf      = nullifier_b(rcpt_rho, a2m_idHash)

    _a2_common = dict(
        chainid=CHAINID, contracts=SIMPLE_CONTRACTS,
        issuer_addr=BOB_ADDR, issuer_identity=bob.canonical, issuer_M=bob.M,
        issuer_pk=bob.kp.pk, issuer_E_addr=bob.E,
        payee_addr=ALICE_ADDR, payee_identity=alice.canonical, payee_M=alice.M,
        payee_pk=alice.kp.pk, payee_E_addr=alice.E,
        opening=a2m_opening, cms=a2m_cms,
        eNote=a2m_eNote, eIss=a2m_eIss, binding=a2m_binding,
        pk_recv=alice_pk_recv, mailbox_binding=alice_mbx,
        nullifier=a2m_nf, face=rcpt_face,
        value=rcpt_face, block_time=RCPT_TIME,
        txhash="0x" + "a2" * 32, block=1234599, logindex=1,
        mint_txhash="0x" + "aa" * 32, mint_block=1234500,
    )
    note_a2_core = build_note_a2(
        role="recipient", payee_sk=alice.kp.sk, k_recv=alice_k, rng=rng,
        **_a2_common)
    note_a2_iss_core = build_note_a2(
        role="issuer", issuer_sk=bob.kp.sk, r_note=a2m_r_note,
        r_id=a2m_r_prime, rng=rng, **_a2_common)

    # -- eoa-pub-unicode: the canonical-dialect torture split.  A payer whose
    # identity exercises the raw-UTF-8 canonical dialect (Latin accents +
    # CJK; the kernel vectors add an astral-emoji row), so the WHOLE pipeline
    # -- canonical bytes -> m -> receipt core -> base64url envelope ->
    # receipt_id -> rendered golden text -- pins unicode handling, and an
    # external implementation that escapes or mangles encodings fails the
    # fixtures loudly.  Built last: its draws stay clear of every pinned
    # section above.
    uni_canonical = canonical_identity_data(UNICODE_FIELDS)
    uni_m = identity_scalar(uni_canonical)
    uni_M = mul(G1, uni_m)
    uni_kp = identity_keygen(rng=rng)
    eoa_pub_unicode_core = build_eoa_pub(
        chainid=CHAINID, contracts=SIMPLE_CONTRACTS,
        payer_addr=UNICODE_ADDR, payer_identity=uni_canonical, payer_M=uni_M,
        payer_pk=uni_kp.pk,
        payee_addr=BOB_ADDR, payee_identity=bob.canonical, payee_M=bob.M,
        payee_pk=bob.kp.pk, payee_sk=bob.kp.sk, payee_E_addr=bob.E,
        value=1_250000, block_time=RCPT_TIME,
        txhash="0x" + "1f" * 32, block=1234601, logindex=3,
        rng=rng,
    )

    # Serialise for the vector file — both the canonical bytes and the envelope
    # text, so the off-chain verifier tests can load them directly.
    abrcpt_cores = {
        "eoa_pub":         eoa_pub_core,
        "eoa_priv":        eoa_priv_core,
        "note_b1":         note_b1_core,
        "note_a1":         note_a1_core,
        "note_a2":         note_a2_core,
        "note_b1_issuer":  note_b1_iss_core,
        "note_a1_issuer":  note_a1_iss_core,
        "note_a2_issuer":  note_a2_iss_core,
        "eoa_pub_unicode": eoa_pub_unicode_core,
    }
    abrcpt = {}
    for kind, core in abrcpt_cores.items():
        core_bytes = serialize_core(core)
        abrcpt[kind] = {
            "id":       receipt_id(core_bytes),
            "envelope": envelope_text(core_bytes),
        }

    return {
        "$schema_version": 2,
        "seed":    f"0x{seed:064x}",
        "ORDER":   f"0x{ORDER:064x}",
        "chainid": scalar_to_hex(CHAINID),
        "registry": scalar_to_hex(REGISTRY_ADDR),
        "issuer": {
            "sk_x": scalar_to_hex(issuer.sk_x),
            "sk_y": scalar_to_hex(issuer.sk_y),
            "pk_X": _g2(issuer.pk_X),
            "pk_Y": _g2(issuer.pk_Y),
            "pk_Y1": _g1(issuer.pk_Y1),
        },
        "alice": _party_to_json(alice),
        "bob":   _party_to_json(bob),
        # Unicode canonical-dialect pin: external implementations must
        # reproduce m over the raw UTF-8 bytes of this canonical string.
        "unicode_party": {
            "fields":    UNICODE_FIELDS,
            "canonical_identity_data": uni_canonical,
            "m":         scalar_to_hex(uni_m),
            "M":         _g1(uni_M),
        },
        "approve": {
            "sender":   scalar_to_hex(ALICE_ADDR),
            "spender":  scalar_to_hex(BOB_ADDR),
            "chainid":  scalar_to_hex(CHAINID),
            "registry": scalar_to_hex(REGISTRY_ADDR),
            "E_alice":   {"R": _g1(alice.E.R),  "C": _g1(alice.E.C)},
            "E_for_bob": {"R": _g1(E_for_bob.R), "C": _g1(E_for_bob.C)},
            "r_prime":  scalar_to_hex(r_prime),
            "cp_proof": {
                "e":  scalar_to_hex(cp.e),
                "s1": scalar_to_hex(cp.s1),
                "s2": scalar_to_hex(cp.s2),
                "T1": _g1(cp.T1),
                "T2": _g1(cp.T2),
                "T3": _g1(cp.T3),
            },
        },
        "issuer_schnorr": {
            "issuer":  scalar_to_hex(ISSUER_SCHNORR_ADDR),
            "chainid": scalar_to_hex(CHAINID),
            "pk":      _g1(iss_pk),
            "cms":     [scalar_to_hex(c) for c in schnorr_cms],
            "hBatch":  _u256(h_batch),
            "proof": {
                "e": scalar_to_hex(iss_sig.e),
                "s": scalar_to_hex(iss_sig.s),
                "R": _g1(iss_sig.R),
            },
        },
        "receipt": {
            "flavor":     scalar_to_hex(FLAVOR_B1),
            "issuer":     scalar_to_hex(BOB_ADDR),     # public Corporate Identity
            "recipient":  scalar_to_hex(ALICE_ADDR),   # payee assembling the receipt
            "chainid":    scalar_to_hex(CHAINID),
            "issuer_pk":  _g1(bob.kp.pk),              # registry _pk[issuer]
            "issuer_M":   _g1(bob.M),                  # named payer Identity point
            "opening": {
                "flavor":    scalar_to_hex(FLAVOR_B1),
                "v":         scalar_to_hex(rcpt_face),
                "rho":       scalar_to_hex(rcpt_rho),
                "idHash":    scalar_to_hex(rcpt_idHash),
                "predicate": scalar_to_hex(0),
            },
            "cm":         scalar_to_hex(rcpt_cm),
            "cms":        [scalar_to_hex(c) for c in rcpt_cms],
            "hBatch":     _u256(rcpt_hBatch),
            "issuer_sig": {
                "e": scalar_to_hex(rcpt_sig.e),
                "s": scalar_to_hex(rcpt_sig.s),
                "R": _g1(rcpt_sig.R),
            },
            "nullifier":  scalar_to_hex(rcpt_nf),
            "face":       scalar_to_hex(rcpt_face),
        },
        "approve_receipt": {
            "sender":        scalar_to_hex(ALICE_ADDR),   # named counterparty (payer)
            "spender":       scalar_to_hex(BOB_ADDR),     # recipient assembling it
            "chainid":       scalar_to_hex(CHAINID),
            "registry":      scalar_to_hex(REGISTRY_ADDR),
            "sender_pk":     _g1(alice.kp.pk),            # registry _pk[sender]
            "sender_E_addr": {"R": _g1(alice.E.R), "C": _g1(alice.E.C)},  # _E_addr[sender]
            "spender_pk":    _g1(bob.kp.pk),              # registry _pk[spender]
            "E_for_spender": {"R": _g1(E_for_bob.R), "C": _g1(E_for_bob.C)},
            "cp_proof": {
                "e":  scalar_to_hex(cp.e),
                "s1": scalar_to_hex(cp.s1),
                "s2": scalar_to_hex(cp.s2),
                "T1": _g1(cp.T1),
                "T2": _g1(cp.T2),
                "T3": _g1(cp.T3),
            },
            "M_named":  _g1(alice.M),
            "vd_proof": {
                "e":  scalar_to_hex(rcpt_vd.e),
                "s":  scalar_to_hex(rcpt_vd.s),
                "T1": _g1(rcpt_vd.T1),
                "T2": _g1(rcpt_vd.T2),
            },
        },
        "issuer_reenc": {
            "issuer":  scalar_to_hex(BOB_ADDR),       # private A2 issuer (msg.sender)
            "chainid": scalar_to_hex(CHAINID),
            "pk_iss":  _g1(bob.kp.pk),                # registry _pk[issuer]
            "E_reg":   {"R": _g1(bob.E.R), "C": _g1(bob.E.C)},   # _E_addr[issuer]
            "E_iss":   {"R": _g1(a2b_E_iss.R), "C": _g1(a2b_E_iss.C)},  # leaf E_iss-for-rec
            "proof": {
                "e":   scalar_to_hex(a2b_proof.e),
                "s_r": scalar_to_hex(a2b_proof.s_r),
                "s_b": scalar_to_hex(a2b_proof.s_b),
                "s_s": scalar_to_hex(a2b_proof.s_s),
                "s_g": scalar_to_hex(a2b_proof.s_g),
                "A1":  _g1(a2b_proof.A1),
                "A2":  _g1(a2b_proof.A2),
                "A3":  _g1(a2b_proof.A3),
                "A4":  _g1(a2b_proof.A4),
                "A5":  _g1(a2b_proof.A5),
                "Q":   _g1(a2b_proof.Q),
                "U":   _g1(a2b_proof.U),
                "T":   _g1(a2b_proof.T),
            },
        },
        "abrcpt": abrcpt,
    }


def emit_vectors(path: str, seed: int = 0xa1bc_b0ca) -> Dict[str, Any]:
    """Build vectors and write to `path` as pretty-printed JSON."""
    data = build_vectors(seed=seed)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    return data
