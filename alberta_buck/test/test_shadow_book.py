"""WP-13: the pseudo-stabilizer booking (alberta_buck/sim/shadow_book.py)."""

from types import SimpleNamespace

from alberta_buck.sim import shadow_book


def test_net_inventory_signs():
    # absorbed positive, issued negative (WAVE3.org "Signs and units")
    assert shadow_book.net_inventory({}) == 0
    assert shadow_book.net_inventory({"ut_absorbed_open": 5_000_000}) == 5_000_000
    assert shadow_book.net_inventory({"ut_issued_open": 2_000_000}) == -2_000_000
    assert shadow_book.net_inventory({"fac_drawn": 3_000_000}) == -3_000_000
    assert shadow_book.net_inventory({"ut_absorbed_open": 5, "ut_issued_open": 2,
                                      "fac_drawn": 3}) == 0


class _Fn:
    def __init__(self, log, q):
        self.log, self.q = log, q


class _Observer:
    def __init__(self, log):
        self.functions = SimpleNamespace(setShadowOffset=lambda q: _Fn(log, q))


def _deployment(log, with_observer=True):
    def send(fn, sender=None):
        fn.log.append((fn.q, sender))
    chain = SimpleNamespace(send=send)
    return SimpleNamespace(chain=chain, gov="0xG0V",
                           observer=_Observer(log) if with_observer else None)


def test_book_is_noop_without_observer():
    log = []
    ctr = {"ut_absorbed_open": 7}
    shadow_book.book(_deployment(log, with_observer=False), ctr)
    assert log == [] and "sh_offset" not in ctr


def test_book_sends_only_on_change():
    log = []
    d = _deployment(log)
    ctr = {}
    shadow_book.book(d, ctr)                       # 0 -> 0: nothing to send
    assert log == [] and ctr.get("sh_offset_txs", 0) == 0
    ctr["ut_absorbed_open"] = 4_000_000
    shadow_book.book(d, ctr)
    assert log == [(4_000_000, "0xG0V")]
    assert ctr["sh_offset"] == 4_000_000 and ctr["sh_offset_txs"] == 1
    shadow_book.book(d, ctr)                       # unchanged: no second tx
    assert len(log) == 1
    ctr["fac_drawn"] = 9_000_000                   # issued: the sum turns negative
    shadow_book.book(d, ctr)
    assert log[-1] == (-5_000_000, "0xG0V") and ctr["sh_offset_txs"] == 2


def test_book_records_failure_and_keeps_last_value():
    log = []
    d = _deployment(log)
    ctr = {"ut_absorbed_open": 1}
    shadow_book.book(d, ctr)
    assert ctr["sh_offset"] == 1

    def boom(fn, sender=None):
        raise RuntimeError("Revert")
    d.chain.send = boom
    ctr["ut_absorbed_open"] = 2
    shadow_book.book(d, ctr)
    assert ctr["sh_offset"] == 1 and "Revert" in ctr["sh_offset_err"]
