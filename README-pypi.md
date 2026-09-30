# alberta-buck

The Alberta Buck reference implementation in Python, protocol v2: the
identity layer, the Notes, the receipts, and the agent simulations that run
them against the real contracts.

- **`alberta_buck.wallet`** -- the executable specification: Pointcheval-Sanders
  identity credentials and the A' hiding registration presentation, ElGamal
  account credentials, the sigma protocols, the Notes flows (A1 and A2
  addressed notes, B1 bearer notes: commitments, deliveries, the folded
  deposit gates' witnesses), and AB-RCPT/2 payment receipts.  The compiled
  kernels (`alberta-buck-kernel`, in Rust) are proven bit-identical to it.
- **`alberta_buck.registry`** -- the identity accumulator: salted Poseidon
  trees, registry certificates, the aggregator, attribute proofs, the
  insurance regulator, and particulars certificates for details that change.
- **`alberta_buck.sim`** -- agent simulations over the contracts, on anvil or
  in-process (pyrevm): the BUCK_K PID controller, the baskets, the equilibrium
  experiments (`sim/experiments/*.toml`), and the Notes privacy world.
- **`alberta_buck.oracle`** -- Chainlink price-history readers (against a fork).

**Unaudited prototype software for a monetary system.**  It is published so
the research can be reproduced and built on -- not to hold anything of value.

## Install

```sh
pip install alberta-buck               # pure Python; installs everywhere
pip install alberta-buck[kernel]       # + the compiled kernels, where a wheel exists
pip install alberta-buck[sim]          # everything: kernel, pyrevm, plots, server
```

| extra    | adds                                   |
|----------|----------------------------------------|
| `kernel` | the Rust kernels (same bytes, faster)  |
| `fast`   | `pyrevm`, the in-process EVM backend   |
| `plots`  | `matplotlib`, for the `sim.plot_*` renderers |
| `server` | `websockets`, for `sim.server`         |
| `sim`    | all of the above                       |

The extras never change a result, only how fast it arrives or what it can
draw: every heavy dependency is imported where it is used.

## Use

```python
from alberta_buck.wallet import backend, identity_scalar, ps_keygen, ps_sign, ps_verify

issuer = ps_keygen()                                   # a credential issuer's key pair
m = identity_scalar({"registry": "ab", "person": "0001", "birth": "1970-01-01"})
sigma = ps_sign(issuer, m)                             # the issuer certifies the identity
print(backend(), ps_verify(issuer.pk_X, issuer.pk_Y, sigma, m))   # "kernel True" / "py True"
```

`BUCK_IDENTITY_BACKEND=py` forces the pure-Python path; `kernel` requires the
compiled one.

## The Notes use a DEVELOPMENT trusted setup

The simulations deploy the contracts from
[`alberta-buck-contracts`](https://pypi.org/project/alberta-buck-contracts/),
whose Groth16 verifiers come, **until v1.0.0, from a development trusted setup
whose toxic waste is public: anyone can forge a proof they accept.**  That is
deliberate -- it lets a simulation model forged-proof attacks and the
defences around them -- and it means nothing here may guard value.  The
real-proof fixture worlds shipped under `alberta_buck/test/vectors` were
proven with that setup.

## What needs a repository checkout

- New proofs: the circuits and proving keys live in the repository
  (`circuits/`, `make nix-snark-*`), not in this package.
- Worlds routed through Uniswap: Uniswap's contracts are not redistributed
  here, and those worlds load them from a checkout.
- The papers: executable Org documents in the repository.

## Status

0.3.0, prototype, protocol v2.  Unaudited.

## Licence

GPL-3.0-or-later.  The kernels and core platform it can use are CAL-1.0; see
[LICENSING.md](https://github.com/alberta-buck/alberta-buck/blob/master/LICENSING.md).

Repository: <https://github.com/alberta-buck/alberta-buck>
