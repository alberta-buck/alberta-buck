"""In-process EVM backend: pyrevm behind a web3.py provider.

`PyrevmAnvil` is a drop-in for `alberta_buck.sim.anvil.Anvil`: same facade
(`w3`, `warp_to`, `set_balance`, context manager), but instead of spawning an
anvil subprocess and paying an HTTP JSON-RPC round trip + receipt poll per
transaction, it executes directly on revm (Rust EVM) in-process.  Measured on
the MockERC20 mint/transfer loop this is ~6,900x faster than anvil (75k tx/s
vs 11 tx/s) -- a 5-year equilibrium run drops from ~35 minutes to seconds.

The seam is a custom web3 *provider* (`_RevmProvider`), so all existing sim
code -- `Chain`/`Web3Session`, web3 contract objects, receipt/log parsing --
runs unmodified: web3.py does its usual ABI encode/decode and the provider
answers the ~15 RPC methods the sims actually use.

Fidelity notes (vs anvil):
  * one block per transaction, timestamp constant between `warp_to` calls
    (anvil bumps +1s per auto-mined block; the sims own the clock explicitly
    via warp_to, so this is deterministic rather than lossy);
  * `eth_call` executes non-static inside a snapshot/revert bracket (matches
    anvil semantics: writes allowed, never persisted);
  * gas is executed but unmetered economically (gasPrice 0 profile, like the
    sim's anvil flags); balances only matter where contracts check them.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import pyrevm
import rlp
from eth_account import Account
from eth_account._utils.legacy_transactions import Transaction as LegacyTx
from eth_utils import keccak, to_checksum_address
from web3 import Web3
from web3.providers import BaseProvider

# anvil's well-known dev accounts (index 0..9), unlocked in this backend.
DEV_ACCOUNTS = [
    "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
    "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
    "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
    "0x90F79bf6EB2c4f870365E785982E1f101E93b906",
    "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65",
    "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc",
    "0x976EA74026E726554dB657fA54763abd0C3a0aa9",
    "0x14dC79964da2C08b23698B3D3cc7Ca32193d9955",
    "0x23618e81E3f5cdF7f54C3d65f7FBc0aBf5B21E8f",
    "0xa0Ee7A142d267C1f36714E4a8F75612F20a79720",
]

GENESIS_TS = 1_700_000_000
ZERO32 = "0x" + "00" * 32
BLOOM = "0x" + "00" * 256


def _hex(v: int) -> str:
    return hex(v)


def _h32(seed: bytes) -> str:
    return "0x" + keccak(seed).hex()


class _RevmProvider(BaseProvider):
    """The ~15 JSON-RPC methods the sims use, served from an in-process revm."""

    def __init__(self, backend: "PyrevmAnvil"):
        super().__init__()
        self.b = backend

    # -- provider plumbing ------------------------------------------------ #

    def make_request(self, method: str, params: Any):
        try:
            handler = getattr(self, "rpc_" + method.replace("eth_", "", 1)
                              if method.startswith("eth_") else "rpc__" + method)
        except AttributeError:
            return {"jsonrpc": "2.0", "id": 1,
                    "error": {"code": -32601, "message": f"unsupported: {method}"}}
        try:
            return {"jsonrpc": "2.0", "id": 1, "result": handler(params or [])}
        except _RpcError as e:
            return {"jsonrpc": "2.0", "id": 1, "error": e.err}

    def is_connected(self, show_traceback: bool = False) -> bool:
        return True

    # -- chain basics ------------------------------------------------------ #

    def rpc_chainId(self, p):
        return _hex(self.b.chain_id)

    def rpc_accounts(self, p):
        return DEV_ACCOUNTS

    def rpc_blockNumber(self, p):
        return _hex(self.b.block)

    def rpc_gasPrice(self, p):
        return "0x0"

    def rpc_estimateGas(self, p):
        return _hex(3_000_000)

    def rpc_getBalance(self, p):
        return _hex(self.b.evm.get_balance(to_checksum_address(p[0])))

    def rpc_getCode(self, p):
        code = self.b.evm.get_code(to_checksum_address(p[0]))
        return "0x" + (bytes(code).hex() if code else "")

    def rpc_getTransactionCount(self, p):
        return _hex(self.b.nonces.get(to_checksum_address(p[0]), 0))

    def rpc_getBlockByNumber(self, p):
        return self.b.block_dict()

    def rpc_getBlockByHash(self, p):
        return self.b.block_dict()

    # -- execution --------------------------------------------------------- #

    def rpc_call(self, p):
        tx = p[0]
        frm = to_checksum_address(tx.get("from") or DEV_ACCOUNTS[0])
        data = bytes.fromhex(tx.get("data", tx.get("input", "0x"))[2:]) or None
        value = int(tx.get("value", "0x0"), 16)
        # Non-static inside a snapshot bracket == anvil eth_call semantics.
        cp = self.b.evm.snapshot()
        try:
            out = self.b.evm.message_call(
                frm, to_checksum_address(tx["to"]), calldata=data,
                value=value or None)
            return "0x" + (bytes(out).hex() if out else "")
        except RuntimeError as e:
            raise _RpcError.from_revert(str(e))
        finally:
            try:
                self.b.evm.revert(cp)
            except OverflowError:      # snapshot consumed by inner revert
                pass

    def rpc_sendTransaction(self, p):
        tx = p[0]
        frm = to_checksum_address(tx["from"])
        data = bytes.fromhex(tx.get("data", tx.get("input", "0x") or "0x")[2:])
        to = tx.get("to")
        value = int(tx.get("value", "0x0"), 16)
        return self.b.execute(frm, to, data, value)

    def rpc_sendRawTransaction(self, p):
        raw = bytes.fromhex(p[0][2:]) if isinstance(p[0], str) else bytes(p[0])
        frm = to_checksum_address(Account.recover_transaction(raw))
        if raw[0] > 0x7F:                          # legacy (type-0)
            tx = rlp.decode(raw, LegacyTx)
            to = "0x" + tx.to.hex() if tx.to else None
            data, value = bytes(tx.data), tx.value
        else:                                      # typed (EIP-2718)
            from eth_account.typed_transactions import TypedTransaction
            d = TypedTransaction.from_bytes(raw).as_dict()
            to = d.get("to") or None
            data = bytes(d.get("data", b""))
            value = int(d.get("value", 0))
        return self.b.execute(frm, to, data, value)

    def rpc_getTransactionReceipt(self, p):
        r = self.b.receipts.get(p[0])
        if r is None:
            raise _RpcError({"code": -32000, "message": "receipt not found"})
        return r

    def rpc_getTransactionByHash(self, p):
        r = self.b.receipts.get(p[0])
        if r is None:
            return None
        return {"hash": p[0], "blockNumber": r["blockNumber"],
                "from": r["from"], "to": r["to"], "value": "0x0",
                "gas": r["gasUsed"], "gasPrice": "0x0", "input": "0x",
                "nonce": "0x0", "blockHash": r["blockHash"],
                "transactionIndex": "0x0", "type": "0x0", "chainId": _hex(self.b.chain_id)}


class _RpcError(Exception):
    def __init__(self, err: dict):
        super().__init__(err.get("message", ""))
        self.err = err

    @staticmethod
    def from_revert(msg: str) -> "_RpcError":
        # pyrevm raises RuntimeError("Revert { gas_used: N, output: 0x.. }")
        out = ""
        if "output: 0x" in msg:
            out = "0x" + msg.split("output: 0x", 1)[1].split(" ", 1)[0].rstrip("} ")
        reason = _decode_revert(out)
        return _RpcError({"code": 3,
                          "message": f"execution reverted: {reason}" if reason
                                     else "execution reverted",
                          "data": out or "0x"})


def _decode_revert(out_hex: str) -> str:
    """Error(string) -> the string; custom errors -> their selector hex."""
    if not out_hex or out_hex == "0x":
        return ""
    raw = bytes.fromhex(out_hex[2:])
    if raw[:4] == bytes.fromhex("08c379a0") and len(raw) >= 68:
        try:
            slen = int.from_bytes(raw[36:68], "big")
            return raw[68:68 + slen].decode("utf-8", "replace")
        except Exception:
            return out_hex[:10]
    return "custom error " + out_hex[:10]


class PyrevmAnvil:
    """Anvil-compatible facade over an in-process revm EVM."""

    def __init__(self, port: Optional[int] = None, gas_limit: int = 0,
                 chain_id: Optional[int] = None, auto_impersonate: bool = False,
                 timestamp: Optional[int] = None):
        self.port = port or 0                       # no socket; kept for parity
        self.chain_id = chain_id or 31337
        # The EVM's block.chainid must be the chain id eth_chainId reports:
        # identity proofs bind it (Fiat-Shamir), and pyrevm defaults to 1.
        self.evm = pyrevm.EVM(env=pyrevm.Env(cfg=pyrevm.CfgEnv(chain_id=self.chain_id)),
                              gas_limit=gas_limit or 3_000_000_000)
        self.block = 0
        self.ts = timestamp or GENESIS_TS
        self.nonces: dict[str, int] = {}
        self.receipts: dict[str, dict] = {}
        self.w3: Optional[Web3] = None

    # -- lifecycle ---------------------------------------------------------- #

    def __enter__(self) -> "PyrevmAnvil":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> "PyrevmAnvil":
        for a in DEV_ACCOUNTS:
            self.evm.set_balance(a, 10 ** 24)
        self._push_block_env()
        self.w3 = Web3(_RevmProvider(self))
        return self

    def stop(self) -> None:
        pass

    # -- admin (Anvil API) --------------------------------------------------- #

    def set_balance(self, addr: str, wei: int) -> None:
        self.evm.set_balance(to_checksum_address(addr), wei)

    def set_code(self, addr: str, code) -> None:
        """Anvil `anvil_setCode` analogue.  Preserves balance and nonce."""
        addr = to_checksum_address(addr)
        if isinstance(code, str):
            h = code[2:] if code.startswith("0x") else code
            code_bytes = bytes.fromhex(h)
        else:
            code_bytes = bytes(code)
        try:
            bal = int(self.evm.get_balance(addr))
        except Exception:
            bal = 0
        nonce = self.nonces.get(addr, 0)
        self.evm.insert_account_info(
            addr, pyrevm.AccountInfo(balance=bal, nonce=nonce, code=code_bytes))

    def _rpc(self, method: str, params: list):
        """Subset of Anvil admin RPC used by NotesStack.bind_identities."""
        if method == "anvil_setBalance":
            wei = int(params[1], 16) if isinstance(params[1], str) else int(params[1])
            self.set_balance(params[0], wei)
            return {"jsonrpc": "2.0", "id": 1, "result": True}
        if method == "anvil_setCode":
            self.set_code(params[0], params[1])
            return {"jsonrpc": "2.0", "id": 1, "result": True}
        if method == "anvil_setNextBlockTimestamp":
            ts = int(params[0], 16) if isinstance(params[0], str) else int(params[0])
            self.set_next_block_timestamp(ts)
            return {"jsonrpc": "2.0", "id": 1, "result": True}
        if method == "evm_mine":
            self.mine()
            return {"jsonrpc": "2.0", "id": 1, "result": True}
        raise NotImplementedError(f"PyrevmAnvil._rpc: {method}")

    def set_next_block_timestamp(self, ts: int) -> None:
        self.ts = max(ts, self.ts)

    def mine(self) -> None:
        self.block += 1
        self._push_block_env()

    def warp_to(self, ts: int) -> None:
        self.set_next_block_timestamp(ts)
        self.mine()

    # -- execution core ------------------------------------------------------ #

    def _push_block_env(self) -> None:
        self.evm.set_block_env(pyrevm.BlockEnv(
            number=self.block, timestamp=self.ts))

    def execute(self, frm: str, to: Optional[str], data: bytes,
                value: int) -> str:
        self.block += 1
        self._push_block_env()
        nonce = self.nonces.get(frm, 0)
        self.nonces[frm] = nonce + 1
        txh = _h32(frm.encode() + nonce.to_bytes(8, "big")
                   + self.block.to_bytes(8, "big"))
        blockh = _h32(b"blk" + self.block.to_bytes(8, "big"))

        ok = True
        contract_addr = None
        try:
            if to is None:
                contract_addr = self.evm.deploy(frm, data, value=value or None)
            else:
                self.evm.message_call(frm, to_checksum_address(to),
                                      calldata=data or None,
                                      value=value or None)
        except RuntimeError:
            ok = False
        res = self.evm.result
        gas_used = int(res.gas_used) if res else 21000

        logs = []
        if ok and res:
            for idx, lg in enumerate(res.logs):
                topics = [t if isinstance(t, str) else "0x" + bytes(t).hex()
                          for t in lg.topics]
                ldata = lg.data
                if isinstance(ldata, (tuple, list)):   # (raw topics, data)
                    ldata = ldata[-1] if ldata else b""
                if not isinstance(ldata, str):
                    ldata = "0x" + bytes(ldata).hex()
                logs.append({
                    "address": to_checksum_address(lg.address),
                    "topics": topics, "data": ldata,
                    "blockNumber": _hex(self.block), "blockHash": blockh,
                    "transactionHash": txh, "transactionIndex": "0x0",
                    "logIndex": _hex(idx), "removed": False,
                })

        self.receipts[txh] = {
            "transactionHash": txh, "transactionIndex": "0x0",
            "blockHash": blockh, "blockNumber": _hex(self.block),
            "from": frm, "to": to,
            "cumulativeGasUsed": _hex(gas_used), "gasUsed": _hex(gas_used),
            "contractAddress": contract_addr, "logs": logs,
            "logsBloom": BLOOM, "status": "0x1" if ok else "0x0",
            "effectiveGasPrice": "0x0", "type": "0x0",
        }
        return txh

    # -- block view ------------------------------------------------------------ #

    def block_dict(self) -> dict:
        return {
            "number": _hex(self.block), "hash": _h32(b"blk" + self.block.to_bytes(8, "big")),
            "parentHash": _h32(b"blk" + max(0, self.block - 1).to_bytes(8, "big")),
            "nonce": "0x0000000000000000", "sha3Uncles": ZERO32,
            "logsBloom": BLOOM, "transactionsRoot": ZERO32,
            "stateRoot": ZERO32, "receiptsRoot": ZERO32,
            "miner": DEV_ACCOUNTS[0], "difficulty": "0x0",
            "totalDifficulty": "0x0", "extraData": "0x",
            "size": "0x0", "gasLimit": _hex(3_000_000_000), "gasUsed": "0x0",
            "timestamp": _hex(self.ts), "transactions": [], "uncles": [],
            "baseFeePerGas": "0x0", "mixHash": ZERO32,
        }
