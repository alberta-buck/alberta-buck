"""Reference-math tests for the public-issuer Schnorr binding.

Mirrors the on-chain IdentityRegistry.verifyIssuerSchnorr (Notes
mutual-decryptability, Phase 1): completeness (honest signature verifies) and
soundness (tampered batch / response, wrong key, issuer/chain replay rejected),
plus the batch-commitment encoding parity with keccak256(abi.encodePacked(cms)).
"""

from eth_utils import keccak

from alberta_buck.wallet.bn254 import G1, ORDER, mul
from alberta_buck.wallet.schnorr import (
    SchnorrProof,
    batch_commitment,
    issuer_schnorr_sign,
    issuer_schnorr_verify,
)

ISSUER = 0x155EC00000000000000000000000000000155EC0
CHAINID = 1


def _kp(seed: int):
    sk = (seed % (ORDER - 1)) + 1
    return sk, mul(G1, sk)


def test_roundtrip():
    sk, pk = _kp(0xABC)
    hb = batch_commitment([0x1234, 0x5678])
    sig = issuer_schnorr_sign(sk, hb, ISSUER, CHAINID)
    assert issuer_schnorr_verify(pk, sig, hb, ISSUER, CHAINID)


def test_tampered_hbatch_rejected():
    sk, pk = _kp(0xABC)
    hb = batch_commitment([0x1234])
    sig = issuer_schnorr_sign(sk, hb, ISSUER, CHAINID)
    assert not issuer_schnorr_verify(pk, sig, hb ^ 1, ISSUER, CHAINID)


def test_tampered_response_rejected():
    sk, pk = _kp(0xABC)
    hb = batch_commitment([1])
    sig = issuer_schnorr_sign(sk, hb, ISSUER, CHAINID)
    bad = SchnorrProof(e=sig.e, s=(sig.s + 1) % ORDER, R=sig.R)
    assert not issuer_schnorr_verify(pk, bad, hb, ISSUER, CHAINID)


def test_wrong_key_rejected():
    sk, _ = _kp(0xABC)
    _, pk2 = _kp(0xDEF)
    hb = batch_commitment([1])
    sig = issuer_schnorr_sign(sk, hb, ISSUER, CHAINID)
    assert not issuer_schnorr_verify(pk2, sig, hb, ISSUER, CHAINID)


def test_replay_other_issuer_or_chain_rejected():
    sk, pk = _kp(0xABC)
    hb = batch_commitment([1])
    sig = issuer_schnorr_sign(sk, hb, ISSUER, CHAINID)
    assert not issuer_schnorr_verify(pk, sig, hb, ISSUER + 1, CHAINID)
    assert not issuer_schnorr_verify(pk, sig, hb, ISSUER, CHAINID + 1)


def test_batch_commitment_matches_packed_keccak():
    # hBatch must equal keccak256(abi.encodePacked(cms)) as a uint256.
    cms = [0x1234, 0x5678]
    expected = int.from_bytes(
        keccak(b"".join(int(c).to_bytes(32, "big") for c in cms)), "big"
    )
    assert batch_commitment(cms) == expected
