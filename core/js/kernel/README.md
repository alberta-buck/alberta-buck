# alberta-buck-kernel

The Alberta Buck monetary and identity kernels, compiled to WebAssembly for
Node and the browser.

Two independent modules ride in this package:

- **math** -- integer monetary arithmetic: depreciation, demurrage, the
  on-chain PID controller, full-width `mulDiv`. Bit-identical to the Solidity
  contracts, which are the specification, proven by golden vectors that forge
  generates from the contracts themselves and that the Rust, Python and
  JavaScript suites all replay.
- **identity** -- BN254 curve arithmetic, Poseidon, keccak Fiat-Shamir
  transcripts, and the sigma protocols of the BUCK identity layer, together
  with the wallet and registry kernels (canonical JSON, the AB-RCPT/2 receipt
  envelope, the Notes flows, the identity Merkle accumulator). One arkworks
  copy serves all three.

The kernels contain no randomness and no clocks: every nonce is an explicit
argument. That is what lets the same inputs reproduce the same bytes in
Rust, Python and JavaScript.

## Install

```sh
npm install alberta-buck-kernel
```

## Use

The package ships both builds -- CommonJS for Node, ES modules for the
browser -- and the bare subpaths pick by environment:

```js
// Node
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const math = require("alberta-buck-kernel/math");
```

```js
// Browser: the web build needs an explicit wasm source, because bundlers
// break wasm-bindgen's default import.meta.url-relative fetch.
import init, * as identity from "alberta-buck-kernel/web/identity";
await init({ module_or_path: "/path/served/by/your/app/buck_identity_bg.wasm" });
```

`alberta-buck-kernel/node/*` and `alberta-buck-kernel/web/*` force one build
or the other when the environment default is not what you want.

Most callers should reach for
[`alberta-buck-core`](https://www.npmjs.com/package/alberta-buck-core)
instead, which wraps these in a BigInt-native API and handles the loading.

## ABI notes

`math` crosses `u128`/`i128` as JS `BigInt`. `identity` crosses 256-bit words
as `0x`-prefixed hex strings; `alberta-buck-core` wraps that into the
BigInt-native surface.

The wasm is not run through `wasm-opt`: `buck_identity_bg.wasm` is about
1.2 MB uncompressed and compresses well over the wire. Optimizing it is a
size improvement, not a correctness one, and is tracked in the deployment
plan.

## Status

0.3.0, prototype. Unaudited software implementing the arithmetic and
cryptography of a monetary system. It has been checked for agreement with a
reference implementation, which is not the same as having been checked for
security.

## Licence

CAL-1.0 (Cryptographic Autonomy License v1.0). Beyond the usual copyleft,
CAL requires that anyone you provide this software's functionality to
receives their own data and is not locked out of it by cryptographic or
technical means. See
[LICENSING.md](https://github.com/alberta-buck/alberta-buck/blob/master/LICENSING.md).

Repository: <https://github.com/alberta-buck/alberta-buck>
