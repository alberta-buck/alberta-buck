"""Backend-agnostic chain sessions: send/call/deploy with expectations + journal.

Layer 1 of the platform (alberta-buck-platform.org): one testing API over
every execution backend.  Web3Session is the web3.py implementation, used
against anvil (the primary sim backend), public testnets, and Tenderly
virtual testnets.  A JS peer (viem over Tevm/anvil) shares the journal
format; core/vectors/journal-sample.jsonl pins it across languages.

Two kinds of senders:
  * an *unlocked* dev account (a hex address string) -- the node signs;
  * a generated EOA (`eth_account.LocalAccount`) -- we sign locally
    (web3 v7: `signed.raw_transaction`).

Expectations: every state-changing op declares expect=Expect.OK (default)
or Expect.REVERT.  An unexpected revert raises RuntimeError carrying the
Solidity revert reason (exactly the pre-session behavior); an unexpected
success logs a warning and returns normally.  Either way the outcome is
journaled, and contradictions accumulate in ``session.mismatches``.

Journal: append-only JSONL, one object per state-changing op (reads are
not journaled).  Off by default -- zero hot-path cost -- and enabled per
session (``journal=`` path or Journal) or via the BUCK_JOURNAL env var.
"""

from __future__ import annotations

import enum
import json
import logging
import os
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3

from buck_core.artifacts import load_artifact, repo_root

log = logging.getLogger("buck_core.session")

# Gas big enough for the largest deploy (Universal Router) without estimation.
DEPLOY_GAS = 55_000_000
CALL_GAS = 12_000_000


class Expect(enum.Enum):
    """Declared expectation for a state-changing operation."""
    OK = "ok"
    REVERT = "revert"


def _txhex(h: Any) -> str:
    """Render a transaction hash as a 0x-prefixed string across hexbytes versions."""
    if h is None:
        return ""
    if hasattr(h, "to_0x_hex"):
        return h.to_0x_hex()
    if hasattr(h, "hex"):
        s = h.hex()
        return s if s.startswith("0x") else "0x" + s
    return str(h)


class Journal:
    """Append-only JSONL record of session operations.

    One JSON object per line, fields in schema order (see the platform doc):
    i, tag, op, fn, sender, expect, outcome, matched, gas, tx, block, err.
    Consumers ignore unknown fields; the JS reader (core/js/src/journal.js)
    parses the same files.
    """

    FIELDS = ("i", "tag", "op", "fn", "sender", "expect", "outcome",
              "matched", "gas", "tx", "block", "err")

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        self._seq = 0

    def record(self, **fields) -> dict:
        self._seq += 1
        entry = {"i": self._seq}
        entry.update({k: fields.get(k, "") for k in self.FIELDS if k != "i"})
        self._fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        self._fh.flush()
        return entry

    def close(self) -> None:
        self._fh.close()

    @staticmethod
    def load(path: str | Path) -> list[dict]:
        entries = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        return entries

    @staticmethod
    def mismatches(entries: list[dict]) -> list[dict]:
        return [e for e in entries if e.get("matched") is False]


