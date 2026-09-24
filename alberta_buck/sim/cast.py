"""The privacy paper's cast: fictional people and one fictional institution.

Each record is a CORE record -- the preimage of an identity point M, fixed at
first certification and never edited (doc/review/privacy-paper-plan.org,
decision 7).  ``id_number`` is the registry's person number, never reused;
``issued_at`` and ``epoch`` are the first certification.  Details that change
-- a current legal name, a street address, a phone number, a photo -- belong
to particulars certificates, not here, so that M outlives them.

Every name, number and institution below is invented for the demonstration.
The privacy fixture world (scripts/snark/gen_privacy_world.py) and the
executable privacy paper (alberta-buck-privacy.org) both build from these.
"""

BOB = {
    "given_name":    "Bob",
    "family_name":   "Tremblay",
    "jurisdiction":  "Alberta, Canada",
    "id_type":       "Alberta Identity",
    "id_number":     "AB-P-7730-0142",
    "date_of_birth": "1981-06-09",
    "issuer_id":     "alberta-identity",
    "issued_at":     "2026-04-02T15:20:00Z",
    "epoch":         42,
}

CAROL = {
    "given_name":    "Carol",
    "family_name":   "Nakamura",
    "jurisdiction":  "Alberta, Canada",
    "id_type":       "Alberta Identity",
    "id_number":     "AB-P-8812-5507",
    "date_of_birth": "1994-10-27",
    "issuer_id":     "alberta-identity",
    "issued_at":     "2026-04-09T10:05:00Z",
    "epoch":         42,
}

# A public identity: its record is meant to be read by anyone.
ASPEN = {
    "given_name":    "Aspen Mutual",
    "family_name":   "Credit Union",
    "jurisdiction":  "Alberta, Canada",
    "id_type":       "Credit Union Charter",
    "id_number":     "AB-CU-0417",
    "chartered":     "1938-05-12",
    "issuer_id":     "alberta-identity",
    "issued_at":     "2026-01-15T09:00:00Z",
    "epoch":         42,
}

# The identity registry's private subtree, and the chain every proof binds.
KYC      = "kyc:ca-ab-2026"
CHAINID  = 1

# BUCK carries 6 decimals.
UNIT     = 1_000_000

__all__ = ["BOB", "CAROL", "ASPEN", "KYC", "CHAINID", "UNIT"]
