"""Canonical JSON test-vector emission.

Produces a single JSON file containing all the data the Solidity tests need to
exercise the IdentityRegistry verifier paths against the Python reference:

* PS keypair (issuer)
* Identity records (Alice, Bob): canonical_data, m, ElGamal keypair, ciphertext
* PS signatures (raw and rerandomized)
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
from alberta_buck.wallet.ps import ps_keygen, ps_sign, ps_rerandomize
from alberta_buck.wallet.elgamal import identity_keygen, elgamal_encrypt
from alberta_buck.wallet.nizk import registration_prove, RegistrationProof
from alberta_buck.wallet.chaum_pedersen import chaum_pedersen_prove
from alberta_buck.wallet.spend_cp import spend_cp_prove
from alberta_buck.wallet.notes import (
    FLAVOR_A1, FLAVOR_A2, FLAVOR_B1, NoteOpening, note_commitment,
    nullifier_a, nullifier_b, id_hash_a2, id_hash_b1,
)
from alberta_buck.wallet.schnorr import issuer_schnorr_sign, batch_commitment
from alberta_buck.wallet.verifiable_decrypt import verifiable_decrypt_prove
from alberta_buck.wallet.issuer_reenc import issuer_reenc_prove
from alberta_buck.wallet.envelope import (
    serialize_core, envelope_text, receipt_id,
)
from alberta_buck.wallet.build_receipt import (
    build_eoa_pub, build_eoa_priv,
    build_note_b1, build_note_a1, build_note_a2,
)


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

# Public-issuer Schnorr binding (Notes mutual-decryptability, Phase 1).  A
# distinct address so the Solidity parity test can bind it as an
# isPublicIdentity contract without colliding with the registered EOAs.
ISSUER_SCHNORR_ADDR = 0x155EC00000000000000000000000000000155EC0


@dataclass
class _Party:
    fields: Dict[str, Any]
    addr: int
    canonical: str
    m: int
    M: Any
    sigma: Any
    sigma_p: Any
    kp: Any
    r: int
    E: Any
    proof: RegistrationProof


def _build_party(rng, issuer, fields, addr) -> _Party:
    canonical = canonical_identity_data(fields)
    m = identity_scalar(canonical)
    sigma = ps_sign(issuer, m, rng=rng)
    sigma_p, _ = ps_rerandomize(sigma, rng=rng)
    kp = identity_keygen(rng=rng)
    r = rand_scalar(rng)
    M = mul(G1, m)
    E = elgamal_encrypt(M, kp.pk, r)
    proof = registration_prove(sigma_p, m, r, kp.pk, E, addr, rng=rng)
    return _Party(fields, addr, canonical, m, M, sigma, sigma_p, kp, r, E, proof)


def _party_to_json(p: _Party) -> Dict[str, Any]:
    return {
        "fields": p.fields,
        "canonical_identity_data": p.canonical,
        "m": scalar_to_hex(p.m),
        "M": _g1(p.M),
        "ps_sig_raw":    {"sigma_1": _g1(p.sigma.sigma_1),   "sigma_2": _g1(p.sigma.sigma_2)},
        "ps_sig_rerand": {"sigma_1": _g1(p.sigma_p.sigma_1), "sigma_2": _g1(p.sigma_p.sigma_2)},
        "elgamal_kp":    {"sk": scalar_to_hex(p.kp.sk), "pk": _g1(p.kp.pk)},
        "r":             scalar_to_hex(p.r),
        "ciphertext":    {"R": _g1(p.E.R), "C": _g1(p.E.C)},
        "registrant":    scalar_to_hex(p.addr),
        "registration_proof": {
            "e":    scalar_to_hex(p.proof.e),
            "s_m":  scalar_to_hex(p.proof.s_m),
            "s_r":  scalar_to_hex(p.proof.s_r),
            "A_ps": _g1(p.proof.A_ps),
            "T_C":  _g1(p.proof.T_C),
            "T_R":  _g1(p.proof.T_R),
        },
    }


def build_vectors(seed: int = 0xa1bc_b0ca) -> Dict[str, Any]:
    """Deterministic vector set keyed by `seed`."""
    rng = _seeded_rng(seed)

    issuer = ps_keygen(rng=rng)
    alice  = _build_party(rng, issuer, ALICE_FIELDS, ALICE_ADDR)
    bob    = _build_party(rng, issuer, BOB_FIELDS,   BOB_ADDR)

    # Approve flow: Alice re-encrypts her M for Bob.
    r_prime = rand_scalar(rng)
    E_for_bob = elgamal_encrypt(alice.M, bob.kp.pk, r_prime)
    cp = chaum_pedersen_prove(
        alice.E, E_for_bob, alice.kp.pk, bob.kp.pk,
        alice.kp.sk, r_prime,
        ALICE_ADDR, BOB_ADDR, CHAINID,
        rng=rng,
    )

    # Phase 8 V2 A-spend CP-DLEQ: an A-note ciphertext encrypts Alice's
    # identity point M under Alice's pk with fresh randomness; Alice spends
    # it by proving her registered (sk, pk) matches.
    r_note = rand_scalar(rng)
    E_n_alice = elgamal_encrypt(alice.M, alice.kp.pk, r_note)
    spend_cp = spend_cp_prove(
        E_n_alice, alice.E, alice.kp.pk, alice.kp.sk,
        SPEND_RECIPIENT, CHAINID,
        rng=rng,
    )

    # ---- A2 leaf for the spend_a V2 SNARK fixture --------------------------
    #
    # The leaf encodes a A2 (addressed, private-issuer) note for Alice with
    # ``idHash = Poseidon-8(E_n.R, E_n.C, E_iss.R, E_iss.C)`` (each word
    # reduced mod F_R, mirroring the in-circuit signal reduction).  The
    # mock ``E_iss_alice`` is just a freshly-randomized ElGamal ciphertext
    # of Alice's identity point under her own pk -- the SNARK's binding
    # gate (I) only constrains that the prover supplies the same four
    # ``issuerData`` words at idHash compute time and as the (private)
    # spend witness, so any 4-tuple suffices for the test.  The same
    # ``E_n_alice`` flows through both the in-circuit binding and the
    # off-chain CP-DLEQ verifier (IdentityRegistry.verifySpendCP), which
    # is the V2 invariant: SNARK and identity check agree on the same E_n.
    r_iss_mock = rand_scalar(rng)
    E_iss_alice = elgamal_encrypt(alice.M, alice.kp.pk, r_iss_mock)
    spendA_face       = 100
    spendA_rho        = rand_scalar(rng)
    spendA_predicate  = 0
    spendA_idHash     = id_hash_a2(E_n_alice, E_iss_alice)
    spendA_opening    = NoteOpening(
        flavor=FLAVOR_A2,
        v=spendA_face,
        rho=spendA_rho,
        id_hash=spendA_idHash,
        predicate=spendA_predicate,
    )
    spendA_cm        = note_commitment(spendA_opening)
    spendA_nullifier = nullifier_a(spendA_rho, spendA_idHash)

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
    # sig (see alberta-buck-notes-decryptability.org, residual gap #2).
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

    # ---- AB-RCPT/1 receipt cores (decryptability Phase 1, envelope) -------
    #
    # Build the five receipt kinds from the canonical identity-setup data
    # (Alice, Bob) plus the approve and note artifacts above.  Each produces
    # a ReceiptCore, serialized to canonical bytes, and the envelope text.
    # The bit-identical property: two calls with the same inputs yield the
    # same canonical bytes, so the receipt_id is deterministic.

    SIMPLE_CONTRACTS = {
        "registry": "0x" + "1d" * 20,
        "buck":     "0x" + "b0" * 20,
        "notes":    "0x" + "70" * 20,
    }

    def _e(rng):
        """Shorthand for a receipt-core result struct."""
        pass

    # -- eoa-pub ---------------------------------------------------------------
    # Bob (payee, Private) receives an EOA transfer from Alice as a *Public*
    # Identity.  Bob self-names; Alice is named via her public identity_data.
    eoa_pub_core = build_eoa_pub(
        chainid=CHAINID, contracts=SIMPLE_CONTRACTS,
        payer_addr=ALICE_ADDR, payer_identity=alice.canonical, payer_M=alice.M,
        payer_pk=alice.kp.pk,
        payee_addr=BOB_ADDR, payee_identity=bob.canonical, payee_M=bob.M,
        payee_pk=bob.kp.pk, payee_sk=bob.kp.sk, payee_E_addr=bob.E,
        value=500_000000, block_time=1779999000,
        txhash="0x" + "ea" * 32, block=1234567, logindex=2,
        rng=rng,
    )

    # -- eoa-priv --------------------------------------------------------------
    # Bob (payee, Private) receives from Alice as a *Private* Identity.
    # Bob uses the approve handshake (cp, E_for_bob) + his vd_proof to name
    # Alice, and self-names the same way.
    eoa_priv_core = build_eoa_priv(
        chainid=CHAINID, contracts=SIMPLE_CONTRACTS,
        payer_addr=ALICE_ADDR, payer_identity=alice.canonical, payer_M=alice.M,
        payer_pk=alice.kp.pk, payer_E_addr=alice.E,
        E_for_payee=E_for_bob, cp_proof=cp,
        payee_addr=BOB_ADDR, payee_identity=bob.canonical, payee_M=bob.M,
        payee_pk=bob.kp.pk, payee_sk=bob.kp.sk, payee_E_addr=bob.E,
        value=500_000000, block_time=1779999000,
        txhash="0x" + "ee" * 32, block=1234567, logindex=2,
        rng=rng,
    )

    # -- note-b1 ---------------------------------------------------------------
    # Bob (payee) cashes a B1 bearer note.  The issuer (Bob himself, as a public
    # Corporate Identity) signed the batch; Alice is the depositor.
    # Bob's registered identity_key serves as the issuer Schnorr key.
    note_b1_core = build_note_b1(
        chainid=CHAINID, contracts=SIMPLE_CONTRACTS,
        issuer_addr=BOB_ADDR, issuer_identity=bob.canonical, issuer_M=bob.M,
        issuer_pk=bob.kp.pk,
        payee_addr=ALICE_ADDR, payee_identity=alice.canonical, payee_M=alice.M,
        payee_pk=alice.kp.pk, payee_sk=alice.kp.sk, payee_E_addr=alice.E,
        opening=rcpt_opening, cms=rcpt_cms, issuer_sig=rcpt_sig,
        nullifier=rcpt_nf, face=rcpt_face,
        value=rcpt_face, block_time=1779999000,
        txhash="0x" + "b1" * 32, block=1234599, logindex=1,
        mint_txhash="0x" + "bb" * 32, mint_block=1234500,
        rng=rng,
    )

    # -- note-a1 ---------------------------------------------------------------
    # Bob (issuer) mints an A1 addressed note to Alice.  Alice deposits it via
    # spendACP.  Same structure as B1 but nullifier tag 4243, SpentA event.
    # We reuse the B1 opening as a template with flavor A1.
    a1_opening = NoteOpening(
        flavor=FLAVOR_A1, v=rcpt_face, rho=rcpt_rho,
        id_hash=rcpt_idHash, predicate=0,
    )
    a1_cm = note_commitment(a1_opening)
    a1_cms = [rand_scalar(rng) % F_R, a1_cm, rand_scalar(rng) % F_R]
    a1_hBatch = batch_commitment(a1_cms)
    a1_sig = issuer_schnorr_sign(bob.kp.sk, a1_hBatch, BOB_ADDR, CHAINID, rng=rng)
    a1_nf = nullifier_a(rcpt_rho, rcpt_idHash)
    note_a1_core = build_note_a1(
        chainid=CHAINID, contracts=SIMPLE_CONTRACTS,
        issuer_addr=BOB_ADDR, issuer_identity=bob.canonical, issuer_M=bob.M,
        issuer_pk=bob.kp.pk,
        payee_addr=ALICE_ADDR, payee_identity=alice.canonical, payee_M=alice.M,
        payee_pk=alice.kp.pk, payee_sk=alice.kp.sk, payee_E_addr=alice.E,
        opening=a1_opening, cms=a1_cms, issuer_sig=a1_sig,
        nullifier=a1_nf, face=rcpt_face,
        value=rcpt_face, block_time=1779999000,
        txhash="0x" + "a1" * 32, block=1234599, logindex=1,
        mint_txhash="0x" + "aa" * 32, mint_block=1234500,
        rng=rng,
    )

    # -- note-a2 ---------------------------------------------------------------
    # Bob (issuer, Private) mints an A2 addressed note to Alice.  The
    # E_iss_for_rec field is an ElGamal encrypting Bob's M under Alice's pk.
    a2_E_iss = elgamal_encrypt(bob.M, alice.kp.pk, rand_scalar(rng))
    a2_nf = nullifier_a(rcpt_rho, rcpt_idHash)
    note_a2_core = build_note_a2(
        chainid=CHAINID, contracts=SIMPLE_CONTRACTS,
        issuer_addr=BOB_ADDR, issuer_identity=bob.canonical, issuer_M=bob.M,
        issuer_pk=bob.kp.pk, issuer_E_addr=bob.E,
        E_iss_for_rec=a2_E_iss,
        payee_addr=ALICE_ADDR, payee_identity=alice.canonical, payee_M=alice.M,
        payee_pk=alice.kp.pk, payee_sk=alice.kp.sk, payee_E_addr=alice.E,
        value=rcpt_face, block_time=1779999000,
        txhash="0x" + "a2" * 32, block=1234599, logindex=1,
        mint_txhash="0x" + "aa" * 32, mint_block=1234500,
        nullifier=a2_nf,
        rng=rng,
    )

    # Serialise for the vector file — both the canonical bytes and the envelope
    # text, so the Solidity / off-chain verifier tests can load them directly.
    eoa_pub_bytes  = serialize_core(eoa_pub_core)
    eoa_priv_bytes = serialize_core(eoa_priv_core)
    b1_bytes       = serialize_core(note_b1_core)
    a1_bytes       = serialize_core(note_a1_core)
    a2_bytes       = serialize_core(note_a2_core)

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

    return {
        "$schema_version": 1,
        "seed":    f"0x{seed:064x}",
        "ORDER":   f"0x{ORDER:064x}",
        "chainid": scalar_to_hex(CHAINID),
        "issuer": {
            "sk_x": scalar_to_hex(issuer.sk_x),
            "sk_y": scalar_to_hex(issuer.sk_y),
            "pk_X": _g2(issuer.pk_X),
            "pk_Y": _g2(issuer.pk_Y),
        },
        "alice": _party_to_json(alice),
        "bob":   _party_to_json(bob),
        "approve": {
            "sender":   scalar_to_hex(ALICE_ADDR),
            "spender":  scalar_to_hex(BOB_ADDR),
            "chainid":  scalar_to_hex(CHAINID),
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
        "spend_cp": {
            "spender":   scalar_to_hex(ALICE_ADDR),
            "recipient": scalar_to_hex(SPEND_RECIPIENT),
            "chainid":   scalar_to_hex(CHAINID),
            "r_note":    scalar_to_hex(r_note),
            "E_n":       {"R": _g1(E_n_alice.R), "C": _g1(E_n_alice.C)},
            "proof": {
                "e":  scalar_to_hex(spend_cp.e),
                "s":  scalar_to_hex(spend_cp.s),
                "T1": _g1(spend_cp.T1),
                "T2": _g1(spend_cp.T2),
            },
        },
        "spend_a_v2": {
            "spender":   scalar_to_hex(ALICE_ADDR),
            "recipient": scalar_to_hex(SPEND_RECIPIENT),
            "chainid":   scalar_to_hex(CHAINID),
            "flavor":    scalar_to_hex(FLAVOR_A2),
            "face":      scalar_to_hex(spendA_face),
            "rho":       scalar_to_hex(spendA_rho),
            "predicate": scalar_to_hex(spendA_predicate),
            "idHash":    scalar_to_hex(spendA_idHash),
            "cm":        scalar_to_hex(spendA_cm),
            "nullifier": scalar_to_hex(spendA_nullifier),
            "E_n":       {"R": _g1(E_n_alice.R), "C": _g1(E_n_alice.C)},
            "E_iss":     {"R": _g1(E_iss_alice.R), "C": _g1(E_iss_alice.C)},
            # issuerData = (R_iss.x, R_iss.y, C_iss.x, C_iss.y), each
            # auto-reduced mod F_R to match the circom signal coercion.
            "issuerData": [
                scalar_to_hex(point_to_words(E_iss_alice.R)[0] % F_R),
                scalar_to_hex(point_to_words(E_iss_alice.R)[1] % F_R),
                scalar_to_hex(point_to_words(E_iss_alice.C)[0] % F_R),
                scalar_to_hex(point_to_words(E_iss_alice.C)[1] % F_R),
            ],
            "cp_proof": {
                "e":  scalar_to_hex(spend_cp.e),
                "s":  scalar_to_hex(spend_cp.s),
                "T1": _g1(spend_cp.T1),
                "T2": _g1(spend_cp.T2),
            },
        },
        "issuer_schnorr": {
            "issuer":  scalar_to_hex(ISSUER_SCHNORR_ADDR),
            "chainid": scalar_to_hex(CHAINID),
            "pk":      _g1(iss_pk),
            "cms":     [scalar_to_hex(c) for c in schnorr_cms],
            "hBatch":  scalar_to_hex(h_batch),
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
            "hBatch":     scalar_to_hex(rcpt_hBatch),
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
        "abrcpt": {
            "eoa_pub": {
                "id": receipt_id(eoa_pub_bytes),
                "envelope": envelope_text(eoa_pub_bytes),
            },
            "eoa_priv": {
                "id": receipt_id(eoa_priv_bytes),
                "envelope": envelope_text(eoa_priv_bytes),
            },
            "note_b1": {
                "id": receipt_id(b1_bytes),
                "envelope": envelope_text(b1_bytes),
            },
            "note_a1": {
                "id": receipt_id(a1_bytes),
                "envelope": envelope_text(a1_bytes),
            },
            "note_a2": {
                "id": receipt_id(a2_bytes),
                "envelope": envelope_text(a2_bytes),
            },
        },
    }


def emit_vectors(path: str, seed: int = 0xa1bc_b0ca) -> Dict[str, Any]:
    """Build vectors and write to `path` as pretty-printed JSON."""
    data = build_vectors(seed=seed)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    return data
