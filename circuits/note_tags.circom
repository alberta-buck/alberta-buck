// The Notes hashes' leading Poseidon inputs: keccak(tag) mod F_R for the tags
// in alberta_buck/wallet/domains.py (NOTES_*).  Every circuit that hashes a
// note commitment, nullifier or identity hash reads its tag from here, and
// alberta_buck/test/test_domains.py checks these literals against the registry.
//
// The arities already differ (commitment 6, nullifier 3, identity hash 2, 6 or
// 11), but a tag per function keeps each disjoint from every other Poseidon of
// the same width in the protocol, now and after a later field is added.

pragma circom 2.1.4;

// "AlbertaBuck/Notes/Commitment/v2"
function NOTE_TAG_COMMITMENT() {
    return 1136376137050286018401023273372309885493175044404302851683961068611338172768;
}

// "AlbertaBuck/Notes/Nullifier/v2"
function NOTE_TAG_NULLIFIER() {
    return 17700066355725249984720364728090881703410993711662078444317620302607295290390;
}

// "AlbertaBuck/Notes/IdHash/v2"
function NOTE_TAG_ID_HASH() {
    return 21501053284610636585895416321047374739302936042083334411416197327135048420246;
}
