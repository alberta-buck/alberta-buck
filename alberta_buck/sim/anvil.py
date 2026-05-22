"""Spawn / control a local `anvil` and expose its admin RPC.

`anvil` is on PATH in the nix dev shell (foundry).  We run it instant-mining
(block-time 0) and own the clock explicitly via `anvil_setNextBlockTimestamp`
+ `evm_mine` -- the external-driver replacement for Forge's `vm.warp`.
"""

from __future__ import annotations

import socket
import subprocess
import time
from typing import Optional

from web3 import Web3


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Anvil:
    """A handle to a spawned anvil process + a connected Web3."""

    def __init__(self, port: Optional[int] = None, gas_limit: int = 0):
        self.port = port or _free_port()
        # gas_limit 0 -> anvil default (30M); we deploy big contracts so bump.
        self._gas = gas_limit or 60_000_000
        self.proc: Optional[subprocess.Popen] = None
        self.w3: Optional[Web3] = None

    # -- lifecycle ----------------------------------------------------- #

    def __enter__(self) -> "Anvil":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> "Anvil":
        # No --block-time => anvil mines a block per tx (instant); we also
        # mine explicitly on warp.  Disable code-size + block-gas limits
        # (the Universal Router is a very large contract; deploys are big).
        self.proc = subprocess.Popen(
            [
                "anvil",
                "--port", str(self.port),
                "--base-fee", "0",
                "--gas-price", "0",
                "--disable-code-size-limit",
                "--disable-block-gas-limit",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"http://127.0.0.1:{self.port}"
        for _ in range(100):
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 60}))
            try:
                if w3.is_connected():
                    self.w3 = w3
                    return self
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError("anvil did not come up")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    # -- admin RPC ----------------------------------------------------- #

    def _rpc(self, method: str, params: list):
        return self.w3.provider.make_request(method, params)

    def set_balance(self, addr: str, wei: int) -> None:
        self._rpc("anvil_setBalance", [Web3.to_checksum_address(addr), hex(wei)])

    def set_next_block_timestamp(self, ts: int) -> None:
        self._rpc("anvil_setNextBlockTimestamp", [ts])

    def mine(self) -> None:
        self._rpc("evm_mine", [])

    def warp_to(self, ts: int) -> None:
        """Set the next block timestamp and mine, advancing the chain clock."""
        self.set_next_block_timestamp(ts)
        self.mine()
