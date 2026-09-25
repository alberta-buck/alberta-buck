"""The v2 tag registry, and every place a tag is compiled in as a literal.

alberta_buck/wallet/domains.py is the one registry.  Circuits and contracts cannot import it, so
they carry literals: the leaf tags in circuits/leaf_tags.circom, the note hash tags in
circuits/note_tags.circom, the Notes tree's empty leaf in every batch-mint circuit and in
Notes.sol, and H_PEDERSEN in IdentityRegistry.sol and its powers table.  Each moves only with a
trusted setup or a redeploy, so each is pinned here against the registry -- a literal that drifts
fails this file, not a ceremony.
"""

import glob
import re

from alberta_buck.registry.tree import (
    TAG_IDENTITY, TAG_IDENTITY_SALTED, TAG_MAILBOX, TAG_RECEIVING,
)
from alberta_buck.wallet import domains
from alberta_buck.wallet.bn254 import point_to_words
from alberta_buck.wallet.nums import H_PEDERSEN
from alberta_buck.wallet.poseidon import poseidon

TAGS                            = {name: getattr(domains, name) for name in domains.__all__
                                   if isinstance(getattr(domains, name), bytes)}


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def _uint(src: str, name: str) -> int:
    m = re.search(name + r"\s*=\s*(\d+)\s*;", src)
    assert m, f"{name} not found"
    return int(m.group(1))


def test_every_tag_is_v2_and_distinct():
    for name, tag in TAGS.items():
        assert tag.startswith(b"AlbertaBuck/") and tag.endswith(b"/v2"), name
    assert len(set(TAGS.values())) == len(TAGS), "two names share one tag"
    assert domains.RECEIPT_ENVELOPE == "AB-RCPT/2"


def test_leaf_tags_circom_matches_the_registry():
    src = _read("circuits/leaf_tags.circom")
    want = {"LEAF_TAG_IDENTITY": TAG_IDENTITY, "LEAF_TAG_IDENTITY_SALTED": TAG_IDENTITY_SALTED,
            "LEAF_TAG_RECEIVING": TAG_RECEIVING, "LEAF_TAG_MAILBOX": TAG_MAILBOX}
    for fn, tag in want.items():
        m = re.search(r"function " + fn + r"\(\)\s*\{\s*return (\d+);", src)
        assert m, fn
        assert int(m.group(1)) == tag, fn
    assert TAG_IDENTITY == domains.field_tag(domains.LEAF_IDENTITY)
    assert len({TAG_IDENTITY, TAG_IDENTITY_SALTED, TAG_RECEIVING, TAG_MAILBOX}) == 4


def test_note_tags_circom_matches_the_registry():
    src = _read("circuits/note_tags.circom")
    want = {"NOTE_TAG_COMMITMENT": domains.NOTES_COMMITMENT,
            "NOTE_TAG_NULLIFIER": domains.NOTES_NULLIFIER,
            "NOTE_TAG_ID_HASH": domains.NOTES_ID_HASH}
    for fn, tag in want.items():
        m = re.search(r"function " + fn + r"\(\)\s*\{\s*return (\d+);", src)
        assert m, fn
        assert int(m.group(1)) == domains.field_tag(tag), fn
    # Every circuit that hashes a note reads its tags; none carries the old untagged constant.
    for path in ["circuits/spend.circom", "circuits/deposit_fold_a1.circom",
                 "circuits/deposit_fold_a2.circom", *glob.glob("circuits/mint_batch*.circom")]:
        body = _read(path)
        assert 'include "./note_tags.circom";' in body, path
        assert "4242" not in body, path


def test_notes_zero_in_every_mint_circuit_and_the_contract():
    zero = domains.field_tag(domains.NOTES_ZERO)
    circuits = sorted(glob.glob("circuits/mint_batch*.circom"))
    assert len(circuits) == 14, circuits
    for path in circuits:
        m = re.search(r"function ZERO_VALUE\(\)\s*\{\s*return (\d+);", _read(path))
        assert m and int(m.group(1)) == zero, path
    notes = _read("src/Notes.sol")
    assert _uint(notes, "ZERO_VALUE") == zero
    root = zero
    for _ in range(20):
        root = poseidon([root, root])
    assert _uint(notes, "EMPTY_ROOT") == root


def test_h_pedersen_in_the_registry_and_its_powers_table():
    x, y = point_to_words(H_PEDERSEN)
    reg = _read("src/IdentityRegistry.sol")
    assert _uint(reg, "H_PED_X") == x
    assert _uint(reg, "H_PED_Y") == y
    table = _read("circuits/ec/powers/bn254_hp_pows.circom")
    for axis, v in ((0, x), (1, y)):
        limbs = [int(re.search(rf"powers\[0\]\[1\]\[{axis}\]\[{i}\] = (\d+);", table).group(1))
                 for i in range(4)]
        assert sum(l << (64 * i) for i, l in enumerate(limbs)) == v


def test_solidity_carries_the_registry_tags():
    """Every v2 tag a contract spells out is the registry's, letter for letter."""
    sources = {p: _read(p) for p in ("src/IdentityRegistry.sol", "src/Notes.sol",
                                     "src/BuckCredit.sol")}
    for p, src in sources.items():
        for tag in re.findall(r'keccak256\("(AlbertaBuck/[^"]+)"\)', src):
            assert tag.encode() in TAGS.values(), f"{p}: {tag} is not a registry tag"
    reg = sources["src/IdentityRegistry.sol"]
    assert _uint(reg, "LEAF_TAG_IDENTITY") == TAG_IDENTITY
    for p, want in (("src/IdentityRegistry.sol", domains.CONSUMER_NOTES_MEMBERSHIP),
                    ("src/Notes.sol", domains.CONSUMER_NOTES_MEMBERSHIP),
                    ("src/IdentityRegistry.sol", domains.CONSUMER_INSURER_ATTESTATION),
                    ("src/BuckCredit.sol", domains.CONSUMER_INSURER_ATTESTATION),
                    ("src/BuckCredit.sol", domains.FS_IDENTITY_OPENING)):
        assert f'keccak256("{want.decode()}")' in sources[p], (p, want)
