# alberta-buck-core

The JavaScript side of the Alberta Buck platform: one testing API over every
EVM backend, an agent simulation platform that runs in a browser, and
BigInt-native wrappers around the WebAssembly kernels.

- **`Session`** -- the ChainSession API. Send, call and deploy against Tevm
  in-process, anvil, or a public testnet, with the same code. Every
  state-changing operation declares whether it is expected to succeed or
  revert, so expectation mismatches are first-class results rather than
  surprises.
- **Journal** -- an append-only JSONL record of every operation: tag, op,
  sender, expectation, outcome, gas, tx hash, block, error. Journals make
  runs auditable, diffable across backends *and across languages* (the
  Python platform writes the same schema), and replayable for demos.
- **Agents and worlds** -- `runDays`, whales, round-trip traders, and the
  scenario builders that stand up a pool world.
- **A BUCK world** -- `buckworld.js` deploys the identity and monetary
  stack and runs its ceremonies, whole (`onboard`, `createCredit`) or in
  the halves each party performs: `issueCredential` (the issuer, off-chain)
  and `registerWallet` (the holder); `insureAsset` (the insurer) and
  `activateCredit` (the holder). `observer.js` decodes the chain as the
  public sees it, identity material marked opaque. `snapshotTevm` /
  `restoreTevm` and `codec.js` save a Tevm world as JSON and bring it back,
  clock included.
- **Kernel wrappers** -- `identity.js` and `wallet.js` wrap
  [`alberta-buck-kernel`](https://www.npmjs.com/package/alberta-buck-kernel)
  in a BigInt-native API; `identity-web.js` and `wallet-web.js` do the same
  for the browser, taking an explicit wasm source because bundlers break
  wasm-bindgen's default relative fetch.

## Install

```sh
npm install alberta-buck-core
```

Needs npm 11 or later (Node 24 bundles it): npm 10's installer crashes on
this package's dependency graph.

## Use

```js
import { tevmSession, runDays } from "alberta-buck-core";

const session = await tevmSession();          // or anvilSession(...)
// ... deploy contracts, then let agents run simulated days
```

```js
import identity from "alberta-buck-core/identity";

const scalar = identity.reduceModOrder(12345n);
```

Subpaths map onto the modules directly: `alberta-buck-core/session`,
`/journal`, `/identity`, `/wallet`, `/buckworld`, `/observer`, `/v3`, and
so on.

The Alberta Buck contracts come from
[`alberta-buck-contracts`](https://www.npmjs.com/package/alberta-buck-contracts),
a dependency: `loadArtifact` (`alberta-buck-core/nodefs`) reads it outside a
repository checkout. Third-party contracts (Uniswap, WETH9) are not bundled;
they come from their own packages.

## Status

0.2.1, prototype. Unaudited software for a monetary system, published so the
simulation and identity work can be reproduced and built on -- not for
custody of anything real.

## Licence

CAL-1.0 (Cryptographic Autonomy License v1.0). Beyond the usual copyleft,
CAL requires that anyone you provide this software's functionality to
receives their own data and is not locked out of it by cryptographic or
technical means. See
[LICENSING.md](https://github.com/alberta-buck/alberta-buck/blob/master/LICENSING.md).

Repository: <https://github.com/alberta-buck/alberta-buck>
