"""Tests for the receipt renderer: Detail levels, TextDriver output shape,
and extensibility (custom driver, custom sections)."""

from __future__ import annotations

import pytest

from alberta_buck.wallet.render import (
    Detail, StyledLine, ReceiptSection, ReceiptDoc,
    TextDriver, Driver, render_receipt,
)
from alberta_buck.wallet.envelope import deserialize_core, parse_envelope
from alberta_buck.wallet.vectors import build_vectors


@pytest.fixture(scope="module")
def vectors():
    return build_vectors()


def _core(vectors, kind="eoa_pub"):
    env = vectors["abrcpt"][kind]["envelope"]
    return deserialize_core(parse_envelope(env))


# ---- Detail levels: each renders without error ---------------------------

@pytest.mark.parametrize("level", [Detail.MINIMAL, Detail.NAME, Detail.FULL])
def test_detail_levels_render(vectors, level):
    core = _core(vectors, "eoa_pub")
    doc = render_receipt(core, payer_detail=level, payee_detail=level,
                         txn_detail=level, verify_detail=level)
    text = TextDriver(48).render(doc)
    assert len(text) > 0


@pytest.mark.parametrize("level", [Detail.MINIMAL, Detail.NORMAL, Detail.FULL])
def test_txn_detail_levels(vectors, level):
    core = _core(vectors, "eoa_pub")
    doc = render_receipt(core, txn_detail=level)
    text = TextDriver(48).render(doc)
    if level == Detail.FULL:
        assert "Tx  " in text
    if level == Detail.MINIMAL:
        assert "When" not in text


# ---- Identity display: MINIMAL shows M fingerprint, NAME shows name -------

def test_minimal_shows_fingerprint(vectors):
    core = _core(vectors, "eoa_pub")
    doc = render_receipt(core, payer_detail=Detail.MINIMAL)
    text = TextDriver(48).render(doc)
    assert "idpt " in text            # M fingerprint line
    assert "Alice" not in text        # no human name


def test_name_shows_human_name(vectors):
    core = _core(vectors, "eoa_pub")
    doc = render_receipt(core, payer_detail=Detail.NAME)
    text = TextDriver(48).render(doc)
    assert "Alice Johnson" in text
    assert "Alberta Identity Card" in text


def test_full_shows_all_fields(vectors):
    core = _core(vectors, "eoa_pub")
    doc = render_receipt(core, payer_detail=Detail.FULL)
    text = TextDriver(48).render(doc)
    assert "id_number" in text or "AIC-2026" in text
    # Full M point coordinates
    assert "M.x " in text or "M.x" in text


# ---- TextDriver output shape ----------------------------------------------

def test_text_driver_header_footer(vectors):
    core = _core(vectors, "eoa_pub")
    text = TextDriver(48).render(render_receipt(core))
    assert "ALBERTA  BUCK" in text
    assert "P A Y M E N T   R E C E I P T" in text
    assert "a record, not a bearer instrument" in text


def test_text_driver_separators(vectors):
    core = _core(vectors, "eoa_pub")
    text = TextDriver(48).render(render_receipt(core))
    # Separators are hyphens at section boundaries
    assert "---" in text


def test_text_driver_width_respected(vectors):
    core = _core(vectors, "eoa_pub")
    text64 = TextDriver(64).render(render_receipt(core))
    text32 = TextDriver(32).render(render_receipt(core))
    # Separators and centered lines fill the configured width
    sep64 = [l for l in text64.split("\n") if l.startswith("===") or l.startswith("---")]
    sep32 = [l for l in text32.split("\n") if l.startswith("===") or l.startswith("---")]
    assert any(len(l) >= 64 for l in sep64)
    assert any(len(l) == 32 for l in sep32)


# ---- All receipt kinds (both Note parties) render without error -----------

ALL_KINDS = ["eoa_pub", "eoa_priv",
             "note_b1", "note_a1", "note_a2",
             "note_b1_issuer", "note_a1_issuer", "note_a2_issuer",
             "eoa_pub_unicode"]


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_all_kinds_render(vectors, kind):
    core = _core(vectors, kind)
    doc = render_receipt(core)
    text = TextDriver(48).render(doc)
    assert len(text) > 0
    assert "ALBERTA  BUCK" in text
    # A2 in the vectors carries the mint's issuer binding -> VALID.
    if kind.startswith("note_a2"):
        assert "UNVERIFIED" not in text
        assert "VALID" in text


