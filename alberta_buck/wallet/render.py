"""Extensible receipt renderer — take a :class:`ReceiptCore`, produce a
formatted document, and drive it to an output medium.

Reference: alberta-buck-receipt.org ("The Physical Artifact").

Architecture (three layers):

1. **Detail levels** — :class:`Detail` enum controls how much of each party's
   identity and each transaction field is displayed.  Per-component
   (``payer``, ``payee``, ``txn``, ``verify``), independently settable.

2. **Intermediate document** — :class:`ReceiptDoc` is a list of
   :class:`ReceiptSection` items, each a list of :class:`StyledLine` items.
   The renderer builds this document from a :class:`ReceiptCore`; output
   drivers consume it.  The document carries no printer control codes — just
   semantic styling (align, emphasis, size).

3. **Output drivers** — :class:`Driver` subclasses serialise a
   :class:`ReceiptDoc` to a concrete format.  :class:`TextDriver` emits plain
   ASCII (suitable for a ``.txt`` file or a generic line printer);
   a future :class:`EscposDriver` will emit ESC/POS bytes for 80mm thermal
   printers.

Usage::

    from alberta_buck.wallet.render import render_receipt, TextDriver

    doc = render_receipt(core, payer_detail=Detail.NAME)
    text = TextDriver(width=48).render(doc)
    print(text)
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from alberta_buck.wallet.envelope import ReceiptCore, PartyRecord, TxnRecord
from alberta_buck.wallet.bn254 import scalar_to_hex

# ---------------------------------------------------------------------------
# Detail level
# ---------------------------------------------------------------------------

class Detail(enum.Enum):
    """How much of a component to display.

    For a party's identity:
      - MINIMAL — just the M point fingerprint (first/last 4 chars of x).
      - NAME    — human-readable name, id_type, jurisdiction, and address.
      - FULL    — every canonical_identity_data field, plus the full M point.

    For the proof/verification section:
      - MINIMAL — just the status banner.
      - NORMAL   — status + receipt_id + verification instructions.
      - FULL    — status + receipt_id + instructions + the full payload block.
    """
    MINIMAL = "minimal"
    NAME    = "name"
    NORMAL  = "normal"
    FULL    = "full"


# ---------------------------------------------------------------------------
# Intermediate document
# ---------------------------------------------------------------------------

@dataclass
class StyledLine:
    """One line in a receipt section."""
    text:    str
    align:   str = "left"    # "left" | "center" | "right"
    emphasis: bool = False   # driver renders as bold / double-strike
    size:    str = "normal"  # "normal" | "small" | "large"


@dataclass
class ReceiptSection:
    """A group of styled lines, optionally with a separator rule above."""
    lines:     List[StyledLine] = field(default_factory=list)
    separator: bool = True


@dataclass
class ReceiptDoc:
    """Format-agnostic receipt document — a list of sections."""
    sections: List[ReceiptSection] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Output drivers
# ---------------------------------------------------------------------------

class Driver:
    """Abstract output driver."""
    def __init__(self, width: int = 48):
        self.width = width

    def render(self, doc: ReceiptDoc) -> str:
        raise NotImplementedError


class TextDriver(Driver):
    """Plain ASCII text output.

    Style mapping:
      - emphasis → UPPERCASE / ====== separators
      - large    → centred, padded with spaces
      - small    → indented by 2
      - align    → left / centre / right within *width*
    """

    def __init__(self, width: int = 48):
        self.width = width

    def _rule(self, char: str = "=") -> str:
        return char * self.width

    def _format(self, line: StyledLine) -> str:
        text = line.text
        w = self.width

        # Size adjustments
        if line.size == "small":
            text = "  " + text
        elif line.size == "large":
            pass  # handled by alignment

        # Alignment (pad to width).  Lines may be longer than width
        # (e.g. payload blocks); keep them as-is rather than truncating.
        if line.align == "center":
            text = text.center(w)
        elif line.align == "right":
            text = text.rjust(w)
        # left: no padding — the caller controls indentation

        return text

    def render(self, doc: ReceiptDoc) -> str:
        out: List[str] = []
        for sec in doc.sections:
            if sec.separator and out:
                out.append(self._rule("-"))
            for line in sec.lines:
                rendered = self._format(line)
                out.append(rendered)
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Renderer — builds a ReceiptDoc from a ReceiptCore
# ---------------------------------------------------------------------------

DEFAULT_WIDTH = 50

# Fields a public party always shows (NAME level):
_ID_FIELDS_NAME = {"given_name", "family_name", "id_type", "jurisdiction"}


def _parse_identity_fields(identity: str) -> dict:
    """Parse the canonical JSON identity string to a dict."""
    import json
    return json.loads(identity)


def _format_m_fingerprint(M_hex: dict) -> str:
    """Short M fingerprint: 0x + first 12 + .. + last 12 hex digits of x."""
    x = M_hex["x"] if isinstance(M_hex, dict) else ""
    return _format_hex(x, keep=12)


def _format_addr(addr: str) -> str:
    """Ethereum address — rightmost 20 bytes (42 chars), fits in 48 columns."""
    if len(addr) == 66:  # full uint256 hex: keep only last 40 hex digits
        return "0x" + addr[-40:]
    return addr


def _format_hex(h: str, keep: int = 12) -> str:
    """Truncate a long hex string: 0x + first *keep* + .. + last *keep*.

    All shortened hex on the receipt (tx hash, mint hash, M fingerprint,
    nullifier) uses the same 12+12 representation.
    """
    if len(h) <= 2 + 2 * keep + 4:  # short enough already
        return h
    prefix = h[2:2 + keep] if h.startswith("0x") else h[:keep]
    suffix = h[-keep:]
    leader = "0x" if h.startswith("0x") else ""
    return f"{leader}{prefix}..{suffix}"


def _identity_lines(party: PartyRecord, detail: Detail, label: str) -> List[StyledLine]:
    """Build the identity block for one party."""
    lines: List[StyledLine] = []

    try:
        fields = _parse_identity_fields(party.identity)
    except Exception:
        fields = {}

    kind_badge = "PUBLIC IDENTITY" if party.kind == "public" else "PRIVATE IDENTITY"
    lines.append(StyledLine(f"{label} ({kind_badge})", emphasis=True))

    if detail == Detail.MINIMAL:
        lines.append(StyledLine(f" acct {_format_addr(party.addr)}"))
        lines.append(StyledLine(f" idpt {_format_m_fingerprint(party.M)}"))
        return lines

    # NAME or FULL
    given  = fields.get("given_name", "")
    family = fields.get("family_name", "")
    name   = f"{given} {family}".strip()
    if name:
        lines.append(StyledLine(f" {name}", emphasis=True))

    id_type = fields.get("id_type", "")
    juris   = fields.get("jurisdiction", "")
    if id_type or juris:
        lines.append(StyledLine(f" {id_type} - {juris}".strip(" -")))

    lines.append(StyledLine(f" acct {_format_addr(party.addr)}"))

    if detail == Detail.FULL:
        # Print every field from canonical_identity_data
        for k, v in fields.items():
            if k in ("given_name", "family_name", "id_type", "jurisdiction"):
                continue  # already printed above
            lines.append(StyledLine(f" {k}: {v}"))
        # Full M point
        x = party.M["x"] if isinstance(party.M, dict) else ""
        y = party.M["y"] if isinstance(party.M, dict) else ""
        lines.append(StyledLine(f" M.x {x}"))
        lines.append(StyledLine(f" M.y {y}"))
    else:
        # NAME: just the M fingerprint
        lines.append(StyledLine(f" idpt {_format_m_fingerprint(party.M)}"))

    return lines


def _txn_lines(txn: TxnRecord, core_type: str, chainid: int,
               detail: Detail) -> List[StyledLine]:
    """Build the transaction details block.

    Three-column layout at DEFAULT_WIDTH (50):
      - Labels: left-aligned, 7 chars wide (after a 1-space indent)
      - Values: right-aligned in 20 chars, starting at column 9
      - Types/units: left-aligned, starting at column 31 (position 30)

    Lines without a unit (Type, Tx, Mint, Chain, Block, Log) use only the
    label + value columns.  Lines with a unit (Amount→BUCK, When→UTC) use
    all three.
    """
    lines: List[StyledLine] = []
    from datetime import datetime, timezone

    type_labels = {
        "eoa-pub":  "EOA xfer (Identity-bound, public payer)",
        "eoa-priv": "EOA xfer (Identity-bound, private payer)",
        "note-b1":  "Note spend — B1 bearer, public issuer",
        "note-a1":  "Note spend — A1 addressed, public issuer",
        "note-a2":  "Note spend — A2 addressed, private issuer",
    }

    LABEL_W = 7   # label column width (after 1-space indent)
    VAL_W   = 20  # value column width (right-aligned)

    def _row(label: str, text: str) -> StyledLine:
        """Label + free text (Type, Tx, Mint — no column alignment needed)."""
        return StyledLine(f" {label:<{LABEL_W}} {text}")

    def _row_val(label: str, val: str, unit: str = "") -> StyledLine:
        """Label + right-aligned value + optional unit (Amount, When, etc.).

        The unit always starts at the same column regardless of value width,
        so BUCK, UTC, and numeric values are visually aligned.
        """
        text = f" {label:<{LABEL_W}} {val:>{VAL_W}}"
        if unit:
            text = f"{text}  {unit}"
        return StyledLine(text)

    # Amount
    buck = txn.value / 1_000_000
    lines.append(_row("Type", type_labels.get(core_type, core_type)))
    lines.append(_row_val("Amount", f"{buck:.6f}", "BUCK"))

    if detail != Detail.MINIMAL:
        ts = datetime.fromtimestamp(txn.timestamp, tz=timezone.utc)
        lines.append(_row_val("When", ts.strftime("%Y-%m-%d %H:%M"), "UTC"))

    lines.append(_row_val("Chain", str(chainid)))
    lines.append(_row_val("Block", str(txn.block)))
    lines.append(_row_val("Log", str(txn.logindex)))

    if detail != Detail.MINIMAL:
        lines.append(_row("Tx", _format_hex(txn.txhash)))
        if txn.kind == "note-spend" and txn.mint_txhash:
            lines.append(_row("Mint", _format_hex(txn.mint_txhash)))

    if detail == Detail.FULL and txn.kind == "note-spend" and txn.nullifier:
        lines.append(_row("Nullifier", _format_hex(txn.nullifier)))

    return lines


def _verify_lines(core: ReceiptCore, receipt_id_str: str,
                  payload_text: str, detail: Detail) -> List[StyledLine]:
    """Build the verification / re-check instructions block."""
    lines: List[StyledLine] = []

    status = core.issuer_binding_status or ""
    if status == "unverified":
        banner = "UNVERIFIED ISSUER (no binding)"
    else:
        banner = "VALID"

    lines.append(StyledLine(f" status @ issue: {banner}", emphasis=True))

    if detail != Detail.MINIMAL:
        lines.append(StyledLine(f" receipt  {receipt_id_str}"))

    if detail == Detail.FULL:
        lines.append(StyledLine(" Re-verify:  albertabuck verify -"))
        lines.append(StyledLine("   (offline checks proofs + names; add --rpc"))
        lines.append(StyledLine("    to anchor to chain)"))
        lines.append(StyledLine(""))
        lines.append(StyledLine(" " + ("-" * (DEFAULT_WIDTH - 2)), size="small"))
        # Print the payload block
        for line in payload_text.split("\n"):
            lines.append(StyledLine(" " + line, size="small"))

    return lines


def _notes_lines(extra_notes: Optional[List[str]]) -> List[StyledLine]:
    """Optional free-text line items (unverified)."""
    if not extra_notes:
        return []
    lines: List[StyledLine] = [StyledLine(" (informational — NOT verified)", size="small")]
    for n in extra_notes:
        lines.append(StyledLine(f" {n}"))
    return lines


def render_receipt(
    core:             ReceiptCore,
    *,
    payer_detail:     Detail = Detail.NAME,
    payee_detail:     Detail = Detail.NAME,
    txn_detail:       Detail = Detail.NORMAL,
    verify_detail:    Detail = Detail.NORMAL,
    extra_notes:      Optional[List[str]] = None,
) -> ReceiptDoc:
    """Build a :class:`ReceiptDoc` from a :class:`ReceiptCore`.

    Detail levels control each component independently.  ``extra_notes`` are
    free-text line items printed in an unverified NOTES section (if present).
    """
    from alberta_buck.wallet.envelope import serialize_core, envelope_text, receipt_id as _rid

    payload_bytes  = serialize_core(core)
    rid            = _rid(payload_bytes)
    payload_text   = envelope_text(payload_bytes)

    doc = ReceiptDoc()

    # --- header -----------------------------------------------------------
    doc.sections.append(ReceiptSection([
        StyledLine("ALBERTA  BUCK", align="center", emphasis=True, size="large"),
        StyledLine("P A Y M E N T   R E C E I P T", align="center", emphasis=True),
    ], separator=False))

    doc.sections.append(ReceiptSection([
        StyledLine(f"Receipt  {rid}", size="small"),
    ]))

    # --- parties ----------------------------------------------------------
    # The generating side (core.role) is marked "- you": a recipient-built
    # receipt is the payee's copy, an issuer-built one the payer's.
    payer_label = "FROM (payer - you)" if core.role == "issuer" else "FROM (payer)"
    payee_label = "TO (payee - you)" if core.role == "recipient" else "TO (payee)"

    doc.sections.append(ReceiptSection(
        _identity_lines(core.payer, payer_detail, payer_label)
    ))

    doc.sections.append(ReceiptSection(
        _identity_lines(core.payee, payee_detail, payee_label)
    ))

    # --- transaction ------------------------------------------------------
    doc.sections.append(ReceiptSection(
        [StyledLine("TRANSACTION", emphasis=True)] + _txn_lines(core.txn, core.type, core.chainid, txn_detail)
    ))

    # --- optional notes ---------------------------------------------------
    if extra_notes:
        doc.sections.append(ReceiptSection(
            [StyledLine("NOTES", emphasis=True)] + _notes_lines(extra_notes)
        ))

    # --- verification ------------------------------------------------------
    doc.sections.append(ReceiptSection(
        [StyledLine("VERIFICATION", emphasis=True)]
        + _verify_lines(core, rid, payload_text, verify_detail)
    ))

    # --- footer ------------------------------------------------------------
    doc.sections.append(ReceiptSection([
        StyledLine("a record, not a bearer instrument", align="center", size="small"),
        StyledLine("github.com/pjkundert/alberta-buck", align="center", size="small"),
    ]))

    return doc


__all__ = [
    "Detail",
    "StyledLine", "ReceiptSection", "ReceiptDoc",
    "Driver", "TextDriver",
    "render_receipt",
]
