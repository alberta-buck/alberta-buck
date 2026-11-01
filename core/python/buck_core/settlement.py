"""Authenticated exact payments of a standard ERC-20 settlement token.

The recipient can be a simulated bank; an ERC-20 transfer alone does not
prove that a real lender has credited a particular loan. The caller owns the
loan ledger, serializes the payer, and applies each confirmed payment once.
"""

from dataclasses import dataclass
from typing import Any, Callable

from web3 import Web3
from web3.logs import DISCARD

from buck_core.session import Expect, TransactionReceiptPending, TransactionReverted, _txhex


@dataclass(frozen=True)
class SettlementPayment:
    amount: int
    receipt: Any


class SettlementOps:
    """Pay a selected recipient, or recover the exact payment from its receipt.

    ``execute`` may dispatch through the payer's simulation proxy. This
    helper assumes a standard, non-rebasing and non-fee-on-transfer token.
    It does not establish finality, track pending state or deduplicate receipts.
    """

    def __init__(self, chain, token, execute: Callable | None = None):
        self.chain, self.token = chain, token
        self.execute = execute or self._send

    def _send(self, owner, contract, method, args):
        return self.chain.send(getattr(contract.functions, method)(*args), sender=owner, expect=Expect.EITHER)

    @staticmethod
    def _terms(owner, recipient, amount):
        sender = getattr(owner, "address", owner)
        if (not isinstance(sender, str) or not isinstance(recipient, str) or
                not Web3.is_address(sender) or not Web3.is_address(recipient) or
                sender.lower() == recipient.lower() or int(sender, 16) == 0 or int(recipient, 16) == 0 or
                type(amount) is not int or not 0 < amount < 2**256):
            raise ValueError("invalid settlement payment terms")
        return sender, Web3.to_checksum_address(recipient)

    def payment_from_receipt(self, owner, recipient, amount: int, receipt, *, expected_hash: str) -> SettlementPayment:
        sender, recipient = self._terms(owner, recipient, amount)
        if not receipt or receipt.get("status") != 1:
            raise ValueError("settlement payment receipt is not successful")
        if (not isinstance(expected_hash, str) or not expected_hash or
                _txhex(receipt.get("transactionHash")).lower() != expected_hash.lower()):
            raise ValueError("settlement payment transaction hash mismatch")
        events = self.token.events.Transfer().process_receipt(receipt, errors=DISCARD)
        events = [event for event in events if event["address"].lower() == self.token.address.lower()]
        if len(events) != 1:
            raise ValueError("settlement payment evidence missing or ambiguous")
        event = events[0]["args"]
        if (event["from"].lower() != sender.lower() or event["to"].lower() != recipient.lower() or
                event["value"] != amount):
            raise ValueError("settlement payment evidence does not match terms")
        return SettlementPayment(amount, receipt)

    def pay_exact(self, owner, recipient: str, amount: int) -> SettlementPayment:
        """Transfer an exact amount; an unknown dispatch outcome must not be retried."""
        _, recipient = self._terms(owner, recipient, amount)
        receipt = None
        try:
            receipt = self.execute(owner, self.token, "transfer", [recipient, amount])
            tx_hash = _txhex(receipt.get("transactionHash")) if receipt else ""
            if receipt and receipt.get("status") == 0 and tx_hash:
                raise TransactionReverted("transfer", "settlement payment reverted", receipt)
            if not receipt or receipt.get("status") != 1 or not tx_hash:
                raise TransactionReceiptPending(tx_hash or None)
            return self.payment_from_receipt(owner, recipient, amount, receipt, expected_hash=tx_hash)
        except TransactionReverted as exc:
            if exc.transaction_hash and exc.receipt.get("status") == 0:
                raise
            raise TransactionReceiptPending(exc.transaction_hash or None) from exc
        except TransactionReceiptPending:
            raise
        except Exception as exc:
            tx_hash = getattr(exc, "transaction_hash", None)
            if not tx_hash and receipt:
                tx_hash = _txhex(receipt.get("transactionHash")) or None
            raise TransactionReceiptPending(tx_hash) from exc
