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
- **Kernel wrappers** -- `identity.js` and `wallet.js` wrap
  [`alberta-buck-kernel`](https://www.npmjs.com/package/alberta-buck-kernel)
  in a BigInt-native API; `identity-web.js` does the same for the browser,
  taking an explicit wasm source because bundlers break wasm-bindgen's
  default relative fetch.

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
`/journal`, `/identity`, `/wallet`, `/world`, `/v3`, and so on.

Contract artifacts are not bundled here. A world needs the compiled
contracts, which ship separately so that Uniswap's own artifacts come from
Uniswap rather than from us.

## Status

0.2.0, prototype. Unaudited software for a monetary system, published so the
simulation and identity work can be reproduced and built on -- not for
custody of anything real.

## Licence

CAL-1.0 (Cryptographic Autonomy License v1.0). Beyond the usual copyleft,
CAL requires that anyone you provide this software's functionality to
receives their own data and is not locked out of it by cryptographic or
technical means. See
[LICENSING.md](https://github.com/alberta-buck/alberta-buck/blob/master/LICENSING.md).

Repository: <https://github.com/alberta-buck/alberta-buck>
