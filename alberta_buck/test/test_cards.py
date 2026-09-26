"""Payload anatomy cards: every field carries a protection, and both drivers render."""

import pytest

from alberta_buck.sim.privacy_world import PrivacyWorld
from alberta_buck.wallet import cards


@pytest.fixture(scope="module")
def world():
    return PrivacyWorld.load()


def test_badges_name_the_reader():
    f = cards.Field("envelope", "R 0x1..2", "sealed", "Carol")
    assert f.badge() == "SEALED -> Carol"
    assert cards.Field("sk", "-", "secret", "Bob").badge() == "SECRET: Bob"
    assert cards.Field("root", "0x1", "public").badge() == "PUBLIC"


def test_delivery_card_seals_every_wrapped_field(world):
    d                           = world.notes["a2"].delivery
    card                        = cards.delivery(d, "a2", "Carol", "Bob")
    wrapped                     = [k for k in d if k.endswith("Wrapped")]
    sealed                      = [f for f in card.fields if f.protection == "sealed"]
    assert len(sealed) == len(wrapped) + 2          # plus the two ciphertexts
    assert all(f.protection in cards.PROTECTIONS for f in card.fields)
    assert "SEALED -> Carol" in card.text()


def test_bearer_note_card_and_png(world, tmp_path):
    b1                          = world.notes["b1"]
    card                        = cards.bearer_note(b1.opening, b1.cm, "Aspen Mutual Credit Union", "Bob")
    assert "100.00 BUCK" in card.text()
    assert cards.bearer_code(b1.opening.rho).count("-") >= 10
    out = card.png(str(tmp_path / "note.png"))
    assert (tmp_path / "note.png").stat().st_size > 10_000 and out.endswith("note.png")
