"""Async read-through proxy between the UI and on-chain queries.

The curses inspector re-reads chain state on every render.  Done with blocking
`eth_call`s that is sluggish: dozens of HTTP round-trips per keypress.  This
module interposes a cache + background worker so the UI renders instantly from
*last-known* values and the actual reads happen off the UI thread, surfacing
"as soon as the data is available".

Three small pieces, each with one job:

  ChainReader  -- blocking primitive reads (balanceOf / view) on a *dedicated*
                  read-only web3 connection, isolated from the driver's tx
                  connection so background reads never race the sim's writes.
  AsyncCache   -- generic memoizing layer: get(key, fetch, blocking, default).
                  A generation counter marks every entry stale after a sim
                  step; a worker thread refreshes stale/missing keys.
  ChainProxy   -- the facade the UI uses: balance_of(...) and view(...), each
                  blocking (forced sync) or non-blocking (last-known now,
                  refresh later).  `default_blocking` flips the default so the
                  first frame can be painted fully synchronously.

The proxy is strictly a *read* layer for display.  It never sends a tx and
never shares the driver's Web3, so it cannot affect sim correctness.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Hashable

from web3 import Web3


# --------------------------------------------------------------------------- #
#  ChainReader: blocking primitives on an isolated read-only connection.
# --------------------------------------------------------------------------- #

class ChainReader:
    """Blocking `balanceOf` / `view` reads on a private web3 connection.

    Built from a `Deployment` so it knows each view-contract's ABI; ERC-20
    `balanceOf` works against any address (tokens *and* pools) via the shared
    ERC-20 ABI.  Lives on its own HTTP connection to the same anvil, so the
    single worker thread that drives it never touches the driver's Web3.
    """

    def __init__(self, d):
        url = f"http://127.0.0.1:{d.anvil.port}"
        self.w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
        self._erc20 = d.erc20_abi
        self._abi: dict[str, list] = {}
        for c in (d.buck, d.kctrl, d.basket, d.reg, d.credit):
            self._abi[Web3.to_checksum_address(c.address)] = c.abi
        self._tok_c: dict[str, Any] = {}     # erc20 contract wrappers by addr
        self._view_c: dict[str, Any] = {}    # view contract wrappers by addr

    def _erc20_contract(self, addr: str):
        a = Web3.to_checksum_address(addr)
        c = self._tok_c.get(a)
        if c is None:
            c = self.w3.eth.contract(address=a, abi=self._erc20)
            self._tok_c[a] = c
        return c

    def _view_contract(self, addr: str):
        a = Web3.to_checksum_address(addr)
        c = self._view_c.get(a)
        if c is None:
            c = self.w3.eth.contract(address=a, abi=self._abi[a])
            self._view_c[a] = c
        return c

    def balance_of(self, token_addr: str, holder_addr: str) -> int:
        return self._erc20_contract(token_addr).functions.balanceOf(
            Web3.to_checksum_address(holder_addr)).call()

    def view(self, addr: str, fn: str, args: tuple) -> Any:
        c = self._view_contract(addr)
        return getattr(c.functions, fn)(*args).call()


# --------------------------------------------------------------------------- #
#  AsyncCache: memoizing read-through cache with a background refresh worker.
# --------------------------------------------------------------------------- #

class AsyncCache:
    """Cache keyed by an arbitrary hashable, with generation-based staleness.

    `get` returns the freshest known value immediately.  When the value is
    missing or stale and the call is non-blocking, a refresh is enqueued and
    the *last-known* value (or `default`) is returned at once.  `bump()`
    advances the generation so the next reads schedule a refresh.
    """

    def __init__(self):
        self._val: dict[Hashable, Any] = {}
        self._gen: dict[Hashable, int] = {}
        self._cur_gen = 0
        self._pending: set[Hashable] = set()
        self._q: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self.updated = threading.Event()    # set whenever a refresh lands
        self._stop = False
        self._worker = threading.Thread(
            target=self._work, name="chain-proxy", daemon=True)
        self._worker.start()

    # -- staleness ----------------------------------------------------- #

    def bump(self) -> None:
        """Mark every cached value stale (call once per sim step)."""
        with self._lock:
            self._cur_gen += 1

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._pending)

    # -- read ---------------------------------------------------------- #

    def get(self, key: Hashable, fetch: Callable[[], Any],
            blocking: bool, default: Any) -> Any:
        with self._lock:
            known = key in self._val
            fresh = known and self._gen.get(key) == self._cur_gen
            if fresh:
                return self._val[key]
            last = self._val.get(key, default)
            if not blocking:
                if key not in self._pending:
                    self._pending.add(key)
                    self._q.put((key, fetch))
                return last
            gen_now = self._cur_gen
        # blocking + not fresh: fetch synchronously, keep last-known on error.
        try:
            v = fetch()
        except Exception:
            v = last
        with self._lock:
            self._val[key] = v
            self._gen[key] = gen_now
            self._pending.discard(key)
        return v

    # -- worker -------------------------------------------------------- #

    def _work(self) -> None:
        while not self._stop:
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            key, fetch = item
            try:
                v = fetch()
                ok = True
            except Exception:
                ok = False
            with self._lock:
                if ok:
                    self._val[key] = v
                # Mark fresh as of *now* (the read reflects current chain).
                self._gen[key] = self._cur_gen
                self._pending.discard(key)
            self.updated.set()

    def stop(self) -> None:
        self._stop = True
        self._q.put(None)


# --------------------------------------------------------------------------- #
#  ChainProxy: the read facade the UI consumes.
# --------------------------------------------------------------------------- #

class ChainProxy:
    """`balance_of` / `view` with a blocking (default) or async API.

    Set `default_blocking = True` to force synchronous reads (e.g. to paint a
    fully-populated first frame); leave it False for the responsive path where
    each read returns last-known data and refreshes in the background.
    """

    def __init__(self, d):
        self._reader = ChainReader(d)
        self._cache = AsyncCache()
        self.default_blocking = False

    # lifecycle / signalling ------------------------------------------- #

    def bump(self) -> None:
        self._cache.bump()

    @property
    def busy(self) -> bool:
        return self._cache.busy

    @property
    def updated(self) -> threading.Event:
        return self._cache.updated

    def stop(self) -> None:
        self._cache.stop()

    # reads ------------------------------------------------------------ #

    @staticmethod
    def _addr(x) -> str:
        return Web3.to_checksum_address(
            x.address if hasattr(x, "address") else x)

    def balance_of(self, token, holder, blocking: bool | None = None,
                   default: int = 0) -> int:
        ta, ha = self._addr(token), self._addr(holder)
        blk = self.default_blocking if blocking is None else blocking
        return self._cache.get(
            ("bal", ta, ha), lambda: self._reader.balance_of(ta, ha),
            blocking=blk, default=default)

    def view(self, contract, fn: str, *args, blocking: bool | None = None,
             default: Any = 0) -> Any:
        ca = self._addr(contract)
        blk = self.default_blocking if blocking is None else blocking
        return self._cache.get(
            ("view", ca, fn, args), lambda: self._reader.view(ca, fn, args),
            blocking=blk, default=default)
