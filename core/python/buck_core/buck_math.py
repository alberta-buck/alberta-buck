"""Compatibility shim: ``buck_core.buck_math`` -> ``buck_kernel.buck_math``.

The compiled kernels moved out of this package so that alberta-buck-core
can be a pure-Python wheel -- otherwise every consumer of the session and
journal API would drag the whole Rust wheel matrix behind it.  They now
ship separately as alberta-buck-kernel.

Callers keep saying ``import buck_core.buck_math``.  In a repo checkout the
built ``buck_math.so`` sits beside this file and CPython prefers the
extension, so a freshly rebuilt kernel wins.  Installed from wheels there
is no .so here and this shim delegates.  When alberta-buck-kernel is not
installed the ImportError is exactly what the backend selector in
alberta_buck.wallet._kernel expects: it falls back to pure Python.
"""

import sys

from buck_kernel import buck_math as _mod

sys.modules[__name__] = _mod
