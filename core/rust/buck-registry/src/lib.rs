//! buck-registry -- the Alberta Buck registry kernel.
//!
//! The identity-side *authority* infrastructure: the incremental Poseidon
//! Merkle accumulator of registered identity points (`tree`), the
//! registry-signed / ElGamal-sealed identity certificate family
//! (`certificate`), the central sub-root aggregator whose root is the
//! on-chain `identityRoot` (`aggregator`), and the feature-authority
//! attestation tree (`feature`).
//!
//! The executable specification is the Python reference in
//! `alberta_buck/registry`; this crate matches it bit-for-bit, proven by
//! `core/vectors/registry-kernel-vectors.json` (emitted by the Python
//! reference, replayed by the cargo / pytest / node suites).
//!
//! Determinism rule (the platform doctrine): no clocks and no randomness
//! in the kernel.  Every nonce is an explicit argument and every
//! `attested_at` / `updated_at` timestamp is supplied by the caller --
//! the Python shims pass `time.time()`, tests pass constants.
//!
//! Layering: this crate depends only on `buck-identity` (curve ops,
//! Poseidon, keccak, ElGamal); `buck-wallet` depends on this crate for
//! tree membership in the unilateral receipt flows -- mirroring
//! `alberta_buck.wallet.unilateral_a2`'s import of `registry.tree`.

pub mod aggregator;
pub mod certificate;
pub mod feature;
pub mod tree;

pub use buck_identity::{G1w, IdError, Result, W256, ZERO_W};
