"""Unit tests for buck_core.session: expectations + journal, no chain needed.

StubSession overrides the two backend-touching methods (_execute_tx,
_revert_reason), so these tests exercise exactly the layer buck_core adds
over the original sim Chain: expectation semantics, mismatch accounting,
and JSONL journaling.  The live-EVM path is covered by the sim smoke tests
(alberta_buck/test/test_routing_sim_web3.py) through the Chain subclass.
"""

import logging

import pytest

from buck_core.session import Expect, Journal, Web3Session

DEPLOYER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


class _FakeEth:
    chain_id = 31337


class _FakeW3:
    eth = _FakeEth()


class _Fn:
    """Stands in for a web3 ContractFunction."""
    fn_name = "transfer"


class StubSession(Web3Session):
    """Web3Session with the backend replaced by canned receipts."""

    def __init__(self, status=1, reason="", journal=None):
        super().__init__(_FakeW3(), DEPLOYER, journal=journal)
        self._status = status
        self._reason = reason
        self._block = 0

    def _execute_tx(self, fn, sender, from_addr, gas, value):
        self._block += 1
        return {"status": self._status, "gasUsed": 21_000,
                "transactionHash": b"\xaa" * 32, "blockNumber": self._block}

    def _revert_reason(self, fn, from_addr, gas, value, block):
        return self._reason


def test_ok_expected_ok():
    s = StubSession(status=1)
    rcpt = s.send(_Fn())
    assert rcpt["status"] == 1
    assert s.mismatches == []
    assert s.last_revert_reason == ""


def test_revert_expected_ok_raises_with_reason():
    s = StubSession(status=0, reason="BUCK: sender identity not verified")
    with pytest.raises(RuntimeError, match="transfer.*not verified"):
        s.send(_Fn())
    assert len(s.mismatches) == 1
    assert s.mismatches[0]["outcome"] == "revert"


def test_revert_expected_revert_returns_receipt():
    s = StubSession(status=0, reason="BUCK: overdraw")
    rcpt = s.send(_Fn(), expect=Expect.REVERT, tag="saver1:overdraw")
    assert rcpt["status"] == 0
    assert s.mismatches == []
    assert s.last_revert_reason == "BUCK: overdraw"


def test_ok_expected_revert_warns_and_counts(caplog):
    s = StubSession(status=1)
    with caplog.at_level(logging.WARNING, logger="buck_core.session"):
        rcpt = s.send(_Fn(), expect=Expect.REVERT, tag="saver1:overdraw")
    assert rcpt["status"] == 1
    assert len(s.mismatches) == 1
    assert s.mismatches[0]["matched"] is False
    assert any("expected REVERT" in r.message for r in caplog.records)


def test_journal_written_and_loads(tmp_path):
    path = tmp_path / "run.jsonl"
    s = StubSession(status=1, journal=path)
    s.send(_Fn(), tag="a")
    s.send(_Fn(), tag="b", expect=Expect.REVERT)   # mismatch: succeeded
    s.journal.close()

    entries = Journal.load(path)
    assert [e["i"] for e in entries] == [1, 2]
    assert [e["tag"] for e in entries] == ["a", "b"]
    assert all(e["op"] == "send" and e["fn"] == "transfer" for e in entries)
    assert entries[0]["matched"] is True
    mism = Journal.mismatches(entries)
    assert [e["i"] for e in mism] == [2]
    assert entries[1]["tx"].startswith("0x")
    assert entries[1]["gas"] == 21_000


def test_env_var_enables_journal(tmp_path, monkeypatch):
    path = tmp_path / "env.jsonl"
    monkeypatch.setenv("BUCK_JOURNAL", str(path))
    s = StubSession(status=1)
    s.send(_Fn(), tag="via-env")
    s.journal.close()
    assert Journal.load(path)[0]["tag"] == "via-env"
