"""alberta-buck-kernel -- the compiled Alberta Buck kernels for Python.

The optional fast path behind ``alberta_buck``.  Everything here has a
pure-Python equivalent in ``alberta_buck.wallet``, which remains the
executable specification; this package is proven bit-identical to it by
``core/vectors/*-kernel-vectors.json``, so selecting it cannot change a
single emitted byte.

Nothing needs this package.  ``pip install alberta-buck`` works without it
and computes the same answers more slowly; ``pip install alberta-buck[kernel]``
adds it.  Selection happens at call time, via BUCK_IDENTITY_BACKEND:

    py      force the pure-Python path
    kernel  require this package (ImportError if absent)
    unset   use this package when importable, else pure Python

One shared object serves three of the four modules: buck-identity,
buck-wallet and buck-registry are compiled into a single cdylib with three
``#[pymodule]`` entry points, so shipping them separately would triple
1.5 MB for nothing.  Each module below loads that one file under its own
name -- CPython derives the init symbol (``PyInit_buck_wallet``) from the
last dotted component, not from the filename.
"""

__all__ = ["buck_math", "buck_identity", "buck_wallet", "buck_registry"]
