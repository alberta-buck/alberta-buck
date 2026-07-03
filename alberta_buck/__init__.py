# Alberta Buck -- commodity-basket stablecoin tools

# Bootstrap shim: when running from a repo checkout, make the in-repo
# core/python packages (buck_core) importable before any installable wheel
# exists.  Appended (not prepended) so an installed buck-core wheel wins.
# Removed once buck-core ships as a wheel (alberta-buck-platform.org,
# Phase 2).
import sys as _sys
from pathlib import Path as _Path

_core = _Path(__file__).resolve().parent.parent / "core" / "python"
if _core.is_dir() and str(_core) not in _sys.path:
    _sys.path.append(str(_core))
del _sys, _Path, _core
