# alberta-buck-kernel

The compiled Alberta Buck kernels for Python: integer monetary arithmetic
bit-identical to the Solidity contracts, and the BN254 identity, wallet and
registry cryptography.

**Nothing requires this package.** It is the optional fast path behind
[`alberta-buck-core`](https://pypi.org/project/alberta-buck-core/) and the
`alberta-buck` reference implementation. Everything here has a pure-Python
equivalent in `alberta_buck.wallet`, which remains the executable
specification -- this package is proven bit-identical to it by golden
vectors, so choosing it cannot change a single emitted byte. It is only
faster.

```sh
pip install alberta-buck-core            # pure Python, works everywhere
pip install alberta-buck-core[kernel]    # adds this, where a wheel exists
```

`alberta-buck` itself (not yet on PyPI) takes the kernel the same way, as
its own `kernel` extra.

## Selection

At call time, via `BUCK_IDENTITY_BACKEND`:

| value | behaviour |
|---|---|
| unset | use the kernel when importable, else pure Python |
| `kernel` | require the kernel; `ImportError` if absent |
| `py` | force the pure-Python path |

## One wheel per platform

The extension is built `abi3-py311`, so a single wheel serves every CPython
from 3.11 onward -- no rebuild for 3.13, 3.14 and beyond.

Three of the four modules share one shared object: buck-identity,
buck-wallet and buck-registry compile into a single cdylib with three
`#[pymodule]` entry points. Shipping them separately would triple 1.5 MB
for nothing, so each module loads the same file under its own name --
CPython derives the init symbol (`PyInit_buck_wallet`) from the last dotted
component, not from the filename. Installed size is 2.0 MB rather than
4.9 MB.

## Status

0.2.0, prototype. Unaudited cryptographic software. It has been checked for
agreement with a reference implementation, which is not the same as having
been checked for security.

## Licence

CAL-1.0 (Cryptographic Autonomy License v1.0). Beyond the usual copyleft,
CAL requires that anyone you provide this software's functionality to
receives their own data and is not locked out of it by cryptographic or
technical means. See
[LICENSING.md](https://github.com/alberta-buck/alberta-buck/blob/master/LICENSING.md).

Repository: <https://github.com/alberta-buck/alberta-buck>
