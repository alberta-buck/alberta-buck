"""Load a compiled kernel out of this package under a chosen module name.

The wheel ships two objects, not four: buck-identity, buck-wallet and
buck-registry compile into ONE cdylib with three ``#[pymodule]`` entry
points, and CPython derives the init symbol (``PyInit_buck_wallet``) from
the module name it is handed, not from the filename.  So the same file is
loaded three times under three names, and the wheel is 2.0 MB instead of
4.9 MB.

Because the file is found by explicit path rather than by scanning
sys.path, its name is ours to choose -- but the *suffix* still has to be
one the platform's dynamic loader will accept, and that differs: .pyd on
Windows, .so everywhere else.  Both are probed rather than computed from
sys.platform, so a wheel built on one and tested on another fails with a
clear message instead of a missing-file traceback.
"""

import importlib.util
import sys
from importlib.machinery import ExtensionFileLoader
from pathlib import Path

_SUFFIXES = (".abi3.so", ".abi3.pyd")


def load(module_name: str, stem: str):
    """Import `stem`'s object as `module_name` and install it in sys.modules.

    Returns the module.  Raises ImportError when the object is absent --
    which is what the backend selector in alberta_buck.wallet._kernel
    catches to fall back to pure Python, so it must stay an ImportError
    and not become a FileNotFoundError.
    """
    here = Path(__file__).parent
    for suffix in _SUFFIXES:
        so = here / f"{stem}{suffix}"
        if so.exists():
            break
    else:
        raise ImportError(
            f"alberta-buck-kernel is installed but carries no compiled "
            f"{stem} object (looked for "
            f"{', '.join(stem + s for s in _SUFFIXES)} in {here}). "
            f"The wheel is broken; reinstall it, or use the pure-Python "
            f"path with BUCK_IDENTITY_BACKEND=py.")

    spec = importlib.util.spec_from_file_location(
        module_name, str(so), loader=ExtensionFileLoader(module_name, str(so)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[module_name] = mod
    return mod
