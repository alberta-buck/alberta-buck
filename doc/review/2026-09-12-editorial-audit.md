# Alberta Buck corpus review — working record

This is an in-progress evidence and reading record, not a completed security
audit or a finding of novelty. Updated after completing the full corpus read.
Reviewed checkout HEAD: `3a9e170047ade21355a95661d2cd68e2a02fe9ea`.
The user requested full reading of the root `alberta-buck-*.org` corpus,
verification against code/tests, audience-focused editing, retirement of
redundant papers to `doc/historical/`, and completion of the main paper.
Original corpus: 19 papers, 24,703 lines, 174,715 whitespace-delimited words.

## Reading completed

- `alberta-buck-paper.org`: all 1,803 lines.
- `alberta-buck-identity.org`: all 2,930 lines (overlapping reads recovered truncated portions).
- `alberta-buck-identity-example.org`: all 2,343 lines.
- `alberta-buck-proofs.org`: all 1,561 lines, including the recovered notation table.
- `alberta-buck-demo.org`: all 253 lines.
- `alberta-buck-ethereum-routing.org`: all 258 lines.
- `alberta-buck-ethereum-basket.org`: all 426 lines.
- `alberta-buck-ethereum-direct.org`: all 634 lines.
- `alberta-buck-ethereum-example.org`: all 561 lines.
- `alberta-buck-notes.org`: all 3,365 lines, including the recovered implementation-status passage.
- `alberta-buck-notes-flow.org`: all 2,892 lines.
- `alberta-buck-notes-serialized.org`: all 759 lines.
- `alberta-buck-rebalance.org`: all 699 lines (re-read independently after combined output truncation).
- `alberta-buck-demurrage.org`: all 965 lines.
- `alberta-buck-deployment.org`: all 756 lines.
- `alberta-buck-platform.org`: all 747 lines.
- `alberta-buck-operations.org`: all 829 lines.
- `alberta-buck-receipt.org`: all 945 lines.
- `alberta-buck-ethereum.org`: all 1,977 lines, including the recovered mint example.

All 19 root papers have now been read in full. No original papers have been
edited or retired yet. The next work is reproducible evidence, focused source
verification, and audience-focused editing. See [the editorial map](editorial-map.md)
for the proposed destinations and [the findings](identity-findings.md) for
the security claims that constrain the rewrite.

## Mitigation expansion

At the user's request, `identity-findings.md` now expands each finding and
the five additional editorial conclusions with mitigation options, privacy
tradeoffs, Solidity/gas implications and required adversarial validation.
It uses the user's large encrypted-wallet-storage assumption to evaluate
private credentials, independent holder/account/payment secrets and
participant-held receipts. It distinguishes PQ archive encryption, PQ anonymous
credentials and migration of the complete proof/chain security assumptions.
No protocol implementation was changed; repair proposals remain unaudited.

Checked the findings' local links, code fences and whitespace. Also checked
the proposed three-relation approval equations over 32 deterministic group
examples: honest commitments reconstruct correctly, and the false-identity
witness fails the added key relation while satisfying the difference relation.
This is an algebra sanity check, not a Fiat–Shamir or real-EVM implementation
test and not a security proof.

## Reproduced identity findings

Using the existing sibling virtual environment, with wallet backend `kernel`:

1. **Published PS signatures permit identity guessing.** `ps_verify(X, Y,
   published_sigma, candidate_m)` accepts the correct candidate and rejects
   an incorrect one. Rerandomization hides the original signing randomness
   conditional on a fixed message; it does not hide the signed message from
   a verifier who knows candidates. This contradicts issuer-blindness.
2. **Disclosed m plus public signature authorizes a new registration.**
   Rerandomize the signature from registration calldata, create a fresh
   attacker key/ciphertext, and call `registration_prove` for the attacker
   address. `registration_verify` accepts. This is a fresh proof, not replay.
   No issuer secret, victim account key, or original private credential is
   required. The proposed read/write separation is absent.
3. **The implemented approve proof omits the registered-key relation.**
   For a sender knowing its own credential encryption randomness r, choose
   victim scalar m_v and use witness s = sk + (m - m_v)/r mod q. Encrypt
   m_v G to Bob and run the ordinary CP prover with s. The Python verifier
   accepts; Bob decrypts to m_v G, although sG differs from registered pk.
   Solidity `_verifyApprove` implements the same two group checks and hash.
   The formal paper proves a stronger three-relation protocol than this code.

These are counterexamples, not just missing proofs. Preserve reproducible
scripts/tests before changing claims. Do not silently redesign cryptography
as part of a prose edit.

Later the same day, EVM and Groth16 reproductions landed for findings 3
(already listed), 4, 5a, 5c, 7, 8 and 9. Findings 7 and 8 together moved
BUCK. See `identity-findings.md` and `scripts/review/`.

## Additional code findings requiring focused witnesses

- `IdentityRegistry._bindContract` accepts arbitrary supplied pk/E without
  a KYC credential, verified binder, or proof tying them to the binder.
