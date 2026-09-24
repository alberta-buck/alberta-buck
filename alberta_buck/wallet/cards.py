"""Payload anatomy cards: what a payload holds, where each part came from, who can read it.

A card lists a payload's fields.  Each field carries a value (shortened), its
SOURCE (where the value came from) and its PROTECTION -- the one thing a reader
most needs and a hex dump never shows:

  PUBLIC   anyone can read it, Mallory included
  PRIVATE  known to the named parties only; never published
  SEALED   encrypted: only the named reader can open it
  SECRET   never leaves the holder, not even inside a proof
  PROVEN   established by a proof without being shown

Two drivers: :meth:`Card.text` (ASCII, for plain-text contexts and tests) and
:meth:`Card.png` (matplotlib, the palette of the privacy paper's flow figures).
The builders at the bottom make cards from wallet objects.

Used by alberta-buck-privacy.org.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from alberta_buck.wallet.bn254 import point_to_words

PROTECTIONS = ("public", "private", "sealed", "secret", "proven")

INK, MUTED, TEAL, AMBER, INDIGO, GREEN = (
    "#162c3b", "#596c79", "#006c67", "#9a520c", "#3b4a8c", "#2f6b2f")
_STYLE = {
    "public":  ("#edf1f5", INK),
    "private": ("#eef0fb", INDIGO),
    "sealed":  ("#e3f1ef", TEAL),
    "secret":  ("#fff1e0", AMBER),
    "proven":  ("#e8f3e8", GREEN),
}


@dataclass(frozen=True)
class Field:
    label:                      str
    value:                      str
    protection:                 str       # one of PROTECTIONS
    who:                        str = ""  # the reader (sealed), holder (secret) or parties (private)
    source:                     str = ""  # where the value came from, or what it proves

    def badge(self, arrow: str = "->") -> str:
        p = self.protection.upper()
        if self.protection == "sealed" and self.who:
            return f"{p} {arrow} {self.who}"
        if self.who and self.protection in ("secret", "private"):
            return f"{p}: {self.who}"
        return p


@dataclass
class Card:
    title:                      str
    holder:                     str = ""  # who holds or built the payload
    fields:                     List[Field] = field(default_factory=list)
    note:                       str = ""

    def add(self, *fields: Field) -> "Card":
        self.fields.extend(fields)
        return self

    # -- drivers ----------------------------------------------------------------------------------

    def text(self, width: int = 100) -> str:
        head                    = f"{self.title}" + (f"  ({self.holder})" if self.holder else "")
        out                     = [head, "-" * min(width, max(len(head), 40))]
        bw                      = max([len(f.badge()) for f in self.fields] + [6])
        lw                      = max([len(f.label) for f in self.fields] + [5])
        vw                      = max([len(f.value) for f in self.fields] + [5])
        for f in self.fields:
            line = f"[{f.badge():<{bw}}] {f.label:<{lw}}  {f.value:<{vw}}"
            if f.source:
                line += f"  <- {f.source}"
            out.append(line[:width].rstrip())
        if self.note:
            out.append(f"  {self.note}"[:width])
        return "\n".join(out)

    def png(self, path: str, dpi: int = 200) -> str:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch

        rows                    = len(self.fields)
        H                       = 70 + 46 * rows + (34 if self.note else 12)
        fig, ax                 = plt.subplots(figsize=(12, 12 * H / 1200), dpi=dpi)
        fig.subplots_adjust(0, 0, 1, 1)
        ax.set(xlim=(0, 1200), ylim=(H, 0))
        ax.axis("off")
        box = lambda x, y, w, h, face, edge, lw=1: ax.add_patch(FancyBboxPatch(
            (x, y), w, h, boxstyle="round,pad=0,rounding_size=6", facecolor=face,
            edgecolor=edge, linewidth=lw))
        box(6, 6, 1188, H - 12, "#ffffff", "#cad7dc", 1.2)
        box(6, 6, 1188, 50, "#eef6f5", "#cad7dc", 1.2)
        ax.text(24, 31, self.title, fontsize=17, fontweight="bold", color=TEAL, va="center",
                family="DejaVu Sans")
        if self.holder:
            ax.text(1176, 31, self.holder.replace(" -> ", " \u2192 "), fontsize=13, color=MUTED, va="center", ha="right",
                    family="DejaVu Sans")
        for n, f in enumerate(self.fields):
            y = 84 + 46 * n
            if n:
                ax.plot([20, 1180], [y - 23, y - 23], color="#e4ebee", linewidth=0.8)
            face, color = _STYLE[f.protection]
            box(18, y - 15, 212, 30, face, face)
            badge = _fit(f.badge("→"), 28)
            ax.text(124, y, badge, fontsize=min(11.5, 11.5 * 19 / max(len(badge), 1)),
                    color=color, fontweight="bold", ha="center", va="center",
                    family="DejaVu Sans")
            ax.text(244, y, _fit(f.label, 26), fontsize=14, color=INK, fontweight="bold",
                    va="center", family="DejaVu Sans")
            ax.text(510, y, _fit(f.value, 33), fontsize=12.5, color=INK, va="center",
                    family="DejaVu Sans Mono")
            if f.source:
                ax.text(840, y, _fit(f.source, 44), fontsize=11.5, color=MUTED, va="center",
                        family="DejaVu Sans")
        if self.note:
            ax.text(24, H - 26, self.note, fontsize=12, color=MUTED, style="italic",
                    va="center", family="DejaVu Sans")
        fig.savefig(path, facecolor="white", metadata={"Software": None})
        plt.close(fig)
        return path


def _fit(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 2] + ".."


# ---- value shorteners ---------------------------------------------------------------------------

def short_int(v: int, keep: int = 4) -> str:
    h = f"{v:x}"
    return f"0x{h}" if len(h) <= 2 * keep + 2 else f"0x{h[:keep]}..{h[-keep:]}"


def short_point(P) -> str:
    x, y = point_to_words(P)
    return f"({short_int(x, 3)}, {short_int(y, 3)})"


def short_ct(E) -> str:
    """A ciphertext by the x-coordinates of its two points: enough to tell two apart."""
    return f"R {short_int(point_to_words(E.R)[0], 3)}  C {short_int(point_to_words(E.C)[0], 3)}"


def short_addr(a) -> str:
    h = a if isinstance(a, str) else f"0x{a:040x}"
    return f"{h[:6]}..{h[-4:]}"


def buck(v: int, decimals: int = 6) -> str:
    return f"{v / 10 ** decimals:,.2f} BUCK"


def bearer_code(rho: int) -> str:
    """The bearer secret as a human-transcribable code (base32, grouped)."""
    s = base64.b32encode(rho.to_bytes(32, "big")).decode().rstrip("=")
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))


# ---- builders -----------------------------------------------------------------------------------

def identity_record(fields: dict, holder: str, issuer: str = "issuer") -> Card:
    """The core record: M's preimage, fixed at first certification."""
    c = Card("CORE IDENTITY RECORD", f"held by {holder}",
             note="Fixed at first certification: the identity point M is its hash, for life.")
    for k, v in fields.items():
        c.add(Field(k, str(v), "private", f"{holder} + {issuer}", "certified at enrolment"))
    return c


