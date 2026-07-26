"""buck-identity kernel backend selector.

The wallet's curve arithmetic (bn254.add/mul/neg over G1), Poseidon, and
the pairing-based verifiers dispatch to the compiled kernel
(``buck_core.buck_identity``, built by ``make nix-core-build-py``) when it
is importable.  The sigma-protocol orchestration -- transcripts, nonce
draws, response arithmetic -- stays in the Python modules, which remain
the executable spec: the kernel is proven bit-identical to them by
``core/vectors/identity-kernel-vectors.json``, so flipping the backend
cannot change a single emitted byte (``test_kernel_backend.py`` holds the
gate).

Backend selection, checked at call time:

* ``BUCK_IDENTITY_BACKEND=py``     -- force the pure-Python py_ecc path
  (what ``kernel_vectors.py`` sets while emitting the reference vectors).
* ``BUCK_IDENTITY_BACKEND=kernel`` -- require the binding; ImportError if
  it is not built.
* unset                            -- kernel when importable, else py.
"""

from __future__ import annotations

import os

_kernel = None
_checked = False
_kernel_wallet = None
_checked_wallet = False
_kernel_registry = None
_checked_registry = False


def _mode() -> str:
    return os.environ.get("BUCK_IDENTITY_BACKEND", "").strip().lower()


def kernel():
    """The ``buck_core.buck_identity`` module, or ``None`` (py path)."""
    global _kernel, _checked
    mode = _mode()
    if mode == "py":
        return None
    if not _checked:
        _checked = True
        try:
            import buck_core.buck_identity as _k
            _kernel = _k
        except ImportError:
            _kernel = None
    if mode == "kernel" and _kernel is None:
        raise ImportError(
            "BUCK_IDENTITY_BACKEND=kernel but buck_core.buck_identity is not"
            " built (make nix-core-build-py)"
        )
    return _kernel


def kernel_wallet():
    """The ``buck_core.buck_wallet`` module, or ``None`` (py path).

    Same selector, same env var: the three kernel modules are one
    compiled artifact, so they are present or absent together.
    """
    global _kernel_wallet, _checked_wallet
    mode = _mode()
    if mode == "py":
        return None
    if not _checked_wallet:
        _checked_wallet = True
        try:
            import buck_core.buck_wallet as _k
            _kernel_wallet = _k
        except ImportError:
            _kernel_wallet = None
    if mode == "kernel" and _kernel_wallet is None:
        raise ImportError(
            "BUCK_IDENTITY_BACKEND=kernel but buck_core.buck_wallet is not"
            " built (make nix-core-build-py)"
        )
    return _kernel_wallet


def kernel_registry():
    """The ``buck_core.buck_registry`` module, or ``None`` (py path)."""
    global _kernel_registry, _checked_registry
    mode = _mode()
    if mode == "py":
        return None
    if not _checked_registry:
        _checked_registry = True
        try:
            import buck_core.buck_registry as _k
            _kernel_registry = _k
        except ImportError:
            _kernel_registry = None
    if mode == "kernel" and _kernel_registry is None:
        raise ImportError(
            "BUCK_IDENTITY_BACKEND=kernel but buck_core.buck_registry is not"
            " built (make nix-core-build-py)"
        )
    return _kernel_registry


def kernel_active() -> bool:
    """True iff wallet crypto is dispatching to the compiled kernel."""
    return kernel() is not None


def backend() -> str:
    """``"kernel"`` or ``"py"`` -- what a call right now would use."""
    return "kernel" if kernel_active() else "py"