- `register(..., identityLeaf)` inserts a caller-supplied leaf without
  binding it to the registered identity; `bindContract` also inserts leaves.
  The deterministic leaf can additionally link repeated registrations.
- `identity_membership_g1tie.circom` checks P = M + T with freely witnessed
  point T; it does not prove knowledge of b with T = bH. Thus any member M
  can be paired with arbitrary P by choosing T = P - M.
- `issuer_reenc.py` publishes `H_SCALAR`, defines H = H_SCALAR * G, and
  `note_binding.py` uses this H. The proofs instead assume unknown log_G H.
- Notes requires issuer and depositor CP approvals to the pool operator's
  key (the Notes paper explicitly describes this). The operator can open
  pool users' identities; a no-institutional-opener claim must account for it.
- Public face, accounts, issuerMode, timings, batch totals and transaction
  history contradict absolute graph-privacy claims. A1/B1 are public issuer
  modes. Identity-point encryption uses m also known to KYC issuers and
  previous counterparties; it is not an exclusive participant secret.
- `spendCoupledB1` uses the general spend circuit without proving that the
  note's committed flavor is B1 or tying its caller-supplied issuer to the
  note's committed issuer material. Reproduced: an A1 opening spent through
  B1 by a non-addressee, with empty membership, moved the note's face
  (`scripts/review/evm_a1_via_b1.py`).
- `_verifyIdentityMembership` / `_verifyNoteBinding` skip on empty proof or
  unset verifier even when the real adapter is wired (finding 8).
- Registration NIZK never proves `pk = sk*G`; a NUMS public key registers
  (`scripts/review/evm_uncontrolled_register.py`).
- G1-tie limbs are not 64-bit range-checked; a 2^64 carry proves against
  the committed zkey (`scripts/review/g1tie_limb_alias.py`).
- `_bindContract` / unmatched `identityLeaf` reproduced on EVM
  (`scripts/review/evm_uncertified_bind.py`).

## Editorial issues already established

- Main paper's B2 impossibility only addresses confidentiality from prior
  holders of the same copyable opening under a noninteractive model. It
  does not prove bearer cash private from blockchain observers impossible.
- Conditional set-membership argument is not a lower bound proving that
  Groth16 or a particular registry accumulator is necessary.
- Hashing identity records containing epoch/issued_at does not create one
  permanent, unique identity per person. KYC policy and renewal matter.
- Statements about conviction, liability, contempt, consent, and innocence
  cannot follow solely from cryptographic verification. Shared/stolen keys,
  malicious issuers and false registration bindings matter.
- Repeated Holochain architecture/epoch/revocation descriptions mix proposed
  features with current implementation. Identity examples include obsolete
  events/APIs and weaker transfer guards than the explanatory paper.
- Demonstrating distinct random coordinates is not an unlinkability test.
- Basket essay claims physics guarantees real-price mean reversion and
  investment returns. Those are hypotheses, not thermodynamic theorems;
  fees do not alone establish net LP gains or preserved capital.
- Direct-embodiment opening misstates numeraire invariance: a common USD
  move of BUCK and basket with unchanged ratio need not indicate a failure.
- Ethereum worked example calls a $6,000 USDC inflow swap-fee profit,
  mixes total wallet/LP balances, and contains an unfinished opening.

## Local environment and preservation

Update 2026-09-13: wallet counterexamples, mitigation prototypes, and
EVM/Groth16 reproductions for findings 3, 4, 5a, 5c, 7, 8 and 9 have been
added. Both `py` and `kernel` wallet-review backends pass. Integration
helpers, test fixtures and the review circuit remain unfinished scaffolding
for circuit-mitigation work. No production protocol source changed. The
repair order (P0-0 fail-closed first) is in
`mitigation-implementation-guide.md`.

- No applicable AGENTS.md found in cwd/ancestors or initial tracked-file search.
- Read CLAUDE.md and CONTRIBUTING.md. Preserve document copyright headers.
- Existing user changes: modified `test/vectors/identity-cache.json` and
  untracked simulation vectors/images. Initial status saved outside repo at
  `/private/tmp/alberta-buck-review-initial-status.txt`; do not overwrite them.
- Python: `/Users/perry/src/alberta-buck.venv-0.1.0-nix-darwin-cpython-313/bin/python`.
  Default python lacks py_ecc; sibling environment has working kernel.
- Foundry: `/nix/store/1283f23gxvanqigbgm94lz09bsp3qwhc-foundry-1.7.1/bin/`.
- Real SNARK artifacts already exist in `build/snark/`.
- External research: IACR ePrint 2015/525 metadata/abstract now accessible;
  Pointcheval/Sanders, Short Randomizable Signatures, CT-RSA 2016. It states
  security in the generic group model without random oracles; the papers'
  blanket q-SDH attribution still needs correction against the exact protocol.
  Mitigation findings also link EIP-1108, RFC 9380, FIPS 203/204, the lattice
  credential framework (ePrint 2023/560), LaZer and the original STARK paper.
