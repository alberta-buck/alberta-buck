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

import os

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3


def repo_root() -> Path:
    """The repository checkout root -- needed for the Foundry artifacts
    (out/) and contract sources the live-EVM helpers consume.

    Resolution order: the ``ALBERTA_BUCK_REPO`` env var; walking up from this
    file (a repo checkout / editable install); walking up from the cwd (a
    venv-installed package run from inside the repo).  ``foundry.toml`` is
    the marker.  Raises FileNotFoundError when no repo is reachable -- the
    fixture-only paths (package data) do not need one.
    """
    env = os.environ.get("ALBERTA_BUCK_REPO")
    candidates = ([Path(env)] if env else []) + [
        Path(__file__).resolve(), Path.cwd().resolve()]
    for start in candidates:
        for p in (start, *start.parents):
            if (p / "foundry.toml").exists():
                return p
    raise FileNotFoundError(
        "alberta-buck repo root not found (looked for foundry.toml from "
        f"{[str(c) for c in candidates]}); set ALBERTA_BUCK_REPO or run "
        "from inside the repo checkout")


# Gas big enough for the largest deploy (Universal Router) without estimation.
_DEPLOY_GAS = 55_000_000
_CALL_GAS = 12_000_000


def load_artifact(name: str, sol_file: str | None = None) -> tuple[list, str]:
    """Return (abi, bytecode) for out/<sol_file or name>.sol/<name>.json."""
    f = repo_root() / "out" / f"{sol_file or name}.sol" / f"{name}.json"
    art = json.loads(f.read_text())
    return art["abi"], art["bytecode"]["object"]


class Chain:
    def __init__(self, w3: Web3, deployer: str):
        self.w3 = w3
        self.deployer = Web3.to_checksum_address(deployer)  # unlocked anvil acct
        self.chain_id = w3.eth.chain_id
        self._balance_cache: dict[tuple[str, str], int] = {}

    # -- hot read cache ------------------------------------------------ #

    def clear_balance_cache(self) -> None:
        """Drop memoized ERC20 balanceOf reads.

        The externally-driven sim runs against instant-mined Anvil blocks.
        Within a stable block, repeated reserve reads are pure and safe to
        memoize; after a tx/deploy/explicit mine we clear the cache.
        """
        self._balance_cache.clear()

    def balance_of(self, token: Any, holder: str, abi: list | None = None) -> int:
        """Memoized ERC20 `balanceOf(holder)`.

        `token` may be a web3 contract or an address string.  Address strings
        need `abi` so we can build a local contract wrapper on cache miss.
        """
        token_addr = Web3.to_checksum_address(
            token.address if hasattr(token, "address") else token)
        holder_addr = Web3.to_checksum_address(holder)
        key = (token_addr.lower(), holder_addr.lower())
        if key in self._balance_cache:
            return self._balance_cache[key]
        if hasattr(token, "functions"):
            contract = token
        else:
            if abi is None:
                raise ValueError("abi required for address-only balance_of")
            contract = self.w3.eth.contract(address=token_addr, abi=abi)
        value = contract.functions.balanceOf(holder_addr).call()
        self._balance_cache[key] = value
        return value

    # -- account helpers ---------------------------------------------- #

    def new_account(self) -> LocalAccount:
        return Account.create()

    def addr(self, who: Any) -> str:
        if isinstance(who, LocalAccount):
            return who.address
        return Web3.to_checksum_address(who)

    # -- tx send ------------------------------------------------------- #

    def _send(self, fn, sender: Any, gas: int, value: int = 0):
        from_addr = sender.address if isinstance(sender, LocalAccount) \
            else Web3.to_checksum_address(sender)
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
                "from": from_addr,
                "gas": gas,
                "gasPrice": 0,
                "value": value,
            })
        rcpt = self.w3.eth.wait_for_transaction_receipt(h)
        self.clear_balance_cache()
        if rcpt["status"] != 1:
            # Replay via eth_call at the post-block state to extract the
            # revert reason — anvil returns the Solidity require message
            # in the call exception.
            fname = fn.fn_name if hasattr(fn, "fn_name") else str(fn)
            reason = ""
            try:
                fn.call({"from": from_addr, "gas": gas, "value": value},
                        block_identifier=rcpt["blockNumber"])
            except Exception as e:
                reason = str(e)[:400]
            raise RuntimeError(f"tx reverted: {fname} :: {reason}")
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
        self.clear_balance_cache()
        if rcpt["status"] != 1:
            raise RuntimeError(f"deploy {name} reverted")
        return self.w3.eth.contract(address=rcpt["contractAddress"], abi=abi)

    def deploy_from_path(self, artifact_path: str, *args):
        art = json.loads((repo_root() / artifact_path).read_text())
        return self.deploy("", *args, abi=art["abi"],
                           bytecode=art["bytecode"]["object"])

    def at(self, name: str, address: str, sol_file: str | None = None):
        abi, _ = load_artifact(name, sol_file)
        return self.w3.eth.contract(address=Web3.to_checksum_address(address),
                                    abi=abi)
