# buck_wallet: the shared identity/wallet/registry cdylib, loaded under this name.
import importlib.util
import sys
from importlib.machinery import ExtensionFileLoader
from pathlib import Path

_so = str(Path(__file__).with_name("_kernel.abi3.so"))
_spec = importlib.util.spec_from_file_location(
    __name__, _so, loader=ExtensionFileLoader(__name__, _so))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
sys.modules[__name__] = _mod
