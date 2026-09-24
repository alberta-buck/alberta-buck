"""What an outside observer sees: one transaction, decoded from the chain alone.

Mallory has no one's secrets.  She has an RPC endpoint and the contracts' ABIs,
which are public.  :func:`observe` decodes exactly that: the sender, the called
function and its arguments (from the transaction's calldata), and every event
the transaction emitted (from its logs).  Nothing is taken from a wallet.

Values are summarised by what they are -- an address, an amount, a curve point,
a ciphertext, a proof -- using the ABI's own ``internalType`` names, so a
reader can see which parts of a transaction are readable and which are opaque.

Labels are annotations for the READER.  A label marked public (a public
identity, a contract) is something Mallory can look up herself; a private
label is not, and renders apart from the address so the difference shows.

Used by alberta-buck-privacy.org ("What Mallory sees").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# internalType -> what a reader should call it.
_STRUCT_NAMES = {
    "ElGamalCT":             "ciphertext",
    "G1Point":               "curve point",
    "G2Point":               "curve point (G2)",
    "CPProof":               "re-encryption proof",
    "RegistrationProof":     "registration proof",
    "PSPresentation":        "masked credential",
    "SchnorrProof":          "issuer signature",
    "ContractBindingProof":  "binding authorisation",
    "DepositorBindingProof": "depositor binding proof",
    "IssuerReencProof":      "issuer binding proof",
    "PSPublicKey":           "issuer public key",
    "A2Binding":             "issuer binding",
}


@dataclass(frozen=True)
class Label:
    """A reader's annotation for an address."""
    name:   str
    public: bool = False        # True: Mallory can learn this herself


@dataclass
class Observation:
    """One transaction as the chain shows it."""
    txhash:   str
    block:    int
    time:     int
    sender:   str
    contract: str
    function: str
    args:     List[Tuple[str, str]]
    events:   List[Tuple[str, List[Tuple[str, str]]]]
    gas:      int
    status:   int

    def render(self, width: int = 96) -> str:
        """A compact text rendering: header, arguments, then events."""
        out = [f"{self.contract}.{self.function}  from {self.sender}",
               f"  tx {_short_hex(self.txhash)}  block {self.block}  gas {self.gas:,}"
               + ("" if self.status else "  REVERTED")]
        for name, val in self.args:
            out.append(f"  {name:<18} {val}")
        for ev, fields in self.events:
            out.append(f"  emits {ev}")
            for name, val in fields:
                out.append(f"    {name:<16} {val}")
        return "\n".join(line[:width] for line in out)


# Token amounts, rendered in BUCK: the token's 6 decimals are public too.
AMOUNT_FIELDS = {"amount", "value", "face", "totalFace"}


class Observer:
    """Decodes transactions against a set of known contracts."""

    def __init__(self, w3, contracts: Dict[str, Any],
                 labels: Optional[Dict[str, Label]] = None, decimals: int = 6):
        self.w3 = w3
        self.decimals = decimals
        self.contracts = contracts          # name -> web3 contract
        self.labels = {k.lower(): v for k, v in (labels or {}).items()}
        self._by_addr = {c.address.lower(): (name, c) for name, c in contracts.items()}
        self._events = {}                   # topic0 -> (contract, event name)
        for name, c in contracts.items():
            for item in c.abi:
                if item.get("type") != "event" or item.get("anonymous"):
                    continue
                sig = f"{item['name']}({','.join(_abi_type(i) for i in item['inputs'])})"
                self._events[self.w3.keccak(text=sig).hex().removeprefix("0x")] = (c, item["name"])

    def label(self, address: str, name: str, public: bool = False) -> None:
        self.labels[address.lower()] = Label(name, public)

    def addr(self, a: str) -> str:
        """An address as Mallory sees it, with the reader's annotation."""
        lab = self.labels.get(a.lower())
        if lab is None:
            name = self._by_addr.get(a.lower())
            if name:
                return f"{_short_hex(a)} {name[0]}"
            return _short_hex(a)
        if lab.public:
            return f"{_short_hex(a)} {lab.name}"
        return f"{_short_hex(a)}   [reader: {lab.name}]"

    def observe(self, txhash) -> Observation:
        tx = self.w3.eth.get_transaction(txhash)
        rcpt = self.w3.eth.get_transaction_receipt(txhash)
        blk = self.w3.eth.get_block(rcpt["blockNumber"])
        target = (tx["to"] or "").lower()
        cname, contract = self._by_addr.get(target, (_short_hex(target), None))
        fname, args = "(unknown)", []
        if contract is not None:
            fn, params = contract.decode_function_input(tx["input"])
            fname = fn.fn_name
            abi = {i["name"]: i for i in fn.abi["inputs"]}
            args = [(k, self._value(v, abi.get(k))) for k, v in params.items()]
        events = []
        for log in rcpt["logs"]:
            t0 = log["topics"][0].hex().removeprefix("0x") if log["topics"] else ""
            hit = self._events.get(t0)
            if hit is None:
                events.append(("(unknown event)", []))
                continue
            c, ev = hit
            decoded = getattr(c.events, ev)().process_log(log)
            abi = next(i for i in c.abi if i.get("type") == "event" and i["name"] == ev)
            spec = {i["name"]: i for i in abi["inputs"]}
            events.append((ev, [(k, self._value(v, spec.get(k))) for k, v in decoded["args"].items()]))
        return Observation(
            txhash=rcpt["transactionHash"].to_0x_hex(), block=rcpt["blockNumber"],
            time=blk["timestamp"], sender=self.addr(tx["from"]), contract=cname,
            function=fname, args=args, events=events, gas=rcpt["gasUsed"],
            status=rcpt["status"],
        )

    # -- value summaries ---------------------------------------------------------------------------

    def _value(self, v, abi: Optional[dict]) -> str:
        typ = (abi or {}).get("type", "")
        internal = (abi or {}).get("internalType", "")
        struct = internal.split(".")[-1].removeprefix("struct ").strip("[]") if "struct" in internal else ""
        if typ == "address":
            return self.addr(v)
        if typ.startswith("tuple"):
            what = _STRUCT_NAMES.get(struct, struct or "tuple")
            if typ.endswith("[]"):
                return f"{len(v)} x {what}"
            if struct == "G1Point":
                return f"{what} ({_short_int(v[0])}, {_short_int(v[1])})"
            return what
        if typ == "bytes":
            return f"{len(v)} bytes" + (" (Groth16 proof)" if len(v) == 256 else "")
        if typ.startswith("bytes"):
            return _short_hex("0x" + bytes(v).hex())
        if typ.endswith("[]"):
            return f"[{', '.join(_short_int(x) for x in v)}]" if len(v) <= 4 else f"{len(v)} values"
        if typ.startswith("uint") or typ.startswith("int"):
            if (abi or {}).get("name") in AMOUNT_FIELDS:
                return f"{v / 10 ** self.decimals:,.{self.decimals}f} BUCK"
            return _short_int(v)
        if typ == "bool":
            return str(v)
        return str(v)


def _abi_type(i: dict) -> str:
    t = i["type"]
    if t.startswith("tuple"):
        inner = ",".join(_abi_type(c) for c in i["components"])
        return f"({inner}){t[5:]}"
    return t


def _short_hex(h: str, keep: int = 4) -> str:
    h = h if h.startswith("0x") else "0x" + h
    return h if len(h) <= 2 + 2 * keep + 1 else f"{h[:2 + keep]}..{h[-keep:]}"


def _short_int(v: int) -> str:
    return f"{v:,}" if v < 10**12 else _short_hex(hex(v), 6)


__all__ = ["Label", "Observation", "Observer"]
