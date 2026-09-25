# alberta-buck-registry

The Alberta Buck registry kernel -- the identity-side authority
infrastructure:

- `tree` -- the incremental Poseidon Merkle accumulator of registered
  identity points
- `certificate` -- the registry-signed, ElGamal-sealed identity certificate
  family
- `aggregator` -- the central sub-root aggregator whose root is the on-chain
  `identityRoot`
- `feature` -- the feature-authority attestation tree
- `attributes` -- holder-produced attribute proofs over salted subtrees
- `regulator` -- the insurance regulator's periodic attestation, whose
  public subtrees gate credit

The executable specification is the Python reference in
`alberta_buck/registry`.  This crate matches it bit-for-bit, proven by
`tests/vectors/registry-kernel-vectors.json` -- emitted by the Python
reference, shipped inside this crate, and replayed by the Rust, Python and
JavaScript suites alike.

**Determinism rule: no clocks and no randomness in the kernel.**  Every nonce
is an explicit argument and every `attested_at` / `updated_at` timestamp is
supplied by the caller; the Python shims pass `time.time()`, tests pass
constants.

Layering: depends only on `alberta-buck-identity` (curve operations,
Poseidon, keccak, ElGamal).  `alberta-buck-wallet` depends on this crate for
tree membership in the unilateral receipt flows, mirroring the Python import
graph.

## Install

```toml
[dependencies]
alberta-buck-registry = "0.2"
```

The distribution is prefixed, the import is not: `use buck_registry::...`.

For worked usage, read `tests/vectors.rs` in this crate -- it exercises leaf
construction, path folding, certificate issue and verify, the aggregator and
the feature tree, each checked against the golden vectors.

## Status

0.2.0, prototype.  Unaudited software carrying the identity accumulator of a
monetary system.

## Licence

CAL-1.0 (Cryptographic Autonomy License v1.0).  Beyond the usual copyleft,
CAL requires that anyone you provide this software's functionality to
receives their own data and is not locked out of it by cryptographic or
technical means.  See
[LICENSING.md](https://github.com/alberta-buck/alberta-buck/blob/master/LICENSING.md).

Repository: <https://github.com/alberta-buck/alberta-buck>
