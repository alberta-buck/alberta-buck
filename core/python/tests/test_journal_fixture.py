"""The cross-language journal fixture: Python side.

core/vectors/journal-sample.jsonl pins the journal schema across languages;
core/js/test/journal.test.js asserts the SAME facts about the SAME file.
Change the fixture only with both suites in hand.
"""

from pathlib import Path

from buck_core.session import Journal

FIXTURE = Path(__file__).resolve().parents[2] / "vectors" / "journal-sample.jsonl"


def test_fixture_parses():
    entries = Journal.load(FIXTURE)
    assert len(entries) == 4
    assert [e["i"] for e in entries] == [1, 2, 3, 4]
    assert entries[0]["op"] == "deploy"
    assert all(e["op"] == "send" for e in entries[1:])


def test_fixture_expectations():
    entries = Journal.load(FIXTURE)
    mism = Journal.mismatches(entries)
    assert [e["i"] for e in mism] == [4]
    # the expected revert is matched, and carries its Solidity reason
    assert entries[2]["expect"] == "revert"
    assert entries[2]["matched"] is True
    assert entries[2]["err"] == "BUCK: sender identity not verified"


def test_fixture_gas_total():
    entries = Journal.load(FIXTURE)
    assert sum(e["gas"] for e in entries) == 4_222_456
