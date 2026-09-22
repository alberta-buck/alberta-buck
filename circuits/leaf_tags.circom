// The accumulator leaf kinds' leading Poseidon inputs: keccak(tag) mod F_R for
// the tags in alberta_buck/wallet/domains.py (LEAF_*).  Every circuit that
// hashes a leaf reads its tag from here, and alberta_buck/test/test_domains.py
// checks these literals against the registry.
//
// Without them a salted identity leaf and a receiving leaf were both
// three-input Poseidons, so one accumulator value could be a leaf of either
// kind.  A tag per kind makes each its own function.

pragma circom 2.1.6;

// "AlbertaBuck/Accumulator/Leaf/Identity/v2"
function LEAF_TAG_IDENTITY() {
    return 6089190410636387123103508027202099632965250507789735297680287822653321493477;
}

// "AlbertaBuck/Accumulator/Leaf/IdentitySalted/v2"
function LEAF_TAG_IDENTITY_SALTED() {
    return 13644099589070413235410118031202090970160546952867511877932656137630310762716;
}

// "AlbertaBuck/Accumulator/Leaf/Receiving/v2"
function LEAF_TAG_RECEIVING() {
    return 9537816824190300194230894046903356066691285591297873304038367587676147291771;
}

// "AlbertaBuck/Accumulator/Leaf/Mailbox/v2"
function LEAF_TAG_MAILBOX() {
    return 9340038811138425609060232080233697397397195009568715623545244651246391215200;
}
