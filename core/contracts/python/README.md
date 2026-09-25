# alberta-buck-contracts

Compiled ABI and bytecode for the Alberta Buck contracts: the BUCK token
(`Buck`), the insured asset NFT (`BuckCredit`), the on-chain PID controller
(`BuckKControllerDirect`), the identity accumulator (`IdentityRegistry`) and
the Poseidon hashers it takes, the Notes pool (`Notes`) with its verifier
adapters and Groth16 verifiers, the two baskets, and the simulation helpers.

**Unaudited prototype software for a monetary system.** These contracts have
never been audited. They are published so the research and simulations can be
reproduced and built on -- not so they can be deployed with other people's
money behind them.

## The Groth16 verifiers come from a DEVELOPMENT trusted setup

**Until v1.0.0, every Groth16 verifier in this package -- the batch-mint
verifiers, the spend verifier, the two folded deposit gates and the B1
membership verifier -- comes from a development trusted setup.** The setup
scripts contribute fixed entropy that is published in the repository
(`scripts/snark/setup*.sh`), so the toxic waste is public: **anyone can forge
a proof that these verifiers accept.**

That is deliberate, and it is why they are published at all: with the
toxic waste in hand, a simulation can model what an attacker holding a forged
proof could do, and test the defences around the proof -- the batch
signatures, the issuer bindings, the nullifier set -- which a production
setup would make impossible to exercise. It also means none of this may
guard anything of value. A production release will run a multi-party
ceremony, and will say so.

The bundle carries the same warning as data: every verifier's artifact has
`"trustedSetup": "development"`, and the compiler record's `trustedSetup`
names the setup and lists the verifiers.

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

## Standing up Notes

`Notes` takes its verifiers through adapters, so a world deploys the adapters,
registers a verifier per batch size, and wires them in:

- `MintVerifierAdapter` / `MintVerifierA2Adapter` -- `registerVerifier(n,
  address)` for each `MintBatchN{n}Groth16Verifier` /
  `MintBatchA2N{n}Groth16Verifier` (n = 1, 2, 4, 8, 16, 32); a batch is
  verified by the verifier registered for its size.
- `SpendVerifierAdapter(SpendGroth16Verifier)`, then
  `Notes(buck, mintAdapter, spendAdapter, governance)`.
- `setIdentityRegistry`, `setA2MintVerifier(a2Adapter)`,
  `setIdentityMembershipVerifier(IdentityMembershipB1VerifierAdapter())` and
  `setDepositFoldVerifier(DepositFoldVerifierAdapter(registry))`.  These two
  adapters deploy their own verifiers in their constructors; the verifier
  artifacts are shipped too, for direct use and inspection.
- The registry's accumulator hashes with `PoseidonT3` and `PoseidonT4`
  (`setIdentityPoseidon`, `setIdentityPoseidonT4`).

`alberta_buck/sim/notes_stack.py` in the repository is the reference wiring.
Every artifact fits EIP-170: the one verifier snarkjs emits too large
(`MintBatchA2N32Groth16Verifier`, 228 public inputs) ships in a table-driven
form with the same verification key.

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