class Web3Session:
    """The web3.py ChainSession: anvil, public testnets, Tenderly.

    The sim's ``alberta_buck.sim.chain.Chain`` is a subclass of this --
    the original API (send/call/deploy/at/balance_of) is unchanged, with
    ``expect=``/``tag=`` and journaling layered on.
    """

    def __init__(self, w3: Web3, deployer: str,
                 journal: Journal | str | Path | None = None):
        self.w3 = w3
        self.deployer = Web3.to_checksum_address(deployer)  # unlocked dev acct
        self.chain_id = w3.eth.chain_id
        self._balance_cache: dict[tuple[str, str], int] = {}
        if journal is None and os.environ.get("BUCK_JOURNAL"):
            journal = os.environ["BUCK_JOURNAL"]
        self.journal = journal if isinstance(journal, (Journal, type(None))) \
            else Journal(journal)
        self.mismatches: list[dict] = []
        self.last_revert_reason: str = ""

    # -- hot read cache ------------------------------------------------ #

    def clear_balance_cache(self) -> None:
        """Drop memoized ERC20 balanceOf reads.

        The externally-driven sim runs against instant-mined blocks.  Within
        a stable block, repeated reserve reads are pure and safe to memoize;
        after a tx/deploy/explicit mine we clear the cache.
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

    def _execute_tx(self, fn, sender: Any, from_addr: str, gas: int, value: int):
        """Sign (if a LocalAccount) or delegate signing, send, await receipt.

        The single override point for stub/backend variants; everything
        above it (expectations, journal, revert handling) is backend-free.
        """
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
        return self.w3.eth.wait_for_transaction_receipt(h)

    def _revert_reason(self, fn, from_addr: str, gas: int, value: int,
                       block: int) -> str:
        """Replay via eth_call at the post-block state to extract the revert
        reason -- anvil returns the Solidity require message in the call
        exception.  Journal the BARE reason ("SPL", "BUCK: ..."), the
        schema's documented form (core/vectors/journal-sample.jsonl) and
        what the JS session extracts -- not web3's exception repr."""
        try:
            fn.call({"from": from_addr, "gas": gas, "value": value},
                    block_identifier=block)
        except Exception as e:
            msg = getattr(e, "message", None)
            if not isinstance(msg, str) and e.args and isinstance(e.args[0], str):
                msg = e.args[0]
            if isinstance(msg, str):
                if msg.startswith("execution reverted: "):
                    msg = msg[len("execution reverted: "):]
                return msg[:400]
            return str(e)[:400]
        return ""

    def send(self, contract_fn_call, sender: Any | None = None,
             gas: int = CALL_GAS, value: int = 0,
             expect: Expect = Expect.OK, tag: str = ""):
        """Send a state-changing tx; return its receipt.

        expect=Expect.OK (default): a revert raises RuntimeError with the
        Solidity reason.  expect=Expect.REVERT: a revert is the expected
        outcome (reason in ``last_revert_reason``); an unexpected success
        logs a warning.  Every outcome is journaled when a journal is set.
        """
        fn = contract_fn_call
        sender = sender if sender is not None else self.deployer
        from_addr = sender.address if isinstance(sender, LocalAccount) \
            else Web3.to_checksum_address(sender)
        rcpt = self._execute_tx(fn, sender, from_addr, gas, value)
        self.clear_balance_cache()

        fname = fn.fn_name if hasattr(fn, "fn_name") else str(fn)
        ok = rcpt["status"] == 1
        reason = "" if ok else self._revert_reason(
            fn, from_addr, gas, value, rcpt["blockNumber"])
        self.last_revert_reason = reason
        entry = self._journal_op("send", fname, from_addr, expect, ok, rcpt,
                                 reason, tag)
        if ok and expect is Expect.REVERT:
            log.warning("expected REVERT but %s succeeded (tag=%r tx=%s)",
                        fname, tag, _txhex(rcpt.get("transactionHash")))
            self.mismatches.append(entry)
        if not ok:
            if expect is Expect.OK:
                self.mismatches.append(entry)
                raise RuntimeError(f"tx reverted: {fname} :: {reason}")
        return rcpt

    def call(self, contract_fn_call):
        return contract_fn_call.call()

    # -- deploy -------------------------------------------------------- #

    def deploy(self, name: str, *args, sol_file: str | None = None,
               bytecode: str | None = None, abi: list | None = None,
               tag: str | None = None):
        if abi is None or bytecode is None:
            abi, bytecode = load_artifact(name, sol_file)
        c = self.w3.eth.contract(abi=abi, bytecode=bytecode)
        h = c.constructor(*args).transact({
            "from": self.deployer, "gas": DEPLOY_GAS, "gasPrice": 0,
        })
        rcpt = self.w3.eth.wait_for_transaction_receipt(h)
        self.clear_balance_cache()
        ok = rcpt["status"] == 1
        # `tag` labels the INSTANCE (the JS session's {name} option is the
        # same seam); the default remains the contract name.
        self._journal_op("deploy", "constructor", self.deployer, Expect.OK,
                         ok, rcpt, "" if ok else "deploy reverted",
                         tag=tag or f"deploy:{name}")
        if not ok:
            raise RuntimeError(f"deploy {name} reverted")
        return self.w3.eth.contract(address=rcpt["contractAddress"], abi=abi)

    def deploy_from_path(self, artifact_path: str, *args):
        art = json.loads((repo_root() / artifact_path).read_text(encoding="utf-8"))
        return self.deploy("", *args, abi=art["abi"],
                           bytecode=art["bytecode"]["object"])

    def at(self, name: str, address: str, sol_file: str | None = None):
        abi, _ = load_artifact(name, sol_file)
        return self.w3.eth.contract(address=Web3.to_checksum_address(address),
                                    abi=abi)

    # -- journal ------------------------------------------------------- #

    def _journal_op(self, op: str, fname: str, sender: str, expect: Expect,
                    ok: bool, rcpt, err: str, tag: str) -> dict:
        outcome = "ok" if ok else "revert"
        entry = {
            "tag": tag, "op": op, "fn": fname, "sender": sender,
            "expect": expect.value, "outcome": outcome,
            "matched": outcome == expect.value,
            "gas": int(rcpt.get("gasUsed", 0)),
            "tx": _txhex(rcpt.get("transactionHash")),
            "block": int(rcpt.get("blockNumber", 0)),
            "err": err,
        }
        if self.journal is not None:
            return self.journal.record(**entry)
        return entry
