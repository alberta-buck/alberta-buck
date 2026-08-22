"""Agent RNG construction, with an opt-in keyed-hash mode for the JS port.

Default ("" / unset): the historical path -- a Mersenne `random.Random`
seeded from blake2b(seed || class || idx), byte-identical to what
`_agent_rng` and the DirectMint-family setup have always produced.

Keyed mode (`[scenario] rng = "keyed"` in an experiment TOML): agents get
a `KeyedRandom` whose every variate is derived from a blake2b counter
stream -- no Mersenne anywhere -- so a JavaScript implementation can
reproduce the exact draw sequence from the same key recipe.  The recipe
is documented for JS implementors in alberta_buck/sim/TELEMETRY.md
("Keyed RNG"); keep the two in step.

The mode is process-global, set once per run from the scenario (loop.run)
BEFORE any agent setup executes.  It deliberately does not affect the
loop's own world machinery (whale scheduling, identity nonces), which
stays server-side in the port architecture.
"""

from __future__ import annotations

import hashlib
import random

_MODE = ""                      # "" = stdlib Mersenne | "keyed" = KeyedRandom


def set_mode(mode: str | None) -> None:
    global _MODE
    _MODE = mode or ""


def mode() -> str:
    return _MODE


def _key_bytes(seed: int, class_name: str, idx: int) -> bytes:
    """The shared key recipe (identical bytes to the historical seeding):
    seed as 32 big-endian bytes || UTF-8 class name || idx as 8 BE bytes."""
    return (int(seed).to_bytes(32, "big", signed=False)
            + class_name.encode()
            + int(idx).to_bytes(8, "big", signed=False))


class KeyedRandom:
    """A minimal, language-neutral uniform generator.

    key    = blake2b(seed_be32 || utf8(class) || idx_be8, digest_size=16)
    n-th   : h = blake2b(key || n_be8, digest_size=8)   (n = 0, 1, 2, ...)
    float  : u53 = (h as big-endian u64) >> 11;  x = u53 * 2**-53  in [0,1)

    Every supported method consumes exactly ONE stream variate, in call
    order, so a port that makes the same calls in the same order gets the
    same values.  Derived draws use plain float64 arithmetic (exactly
    reproducible in JS):
        uniform(a, b)  = a + (b - a) * x
        randint(a, b)  = a + floor(x * (b - a + 1))     (inclusive)
        randrange(n)   = floor(x * n)
        randrange(a,b) = a + floor(x * (b - a))
        choice(seq)    = seq[randrange(len(seq))]
    Unsupported `random.Random` methods are deliberately absent: a port
    gap fails loudly instead of silently diverging.
    """

    def __init__(self, seed: int, class_name: str, idx: int):
        self._key = hashlib.blake2b(_key_bytes(seed, class_name, idx),
                                    digest_size=16).digest()
        self._n = 0

    def random(self) -> float:
        h = hashlib.blake2b(
            self._key + self._n.to_bytes(8, "big"), digest_size=8).digest()
        self._n += 1
        return (int.from_bytes(h, "big") >> 11) * (2.0 ** -53)

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.random()

    def randint(self, a: int, b: int) -> int:
        return a + int(self.random() * (b - a + 1))

    def randrange(self, a: int, b: int | None = None) -> int:
        if b is None:
            return int(self.random() * a)
        return a + int(self.random() * (b - a))

    def choice(self, seq):
        return seq[self.randrange(len(seq))]


def agent_rng(seed: int, class_name: str, idx: int):
    """Per-agent deterministic RNG keyed off (seed, class, idx) -- Mersenne
    seeded from the key by default, a KeyedRandom stream in keyed mode."""
    if _MODE == "keyed":
        return KeyedRandom(seed, class_name, idx)
    return random.Random(
        int.from_bytes(hashlib.blake2b(_key_bytes(seed, class_name, idx),
                                       digest_size=16).digest(), "big"))
