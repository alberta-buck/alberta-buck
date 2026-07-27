# buck_identity: the shared identity/wallet/registry cdylib, loaded under this name.
from buck_kernel._loader import load

load(__name__, "_kernel")