def credential(cred, holder: str, issuer_name: str = "Alberta's issuer") -> Card:
    return Card("CREDENTIAL", f"issued to {holder}",
                note="The raw signature never leaves the wallet; only masked presentations do.").add(
        Field("identity scalar m", short_int(cred.m), "private", f"{holder} + issuer",
              "hash of the core record"),
        Field("signature", f"({short_point(cred.sigma.sigma_1)}, ..)", "secret", holder,
              f"{issuer_name}'s PS signature on m"),
        Field("issuer key", "published", "public", "", "trusted on chain by governance"),
    )


def particulars(p, holder: str, disclosed: Iterable[str] = ()) -> Card:
    disclosed                   = set(disclosed)
    cert                        = p.certificate
    c                           = Card(f"PARTICULARS v{cert.version}", f"held by {holder}",
                                       note="Signed over M.  A change is a new version; M and everything bound to it stay.")
    for name in sorted(p.values):
        if name in disclosed:
            c.add(Field(name, p.values[name], "public", "", "disclosed; opens its commitment"))
        else:
            c.add(Field(name, short_int(cert.digests[name]), "private", holder,
                        "commitment only; value withheld"))
    return c


def registration(pkg, holder: str) -> Card:
    a = pkg.account
    return Card("REGISTRATION PACKAGE", f"built by {holder}'s wallet",
                note="The proof shows the envelope holds a CERTIFIED identity -- not whose.").add(
        Field("account", short_addr(a.address), "public", "", "a fresh Ethereum key"),
        Field("identity key pk", short_point(pkg.pk), "public", "", "fresh key pair; sk stays"),
        Field("identity envelope E", short_ct(pkg.E), "sealed", holder, "Enc(M, pk): only sk opens it"),
        Field("masked credential", f"A{short_point(pkg.presentation.A)}", "public", "",
              "signature re-masked: unlinkable"),
        Field("registration proof", f"e={short_int(pkg.proof.e)}", "proven", "",
              "certified M; E holds it; holds sk"),
        Field("identity secret sk", "never sent", "secret", holder, "proven, not shown"),
    )


