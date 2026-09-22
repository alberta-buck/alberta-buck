# alberta-buck-wallet

The Alberta Buck wallet kernel -- the deterministic layer above the identity
crypto:

- `canonical` -- THE canonical JSON dialect and identity serialization
- `envelope` -- the AB-RCPT/2 receipt envelope
- `verify` -- tier-1 offline receipt verification
- `builders` / `receipt` -- the per-kind receipt builders and the
  public-issuer / approve verifiers
- `flows` -- the unilateral identity-targeted Note flows A1/A2
- `issuer` -- the credential-issuer ceremony

The executable specification is the Python reference in
`alberta_buck/wallet`.  This crate matches it bit-for-bit -- canonical bytes,
receipt ids, envelope text and every verification predicate -- proven by
`tests/vectors/wallet-kernel-vectors.json`, which ships inside the crate and
is replayed by the Rust, Python and JavaScript suites.

**Determinism rule: no randomness and no clocks in the kernel.**  Every nonce
is an explicit argument, drawn by the caller in exactly the order the Python
reference draws them.

## Install

```toml
[dependencies]
alberta-buck-wallet = "0.1"
```

The distribution is prefixed, the import is not: `use buck_wallet::...`.

## Example

A receipt's canonical bytes round-trip through the printable envelope:

```rust
use buck_wallet::canonical::serialize_core;
use buck_wallet::envelope::{envelope_text, parse_envelope, receipt_id};

let bytes = serialize_core(&core)?;          // core: the receipt's JSON value
let id    = receipt_id(&bytes, 16);          // base32(sha256(canonical bytes))
let text  = envelope_text(&bytes, 64);       // the AB-RCPT/2 block, wrapped
assert_eq!(parse_envelope(&text)?, bytes);
```

`verify::verify_receipt(&core)` performs tier-1 offline verification of a
parsed receipt.  For worked usage of the builders and the Note flows, read
`tests/vectors.rs` in this crate.

## Status

0.1.0, prototype.  Unaudited software that builds and verifies the
instruments people would hold value in.  Treat accordingly.

## Licence

CAL-1.0 (Cryptographic Autonomy License v1.0).  Beyond the usual copyleft,
CAL requires that anyone you provide this software's functionality to
receives their own data and is not locked out of it by cryptographic or
technical means -- which is much of the point of the Notes flows this crate
implements.  See
[LICENSING.md](https://github.com/alberta-buck/alberta-buck/blob/master/LICENSING.md).

Repository: <https://github.com/alberta-buck/alberta-buck>