# ---- Role marker: the generating side is labelled "- you" -----------------

@pytest.mark.parametrize("kind", ["note_b1", "note_a1", "note_a2"])
def test_recipient_role_marks_payee(vectors, kind):
    text = TextDriver(48).render(render_receipt(_core(vectors, kind)))
    assert "TO (payee - you)" in text
    assert "FROM (payer)" in text


@pytest.mark.parametrize("kind", ["note_b1_issuer", "note_a1_issuer", "note_a2_issuer"])
def test_issuer_role_marks_payer(vectors, kind):
    text = TextDriver(48).render(render_receipt(_core(vectors, kind)))
    assert "FROM (payer - you)" in text
    assert "TO (payee)" in text


# ---- Negative: bad detail values ------------------------------------------

def test_detail_enum_values():
    # Just confirming the enum members exist
    assert Detail.MINIMAL.value == "minimal"
    assert Detail.NAME.value == "name"
    assert Detail.NORMAL.value == "normal"
    assert Detail.FULL.value == "full"


# ---- 6dp amount formatting -------------------------------------------------

def test_amount_six_decimal_places(vectors):
    core = _core(vectors, "eoa_pub")
    text = TextDriver(48).render(render_receipt(core))
    # 500.000000, not 500.00
    assert "500.000000" in text


# ---- Extensibility: custom driver ------------------------------------------

class _UpperDriver(Driver):
    """Toy driver: renders everything uppercase, single newline between sections."""
    def render(self, doc: ReceiptDoc) -> str:
        out = []
        for sec in doc.sections:
            if sec.separator and out:
                out.append("---")
            for line in sec.lines:
                out.append(line.text.upper())
        return "\n".join(out)


def test_custom_driver_works(vectors):
    core = _core(vectors, "eoa_pub")
    doc = render_receipt(core, payer_detail=Detail.MINIMAL)
    text = _UpperDriver().render(doc)
    assert "ALBERTA  BUCK" in text
    assert "FROM (PAYER)" in text


# ---- Extensibility: building a ReceiptDoc by hand --------------------------

def test_manual_document_build():
    doc = ReceiptDoc()
    doc.sections.append(ReceiptSection([
        StyledLine("CUSTOM HEADER", align="center", emphasis=True),
    ], separator=False))
    doc.sections.append(ReceiptSection([
        StyledLine("line 1"),
        StyledLine("line 2"),
    ]))
    text = TextDriver(48).render(doc)
    assert "CUSTOM HEADER" in text
    assert "line 1" in text
    assert "line 2" in text
    assert "---" in text  # separator between sections


# ---- Amount formatting: small value shows all 6dp --------------------------

def test_small_amount_shows_full_precision(vectors):
    core = _core(vectors, "note_b1")
    text = TextDriver(48).render(render_receipt(core))
    # The B1 test note has face=250 base units = 0.000250 BUCK
    assert ".000250" in text or "  0.000250" in text


# ---- Golden-file tests: exact rendered output (deterministic vectors) ------
# Golden files live at alberta_buck/test/vectors/receipt-<kind>.golden.txt —
# the exact default-detail render (NAME, NORMAL, NORMAL) at 48 columns, for
# all eight kinds (five recipient-side + three issuer-side Note receipts).
# Any intentional change to receipt layout requires regenerating them via:
#   make nix-golden-receipts
from pathlib import Path as _Path
_VECTORS = _Path(__file__).resolve().parent / "vectors"


def _golden(kind: str) -> str:
    return (_VECTORS / f"receipt-{kind}.golden.txt").read_text()


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_golden_render(vectors, kind):
    """Default-detail render must match the golden file byte-for-byte."""
    core = _core(vectors, kind)
    doc = render_receipt(core)
    text = TextDriver(48).render(doc)
    expected = _golden(kind)
    assert text == expected, f"{kind}: render output differs from golden file"


def test_golden_render_deterministic(vectors):
    """Two renders of the same core produce identical output."""
    core = _core(vectors, "eoa_pub")
    text1 = TextDriver(48).render(render_receipt(core))
    text2 = TextDriver(48).render(render_receipt(core))
    assert text1 == text2
    assert len(text1) > 0
