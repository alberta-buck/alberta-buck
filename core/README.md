# core/ -- the Alberta Buck platform

The multi-language platform underlying BUCK bootstrap: design of record in
[`../alberta-buck-platform.org`](../alberta-buck-platform.org).  New Rust,
JS, and Python implementations land here; the existing `alberta_buck/`
Python package keeps working and transitions onto these incrementally.

## Layout

    core/
      python/     buck_core package: ChainSession API (session.py) --
                  send/call/deploy with expect=OK|REVERT + JSONL journal --
                  Foundry artifact helpers, and the compiled kernels
                  (import buck_core.buck_math / buck_core.buck_identity;
                  built by make nix-core-build-py).
      js/         @alberta-buck/core (ESM): the JS ChainSession (session.js,
                  over any viem client), backends.js (anvilSession joins a
                  running anvil -- e.g. one a Python sim deployed into --
                  tevmSession runs the in-process EVM), the shared journal
                  reader/writer, V3 price math (v3.js), the REAL Uniswap
                  periphery (router.js: Universal Router deploy recipe +
                  V3_SWAP_EXACT_IN encoding), the agent loop (world.js)
                  with PinWhale + RoundTripTrader, the one-pool scenario,
                  and bin/join-sim.mjs.  prototypes/ holds the agent
                  doctrine's structural exemplars (basket investor,
                  mortgage retiree -- the latter runs LIVE in the
                  equilibrium world).  scenarios/eqworld.js builds the
                  minimal equilibrium world (basket + pools + router +
                  agent-facing op helpers); bin/eqsim.mjs runs it
                  headless with SVG charts (src/chart.js).  demo/
                  hosts the interactive pages -- buckworld.html
                  (citizens + market) and eqworld.html (the BUCK-K
                  loop: live chart panels, dynamic add-saver /
                  add-debtor) -- each gated headlessly over the exact
                  shipped esbuild bundle.  Deps: viem + tevm
                  (`npm ci` in core/js).
      rust/       cargo workspace: buck-math -- the integer monetary kernel
                  (BuckCredit depreciation, Buck demurrage fee + carrying
                  buckSeconds apportionment, BuckKControllerDirect ppm PID +
                  fundingFactor + bumpless governance algebra), no_std and
                  dependency-free (wide.rs hand-rolls the 256-bit mulDiv);
                  buck-identity -- the BN254 identity kernel (arkworks
                  curve/pairing, keccak Fiat-Shamir transcripts, circomlib
                  Poseidon, PS credentials, ElGamal, registration NIZK,
                  Chaum-Pedersen approve, issuer Schnorr, verifiable
                  decryption, A2 issuer re-encryption binding, deposit
                  coupling, B1 depositor binding, note commitments /
                  nullifiers / id-hashes; every nonce an explicit argument
                  -- no randomness in the kernel).  bindings/py{,-identity}
                  (PyO3 cdylibs -> buck_core.*) and bindings/js{,-identity}
                  (wasm-bindgen -> core/js/wasm).  buck-math stays no_std
                  (Holochain zomes); buck-identity is std over arkworks
                  (wasm-clean; no_std flip is mechanical if a zome needs
                  it).  Later: buck-wallet.
      vectors/    cross-language fixtures.  journal-sample.jsonl pins the
                  journal schema; the Python and JS suites assert the same
                  facts about the same file.

## Rules

- **Dependencies point one way**: nothing in `core/` imports `alberta_buck`.
  `alberta_buck` imports `buck_core` (today via the sys.path shim in
  `alberta_buck/__init__.py`; via an installed wheel from Phase 2).
- **The Solidity contracts are the mathematical spec**: kernel functions are
  proven bit-identical against forge-generated golden vectors in
  `test/vectors/`, asserted by all three language suites.
- **Tests**: minimal in Rust, primary in Python and JS.  Every ported or new
  core capability gets coverage in each language that exposes it.

## Build / test

    make nix-core-js-deps       # one-time: npm ci in core/js
    make nix-core-build         # kernel bindings: Python .so + JS wasm pkg
    make nix-core-test          # all three suites
    make nix-core-test-py       # python -m pytest core/python/tests
    make nix-core-test-js       # cd core/js && node --test
    make nix-core-test-rust     # cd core/rust && cargo test
    make nix-venv-core-test-py  # Python suite inside the repo venv

The repo venv (`make venv` machinery) hosts BOTH packages: it installs
`alberta_buck[tests,dev]` and `-e core/python`, so `import alberta_buck`
and `import buck_core` coexist; the editable core install sees kernel
rebuilds immediately.  Nothing is ever pip-installed into the global or
user site-packages.

The golden math vectors (`test/vectors/math-vectors.json`) are generated
from the REAL contract code paths by `make nix-match-MathVectors` and
asserted bit-identically by all three suites.  The Tevm-backed JS tests,
kernel-binding tests, and the mixed-language join test skip cleanly when
their artifacts aren't built.

The identity kernel's ground truth is the executable Python reference in
`alberta_buck/wallet` (py_ecc):

- `core/vectors/identity-kernel-vectors.json` -- emitted by the reference
  (`make nix-venv-core-identity-vectors`; regeneration is an
  ABI-break-level event) with EVERY prove nonce recorded; the cargo,
  pytest, and node suites replay each prove call byte-for-byte.
- `test/vectors/identity.json` -- the forge fixture; the kernel suites
  re-verify every recorded proof and recompute every deterministic value.
- `alberta_buck/wallet` dispatches its G1 arithmetic, Poseidon, and
  pairing verifiers to `buck_core.buck_identity` when built
  (`BUCK_IDENTITY_BACKEND=py` forces the reference; `=kernel` requires
  the binding); `alberta_buck/test/test_kernel_backend.py` proves the
  flip changes nothing emitted, and `test_identity_cache_regen.py` that
  sim identity regeneration is backend-invariant end-to-end on anvil.
- `core/js/src/identity.js` wraps the wasm kernel in the BigInt API
  (including `canonicalIdentity()` -- THE canonical JSON dialect: sorted
  keys, compact separators, raw UTF-8, string/integer values only --
  shared by the identity preimage and the AB-RCPT/2 receipt core, and
  byte-identical to Python's `canonical_json()`).

## Journal schema (v1)

One JSON object per line; unknown fields ignored:

| field   | type | meaning                                  |
|---------|------|------------------------------------------|
| i       | int  | 1-based sequence number in the session   |
| tag     | str  | caller label (`"deploy:Buck"`, `"saver3:buy"`) |
| op      | str  | `send` / `deploy` (reads not journaled)  |
| fn      | str  | function name or `constructor`           |
| sender  | str  | from-address                             |
| expect  | str  | `ok` / `revert` (declared expectation)   |
| outcome | str  | `ok` / `revert` (what happened)          |
| matched | bool | outcome == expect                        |
| gas     | int  | gasUsed                                  |
| tx      | str  | transaction hash (0x-hex)                |
| block   | int  | block number                             |
| err     | str  | revert reason when outcome is `revert`   |