def envelope(env, sender: str, reader: str, amount: int = 0) -> Card:
    return Card("IDENTITY ENVELOPE (approve)", f"{sender} -> {reader}",
                note="Proves: same identity as the sender's registration, sealed for the reader.").add(
        Field("counterparty", short_addr(env.target), "public", "", "the account being approved"),
        Field("allowance", buck(amount), "public", "", "0: identity only, no spending"),
        Field("envelope", short_ct(env.E), "sealed", reader, f"Enc(M_{sender[0]}, pk_{reader[0]})"),
        Field("re-encryption proof", f"e={short_int(env.proof.e)}", "proven", "",
              "same M as the registered envelope"),
    )


def bearer_note(opening, cm: int, issuer: str, holder: str) -> Card:
    return Card("BEARER NOTE (B1)", f"issued by {issuer}, held by {holder}",
                note="Whoever holds the bearer secret can cash it: treat it like cash.").add(
        Field("face value", buck(opening.v), "private", holder, "shown only when cashed"),
        Field("issuer", issuer, "public", "", "authenticated by its batch signature"),
        Field("bearer secret", bearer_code(opening.rho)[:29] + "..", "secret", holder,
              "the opening; never published"),
        Field("commitment", short_int(cm), "public", "", "in the note tree since the mint"),
        Field("spent marker", "not yet shown", "secret", holder, "derived from the secret"),
    )


def delivery(d: dict, flavor: str, recipient: str, issuer: str) -> Card:
    """An addressed note's delivery package: what a courier (or Mallory) could carry."""
    c = Card(f"ADDRESSED DELIVERY ({flavor.upper()})", f"{issuer} -> {recipient}",
             note="Only the recipient's receiving secret opens the sealed fields.")
    from alberta_buck.wallet.bn254 import words_to_point
    from alberta_buck.wallet.elgamal import ElGamalCiphertext
    pt                          = lambda o: words_to_point(int(o["x"]), int(o["y"]))
    ct                          = lambda o: ElGamalCiphertext(pt(o["R"]), pt(o["C"]))
    c.add(Field("value ciphertext", short_ct(ct(d["eNote"])), "sealed", recipient,
                "Enc(value, recipient's mailbox)"))
    if "eIss" in d:
        c.add(Field("issuer ciphertext", short_ct(ct(d["eIss"])), "sealed", recipient,
                    f"Enc({issuer}'s M, mailbox)"))
    if "eRec" in d:
        c.add(Field("recipient ciphertext", short_ct(ct(d["eRec"])), "sealed", recipient,
                    "Enc(recipient's M, mailbox)"))
    if "T" in d:
        c.add(Field("key tie T", short_point(pt(d["T"])), "public", "", "blinded; opens only in proof"))
    names = {"v": "value", "rho": "bearer secret", "rNote": "value randomness",
             "rPrime": "issuer randomness", "gamma": "key-tie blind",
             "saltIss": "issuer's naming salt"}
    wrapped = [k[: -len("Wrapped")] for k in d if k.endswith("Wrapped")]
    for base in sorted(wrapped, key=lambda b: list(names).index(b) if b in names else 99):
        k = base + "Wrapped"
        c.add(Field(names.get(base, base), short_int(int(d[k])), "sealed", recipient,
                    "masked with the mailbox secret"))
    return c


def statement(title: str, public: dict, hidden: dict, holder: str, note: str = "") -> Card:
    """A proof's statement: what it shows (public inputs) and what it proves about (witness)."""
    c = Card(title, f"proved by {holder}", note=note)
    for k, v in public.items():
        c.add(Field(k, v, "public", "", "public input: the chain sees it"))
    for k, v in hidden.items():
        c.add(Field(k, v, "proven", "", "witness: used, never shown"))
    return c


__all__ = ["Card", "Field", "PROTECTIONS", "bearer_code", "bearer_note", "buck", "credential",
           "delivery", "envelope", "identity_record", "particulars", "registration",
           "short_addr", "short_ct", "short_int", "short_point", "statement"]
