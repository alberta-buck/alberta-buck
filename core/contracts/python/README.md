# alberta-buck-contracts

Compiled ABI and bytecode for the Alberta Buck contracts: the BUCK token
(`Buck`), the insured asset NFT (`BuckCredit`), the on-chain PID controller
(`BuckKControllerDirect`), the identity accumulator (`IdentityRegistry`), the
two baskets, and the simulation helpers.

**Unaudited prototype software for a monetary system.** These contracts have
never been audited. They are published so the research and simulations can be
reproduced and built on -- not so they can be deployed with other people's
money behind them.

## Nothing here has an address

No Alberta Buck contract is deployed at a fixed address, on any chain. Every
BUCK world deploys fresh into its own EVM and learns the addresses from the
deploy receipts. What this package ships is `(abi, bytecode)` pairs.

`deployments()` is therefore empty. It exists so that publishing real
addresses later is not a breaking change to the package's shape.

## Install

```sh
pip install alberta-buck-contracts
```

## Use

```python
from buck_contracts import artifact, compiler, names

abi, bytecode = artifact("Buck")
print(names())                 # every contract this package ships
print(compiler()["solc"])      # the exact compiler that produced them
```

`buck_core.artifacts.load_artifact` (in `alberta-buck-core`) falls back to
this package when no repository checkout is reachable, which is what lets an
installed `alberta-buck` deploy a world.

## What is not here

A BUCK world also needs Uniswap and WETH. Those are **not** bundled: they
belong to Uniswap, under their own licences, and you should take them from
their own published packages. Asking this package for one of them raises a
`KeyError` naming the package to install instead; `compiler()["external"]`
lists the mapping.

## Reproducibility

The build is pinned and verified rather than assumed. `compiler()` records the
exact solc version, the optimizer settings, the EVM version, the git commit,
and a sha256 over the bundle. The build asserts that every artifact was
produced by the pinned compiler, and that only contracts we own are included.

The same bundle is published to npm as `alberta-buck-contracts`.

## Licence

GPL-3.0-or-later, matching the Solidity sources. See
[LICENSING.md](https://github.com/alberta-buck/alberta-buck/blob/master/LICENSING.md).

Repository: <https://github.com/alberta-buck/alberta-buck>
