# alberta-buck-core

The Python side of the Alberta Buck platform: one testing API over every
EVM backend, with expectation checking and journaling.

- **`Web3Session`** -- the ChainSession API. Send, call and deploy against
  anvil, a public testnet, or in-process pyrevm with the same code. Every
  state-changing operation declares whether it is expected to succeed or
  revert, so expectation mismatches are first-class results rather than
  surprises.
- **Journal** -- an append-only JSONL record of every operation: tag, op,
  sender, expectation, outcome, gas, tx hash, block, error. Journals are
  auditable, diffable across backends *and across languages* (the
  JavaScript platform writes the same schema), and replayable.
- **`load_artifact`** -- contract artifacts from a repo checkout's `out/`
  when there is one, falling back to the installed
  [`alberta-buck-contracts`](https://pypi.org/project/alberta-buck-contracts/)
  otherwise, so an installed package can deploy a world with no checkout.

## Install

```sh
pip install alberta-buck-core
```

The distribution is prefixed; the import is not:

```python
from buck_core import Web3Session, Expect, load_artifact
```

## The compiled kernels are optional

`buck_core.buck_math`, `buck_core.buck_identity`, `buck_core.buck_wallet`
and `buck_core.buck_registry` are shims that delegate to
[`alberta-buck-kernel`](https://pypi.org/project/alberta-buck-kernel/) when
it is installed (`pip install alberta-buck-core[kernel]` brings it along,
where a wheel exists). This package is deliberately a pure-Python wheel: keeping
the binaries out means depending on the session API does not drag a
per-platform wheel matrix behind it.

In a repo checkout the built `.so` sits beside the shim and CPython prefers
the extension, so a freshly rebuilt kernel takes effect immediately.

The canonical monetary functions use full uint256 Python `int` values:

```python
from buck_core import buck_math

year = 31_557_600
cash = 10**42                 # one BUCK: 1e6 raw units times 1e36
remaining = buck_math.net_decay(cash, year)
fund = buck_math.net_fund_after(cash, 0, year)
assert remaining + fund == cash
baseline_relief = buck_math.net_target_relief(100_000_000, 100_000_000 * year, 100_000_000)
```

`net_target_relief` takes raw NFT principal, raw BUCK-seconds and a raw
principal reduction; its output is a baseline, not a liquid fund quote.
`mul_div_256(a, b, denominator, round_up=False)` uses an exact 512-bit
product. Negative/out-of-range values and invalid arithmetic fail explicitly.
These require the current compiled math kernel; they have no legacy fallback.
`fee_owing` and `carrying_transfer` retain the old linear model for comparison
callers. In a checkout, run `make nix-venv-core-net-math-test` for Solidity,
Rust, Python, Node and browser agreement.

## Selected household credit books

`NetCreditOps(chain, stack).mark_household(owner, token_ids)` returns one
block's ordinary wallet cash and selected NFT principal separately, plus
the credit limit, marked Jubilee relief and the current cash-based value
of each facility's nominal premium refund. `premium_net_scaled`,
`relief_scaled` and `available_relief_scaled` use `1e36` per raw BUCK;
the nested `state` uses raw six-decimal BUCK. `exceeds_liquidity_limit`
compares the total claim with the shared block allowance: marked relief
does not imply it can all be paid now.

Each refund uses its facility's actual insurer pool, including when the
insurer has changed its pool for new facilities. Unobserved insurer
strategy assets and future funding are excluded. Missing guarded marks
or a required maturity checkpoint fail instead of inventing relief.
Use `block_number=...` for a historical book; the backend must support it.
Duplicate/foreign facilities and Carrying household wallets are rejected.
`mark_net_basket_positions` separately reads exact receipt `valueOf` marks
and remaining basis before exit costs, without re-rounding `sharePrice`.

The existing Python/JS households now initialize their ordinary accounts
and separate facilities on canonical Buck. Their normal decision loop
still refuses until issuance and exit migration is complete. These books
are integration inputs, not a restored five-year economy.

`TransactionReverted.data` preserves structured provider data from the
post-block `eth_call` replay. A proxy may not declare its target's errors;
use `custom_error_name(target.abi, error)` to classify a known selector
without inspecting message text. Unknown/malformed data remains unknown,
and successful sends clear `last_revert_data`. This replay is diagnostic,
not proof of the original transaction's revert or its call origin. The
mined receipt, events and fresh monetary checks still determine outcomes.

## Status

0.4.0, prototype. Unaudited software for a monetary system, published so
the simulation and identity work can be reproduced and built on.

## Licence

CAL-1.0 (Cryptographic Autonomy License v1.0). Beyond the usual copyleft,
CAL requires that anyone you provide this software's functionality to
receives their own data and is not locked out of it by cryptographic or
technical means. See
[LICENSING.md](https://github.com/alberta-buck/alberta-buck/blob/master/LICENSING.md).

Repository: <https://github.com/alberta-buck/alberta-buck>
