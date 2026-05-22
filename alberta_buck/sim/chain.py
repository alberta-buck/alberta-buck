"""Thin web3.py helpers: load Foundry artifacts, deploy, send txs.

Two kinds of senders:
  * an *unlocked* anvil dev account (a hex address string) -- anvil signs;
  * a generated EOA (`eth_account.LocalAccount`) -- we sign locally
    (web3 v7: `signed.raw_transaction`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "out"

# Gas big enough for the largest deploy (Universal Router) without estimation.
_DEPLOY_GAS = 55_000_000
_CALL_GAS = 12_000_000


def load_artifact(name: str, sol_file: str | None = None) -> tuple[list, str]:
    """Return (abi, bytecode) for out/<sol_file or name>.sol/<name>.json."""
    f = OUT / f"{sol_file or name}.sol" / f"{name}.json"
    art = json.loads(f.read_text())
    return art["abi"], art["bytecode"]["object"]


class Chain:
    def __init__(self, w3: Web3, deployer: str):
        self.w3 = w3
        self.deployer = Web3.to_checksum_address(deployer)  # unlocked anvil acct
        self.chain_id = w3.eth.chain_id

    # -- account helpers ---------------------------------------------- #

    def new_account(self) -> LocalAccount:
        return Account.create()

    def addr(self, who: Any) -> str:
        if isinstance(who, LocalAccount):
            return who.address
        return Web3.to_checksum_address(who)

    # -- tx send ------------------------------------------------------- #

    def _send(self, fn, sender: Any, gas: int, value: int = 0):
        if isinstance(sender, LocalAccount):
            tx = fn.build_transaction({
                "from": sender.address,
                "nonce": self.w3.eth.get_transaction_count(sender.address),
                "gas": gas,
                "gasPrice": 0,
                "value": value,
                "chainId": self.chain_id,
            })
            signed = sender.sign_transaction(tx)
            h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        else:
            h = fn.transact({
                "from": Web3.to_checksum_address(sender),
                "gas": gas,
                "gasPrice": 0,
                "value": value,
            })
        rcpt = self.w3.eth.wait_for_transaction_receipt(h)
        if rcpt["status"] != 1:
            raise RuntimeError(f"tx reverted: {fn.fn_name if hasattr(fn,'fn_name') else fn}")
        return rcpt

    def send(self, contract_fn_call, sender: Any | None = None, gas: int = _CALL_GAS,
             value: int = 0):
        return self._send(contract_fn_call, sender or self.deployer, gas, value)

    def call(self, contract_fn_call):
        return contract_fn_call.call()

    # -- deploy -------------------------------------------------------- #

    def deploy(self, name: str, *args, sol_file: str | None = None,
               bytecode: str | None = None, abi: list | None = None):
        if abi is None or bytecode is None:
            abi, bytecode = load_artifact(name, sol_file)
        c = self.w3.eth.contract(abi=abi, bytecode=bytecode)
        h = c.constructor(*args).transact({
            "from": self.deployer, "gas": _DEPLOY_GAS, "gasPrice": 0,
        })
        rcpt = self.w3.eth.wait_for_transaction_receipt(h)
        if rcpt["status"] != 1:
            raise RuntimeError(f"deploy {name} reverted")
        return self.w3.eth.contract(address=rcpt["contractAddress"], abi=abi)

    def deploy_from_path(self, artifact_path: str, *args):
        art = json.loads((REPO / artifact_path).read_text())
        return self.deploy("", *args, abi=art["abi"],
                           bytecode=art["bytecode"]["object"])

    def at(self, name: str, address: str, sol_file: str | None = None):
        abi, _ = load_artifact(name, sol_file)
        return self.w3.eth.contract(address=Web3.to_checksum_address(address),
                                    abi=abi)
