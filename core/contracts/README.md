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
BUCK world -- the Python simulation, the JavaScript simulation, the browser
demos, the test suites -- deploys fresh into its own EVM and learns the
addresses from the deploy receipts. What this package ships is `(abi,
bytecode)` pairs.

`deployments` is therefore empty. It exists so that publishing real addresses
later is not a breaking change to the package's shape.

## Install

```sh
npm install alberta-buck-contracts
```

```sh
pip install alberta-buck-contracts
```

## Use

```js
import { artifact, compiler } from "alberta-buck-contracts";

const { abi, bytecode } = artifact("Buck");
console.log(compiler.solc);        // the exact compiler that produced it
```

```python
from buck_contracts import artifact, compiler

abi, bytecode = artifact("Buck")
print(compiler()["solc"])
```

## What is not here

A BUCK world also needs Uniswap and WETH. Those are **not** bundled: they
belong to Uniswap, under their own licences, and you should take them from
their own published packages -- `@uniswap/v3-core`, `@uniswap/v2-periphery`,
`@uniswap/universal-router`. Asking this package for one of them raises an
error naming the package to install instead. `compiler.external` lists the
mapping.

## Reproducibility

The build is pinned and verified rather than assumed. `compiler.json` records
the exact solc version, the optimizer settings, the EVM version, the git
commit, and a sha256 over the bundle. The build asserts that every artifact
was produced by the pinned compiler, and that only contracts we own are
included.

## Licence

GPL-3.0-or-later, matching the Solidity sources. See
[LICENSING.md](https://github.com/alberta-buck/alberta-buck/blob/master/LICENSING.md).

Repository: <https://github.com/alberta-buck/alberta-buck>
