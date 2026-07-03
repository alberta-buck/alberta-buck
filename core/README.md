# core/ -- the Alberta Buck platform

The multi-language platform underlying BUCK bootstrap: design of record in
[`../alberta-buck-platform.org`](../alberta-buck-platform.org).  New Rust,
JS, and Python implementations land here; the existing `alberta_buck/`
Python package keeps working and transitions onto these incrementally.

## Layout

    core/
      python/     buck_core package: ChainSession API (session.py) --
                  send/call/deploy with expect=OK|REVERT + JSONL journal --
                  and Foundry artifact helpers.  Later: PyO3 kernel bindings.
      js/         @alberta-buck/core (ESM, zero deps today): journal
                  reader/rollup.  Later: viem ChainSession over Tevm/anvil,
                  agent API, WASM kernel bindings.
      rust/       cargo workspace: buck-math seed (integer bp/ppm scaling).
                  Later: full buck-math, buck-identity, buck-wallet,
                  bindings/{js,py}.  Kernel crates stay no_std (Holochain).
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

    make nix-core-test          # all three suites
    make nix-core-test-py       # python -m pytest core/python/tests
    make nix-core-test-js       # cd core/js && node --test
    make nix-core-test-rust     # cd core/rust && cargo test

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
