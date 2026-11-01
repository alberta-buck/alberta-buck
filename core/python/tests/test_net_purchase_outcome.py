"""A buy with no confirmed outcome must not be retried as a known revert."""
import pytest

from buck_core import NetMarketOps, TransactionReceiptPending, TransactionReverted


def test_buy_requires_a_transaction_hash_to_establish_success_or_revert():
    tx_hash = "0x" + "11" * 32

    def raises(exc):
        def dispatch(*_args):
            raise exc
        return dispatch

    for dispatch in (
        raises(RuntimeError("dispatch failed")),
        raises(TransactionReverted("buy", "unknown", {"status": 0})),
        lambda *_args: {"status": 0},
        lambda *_args: {"status": 1, "logs": []},
        lambda *_args: object(),
    ):
        with pytest.raises(TransactionReceiptPending) as uncertain:
            NetMarketOps(None, None, None, dispatch).buy_exact_input("owner", "pool", 1, 1, "owner")
        assert not uncertain.value.transaction_hash

    for dispatch in (
        raises(TransactionReverted("buy", "known", {"status": 0, "transactionHash": tx_hash})),
        lambda *_args: {"status": 0, "transactionHash": tx_hash},
    ):
        with pytest.raises(TransactionReverted) as reverted:
            NetMarketOps(None, None, None, dispatch).buy_exact_input("owner", "pool", 1, 1, "owner")
        assert reverted.value.transaction_hash == tx_hash
