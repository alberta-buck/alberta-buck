"""Settlement evidence for the simulated bank, exercised on Anvil."""
import shutil
from pathlib import Path

import pytest

from alberta_buck.sim.anvil import Anvil
from alberta_buck.sim.chain import Chain
from buck_core.session import Expect, TransactionReceiptPending, TransactionReverted, _txhex
from buck_core.settlement import SettlementOps


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("anvil") or
                    not all((ROOT / "out" / f"{name}.sol" / f"{name}.json").is_file()
                            for name in ("MockERC20", "SimLP")),
                    reason="requires anvil and built simulation artifacts")
def test_exact_settlement_payment_and_receipt_recovery():
    with Anvil() as anvil:
        chain = Chain(anvil.w3, anvil.w3.eth.accounts[0])
        token = chain.deploy("MockERC20", "USD Coin", "USDC", 6)
        payer = chain.deploy("SimLP")
        bank, stranger = anvil.w3.eth.accounts[1:3]
        chain.send(token.functions.mint(payer.address, 1000))

        def execute(_owner, contract, method, args):
            data = contract.encode_abi(method, args=args)
            return chain.send(payer.functions.exec(contract.address, bytes.fromhex(data[2:])), expect=Expect.EITHER)

        ops = SettlementOps(chain, token, execute)
        for recipient, amount in ((bank, 0), (payer.address, 1), ("0x" + "00" * 20, 1)):
            with pytest.raises(ValueError, match="terms"):
                ops.pay_exact(payer.address, recipient, amount)
        with pytest.raises(TransactionReverted) as refused:
            ops.pay_exact(payer.address, bank, 1001)
        assert refused.value.transaction_hash and token.functions.balanceOf(bank).call() == 0
        assert not chain.mismatches

        def unknown(*_args):
            raise RuntimeError("transport interrupted")
        with pytest.raises(TransactionReceiptPending) as pending:
            SettlementOps(chain, token, unknown).pay_exact(payer.address, bank, 300)
        assert not pending.value.transaction_hash

        def missing_logs(*args):
            receipt = dict(execute(*args))
            receipt["logs"] = []
            return receipt
        with pytest.raises(TransactionReceiptPending) as pending:
            SettlementOps(chain, token, missing_logs).pay_exact(payer.address, bank, 300)
        assert pending.value.transaction_hash and token.functions.balanceOf(bank).call() == 300
        receipt = anvil.w3.eth.get_transaction_receipt(pending.value.transaction_hash)
        payment = ops.payment_from_receipt(payer.address, bank, 300, receipt,
                                           expected_hash=pending.value.transaction_hash)
        assert payment.amount == 300
        with pytest.raises(ValueError, match="hash mismatch"):
            ops.payment_from_receipt(payer.address, bank, 300, receipt, expected_hash="0x" + "11" * 32)
        with pytest.raises(ValueError, match="does not match"):
            ops.payment_from_receipt(payer.address, stranger, 300, receipt, expected_hash=_txhex(receipt.transactionHash))
        with pytest.raises(ValueError, match="does not match"):
            ops.payment_from_receipt(payer.address, bank, 301, receipt, expected_hash=_txhex(receipt.transactionHash))
        duplicate = dict(receipt)
        duplicate["logs"] = list(receipt.logs) * 2
        with pytest.raises(ValueError, match="ambiguous"):
            ops.payment_from_receipt(payer.address, bank, 300, duplicate, expected_hash=_txhex(receipt.transactionHash))
        result = ops.pay_exact(payer.address, bank, 200)
        assert result.amount == 200 and token.functions.balanceOf(bank).call() == 500
        assert token.functions.balanceOf(payer.address).call() == 500
