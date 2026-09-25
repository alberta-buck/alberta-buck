"""Particulars certificates: current details change, M does not, and disclosure is per field."""

import random

import pytest

from alberta_buck.registry.certificate import registry_keygen
from alberta_buck.registry.particulars import disclose, field_digest, issue_particulars, verify_disclosure
from alberta_buck.sim.cast import CAROL
from alberta_buck.wallet.bn254 import G1, mul
from alberta_buck.wallet.identity import identity_scalar


def _rng(seed=0x9A27):
    r = random.Random(seed)
    return lambda: r.getrandbits(256)


CURRENT = {"legal_name": "Carol Nakamura", "address": "1204 10 Ave SW, Calgary AB",
           "phone": "+1 403 555 0142", "photo": "sha256:5b1c0e9f",
           "age_band": "25-34"}


def _setup(rng):
    kp                          = registry_keygen(rng)
    M                           = mul(G1, identity_scalar(CAROL))
    return kp, M


def test_disclosed_fields_verify_and_others_stay_hidden():
    rng                         = _rng()
    kp, M                       = _setup(rng)
    p                           = issue_particulars(kp, "alberta-identity", M, 1, "2026-04-09T10:05:00Z", CURRENT, rng)
    shown                       = verify_disclosure(disclose(p, "photo", "age_band"), kp.pk, M)
    assert shown == {"photo": "sha256:5b1c0e9f", "age_band": "25-34"}
    assert set(p.certificate.digests) == set(CURRENT)       # every field committed, none readable


def test_a_changed_detail_is_a_new_version_over_the_same_identity():
    rng                         = _rng()
    kp, M                       = _setup(rng)
    v1                          = issue_particulars(kp, "alberta-identity", M, 1, "2026-04-09T10:05:00Z", CURRENT, rng)
    moved                       = dict(CURRENT, address="77 Main St, Canmore AB")
    v2                          = issue_particulars(kp, "alberta-identity", M, 2, "2026-09-01T12:00:00Z", moved, rng)
    for p, want in ((v1, CURRENT["address"]), (v2, moved["address"])):
        assert verify_disclosure(disclose(p, "address"), kp.pk, M) == {"address": want}
    # The identity point is untouched, and the unchanged fields re-commit under fresh salts.
    assert v1.certificate.M == v2.certificate.M
    assert v1.certificate.digests["legal_name"] != v2.certificate.digests["legal_name"]


def test_tampering_and_misuse_fail():
    rng                         = _rng()
    kp, M                       = _setup(rng)
    p                           = issue_particulars(kp, "alberta-identity", M, 1, "2026-04-09T10:05:00Z", CURRENT, rng)
    d                           = disclose(p, "legal_name")
    forged = type(d)(d.certificate, {"legal_name": ("Mallory Smith", d.fields["legal_name"][1])})
    with pytest.raises(ValueError, match="does not open"):
        verify_disclosure(forged, kp.pk, M)
    with pytest.raises(ValueError, match="different identity"):
        verify_disclosure(d, kp.pk, mul(G1, 12345))
    other = registry_keygen(rng)
    with pytest.raises(ValueError, match="signature"):
        verify_disclosure(d, other.pk, M)
    assert field_digest("a", "bc", 1) != field_digest("ab", "c", 1)
