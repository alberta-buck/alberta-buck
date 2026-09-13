# Mitigation implementation and testing guide

Audience: an implementer (human or AI) who will work through the identity
findings and land the repairs. This guide is derived from
[identity-findings.md](identity-findings.md) and adds confirmation status,
costed and prioritized work packages, exact files to change across every
backend, and the test each package must pass. Read the findings document
first; this guide does not restate the cryptographic arguments in full.

The findings document is the authority on the security claims. This guide is
the authority on execution order, blast radius, and done-criteria. Where they
disagree, re-verify before acting.

## How to use this guide

1. Do the work packages in the printed order. Later packages assume earlier
   ones are merged.
2. Each package has a **Done when** block. Do not mark a package complete
   until every line holds.
3. Treat the reproduction scripts as the ground truth. A repair is real only
   when the matching reproduction flips from "attack succeeds" to "attack
   rejected" and the honest control still passes.
4. Never weaken a reproduction test to make it pass. When a fix lands, invert
   the assertion deliberately and say so in the commit.

## Environment

```bash
# Python wallet + reproductions (sibling venv already has the built kernel):
PYTHONPATH=. /Users/perry/src/alberta-buck.venv-0.1.0-nix-darwin-cpython-313/bin/python <script>
# Wallet review tests, both backends (py + Rust/PyO3 kernel):
make nix-venv-... is not wired for these; run pytest directly in the venv:
  .../bin/python -m pytest alberta_buck/test/review/test_wallet_failures.py -q
# Contracts (pinned solc 0.8.28), never bare forge:
make nix-build          # or the repo's usual make nix-test
# Rust kernel + vectors:
cargo test -p buck-identity           # under core/rust
# JS parity:
cd core/js && npm test
```

Do not regenerate production SNARK proving/verification keys or committed
vectors except where a package explicitly says to, and then only as a matched
set (see the cross-cutting rules at the end).

## Confirmation status of the findings

Reproductions live under `scripts/review/` and
`alberta_buck/review/` with tests in
`alberta_buck/test/review/test_wallet_failures.py`.

| # | Claim | Severity | Confirmed how |
|---|---|---|---|
| 1 | A published PS signature lets anyone holding a candidate `m` test the identity; the issuer can link its own clients | High (breaks issuer-blindness) | Wallet, both backends (`test_01`) |
| 2 | Disclosed `m` + the public signature let an attacker register a **new** account under that identity | High (read/write not separated) | Wallet, both backends (`test_02`) |
| 3 | The approval proof omits the registered-key relation, so a sender can make a receipt name a **third party** | High (framing) | Wallet (`test_03`) **and on a real EVM** (`scripts/review/evm_approval_forgery.py`) |
| 4 | `_bindContract` and caller-supplied `identityLeaf` are stored with no credential/inclusion proof | Medium-High | Source-established (unambiguous in `src/IdentityRegistry.sol`) |
| 5a | The g1-tie circuit accepts `T = P - M` with free `T`, so any member can pair with any `P` | High (identity substitution) | **Real Groth16** with the committed circuit + zkey (`scripts/review/g1tie_membership_mismatch.py`) |
| 5b | `issuer_reenc.H` has a published discrete log, so `P = mG + bH` is not binding for scalar-known identities | Medium (latent) | Wallet (`test_05`) |
| 6 | Pool operator and prior counterparties hold real decryption capability; identity-derived keys are not exclusive | Medium | Wallet (`test_06`) + source |
| 7 | The B1 spend path does not prove the note's flavor or bind the caller-supplied issuer | High (addressed notes reach a bearer path) | Source-established; `spend.circom`'s own comment admits it |

None of these is yet a demonstrated end-to-end theft of funds. Findings 2, 3,
5, and 7 are unauthorized-action or identity-substitution results that still
require the attacker to hold specific witnesses. Keep that calibration in the
paper edits; do not upgrade "unauthorized registration" to "balance theft"
without a reproduction that moves BUCK.

## SILMARILS is orthogonal: do not use it to repair these findings

`doc/SILMARILS/SILMARILS.tex` and `doc/SILMARILS-evaluation.org` were assessed
against the identity system's needs. Verdict, backed by
`scripts/review/silmarils_model.py`:

- SILMARILS has **no signature re-randomization**. There is no operation that
  turns one issued credential into unlimited unlinkable credentials that a
  third party can still verify against a fixed issuer public key. That
  operation is the entire basis of the identity fountain, and SILMARILS lacks
  an analogue.
- SILMARILS is **not publicly verifiable**. Its two-party mode is
  designated-verifier: only a holder of the per-pair secret `k_sig` can
  verify. Verification never uses the signer's long-term key. An on-chain
  contract holds no per-signer secret, so it cannot be the verifier.
- Publishing the receipt `r` makes the scheme **universally forgeable**: any
  third party can mint a fresh accepting transcript on the same message. This
  is the intended designated-verifier simulatability, and it is exactly wrong
  for a credential that must bind an identity.
- The SSS technique proposed in `SILMARILS-evaluation.org` does **not** carry
  over to an on-chain verifier. A field commitment `x*g + k*h` with public
  `g, h` opens to any value, and a public linear check recovers the witness by
  division. SSS hiding is information-theoretic only while a share is withheld
  from the adversary; a public verifier withholds nothing. The evaluation
  doc's "Open Question 1" resolves negatively for the public-verifier setting.

Where SILMARILS-family ideas legitimately apply: off-chain two-party
authentication between parties that share a secret, which is the paper's own
stated blockchain application. That is a separate track from the identity
credential and from these findings. **Post-quantum migration is a separate
axis from correctness.** Every finding here is present in the classical
construction and is not fixed by changing the hardness assumption. Fix
correctness first; treat PQC as a later, protocol-level migration (lattice
anonymous credentials and hash-based transparent proofs are the candidate
directions in the findings doc, not SILMARILS).

## The four-backend rule

The identity crypto is implemented four times and they must stay byte-identical
on honest inputs. Any change to a proof's transcript, relation set, or wire
shape must land in all four plus the vectors, or cross-language parity breaks:

| Layer | Path |
|---|---|
| Solidity (on-chain verifier) | `src/IdentityRegistry.sol`, `src/Notes.sol` |
| Python (executable spec) | `alberta_buck/wallet/*.py` |
| Rust (PyO3/WASM kernel) | `core/rust/buck-identity/src/*.rs` |
| JS | `core/js/src/identity-core.js`, `core/js/src/identity.js` |
| Vectors | `test/vectors/identity.json` (emit with `python -m alberta_buck.wallet.cli emit-vectors`), consumed by `core/rust/buck-identity/tests/vectors.rs` and `core/js/test/identity.vectors.test.js` |

---

## Work packages, in priority order

### P0-A. Approval three-relation repair (finding 3)

**Why first.** Highest confirmed severity, reproduced on a real EVM, and the
only fix that needs no trusted-setup regeneration. It is a self-contained
sigma-protocol change.

**Mechanism.** `alberta_buck/wallet/chaum_pedersen.py` and
`IdentityRegistry._verifyApprove` check only two relations: `R_b` consistency
and the plaintext-difference relation. They never check `pk_a = sk*G`. A
sender who knows their own encryption randomness `r` sets
`s = sk + (m - m_v)/r mod q` and produces an approval that "re-encrypts" a
victim scalar `m_v`. The proofs paper (`alberta-buck-proofs.org`, Part II,
relations S1/S2/S3) proves a **three**-relation protocol; the shipped code is
a different, unsound two-relation protocol.

**Fix.** Adopt the compact three-relation approval already prototyped in
`alberta_buck/review/mitigations.py` (`prove_approval` / `verify_approval`).
It adds `T_key = a*G` and the verifier check `u*G == T_key + e*pk_a`, keeping
the `R_b` and difference relations. The forged witness fails the new first
relation.

**Files.**
1. `alberta_buck/wallet/chaum_pedersen.py`: replace `CPProof` and both
   functions with the three-relation statement. Decide the wire encoding
   (either keep three commitment points, or the compact `(e, u, v)` form; the
   findings doc §3 costs both). Record the decision in the module docstring.
2. `src/IdentityRegistry.sol`: `struct CPProof`, `_verifyApprove`, `_fsApprove`
   to mirror the new relations and encoding. Add the `pk_a` key relation.
   Reject infinity/zero keys and non-canonical scalars.
3. `core/rust/buck-identity/src/chaum_pedersen.rs`: same relations.
4. `core/js/src/identity-core.js` (and `identity.js`): same relations.
5. Regenerate `test/vectors/identity.json` via `emit-vectors`; re-run the Rust
   and JS vector tests so parity holds.
6. Update every caller of the changed `verifyApprove` ABI: `Buck.approve`
   overload, `alberta_buck/sim/notes_stack.py::approve_pool`, and any wallet
   `build_receipt` path that embeds a CP proof.

**Test.** In `test_wallet_failures.py::test_03` the forged approval currently
asserts the OLD verifier accepts and the NEW prototype rejects. After the fix,
the production `chaum_pedersen_verify` and the Solidity `_verifyApprove` must
both reject the forged witness and accept the honest one. Extend
`scripts/review/evm_approval_forgery.py` to assert `accepted == False` for the
forgery and `True` for the honest control against the rebuilt contract. Add
adversarial cases: changed domain, repeated nonce, zero/infinity keys.

**Cost.** Two extra scalar multiplications on-chain, about +12,000 gas at the
EIP-1108 BN254 schedule, plus hashing and call overhead to measure. The
compact `(e, u, v)` encoding can save about 192 calldata bytes. No trusted
setup. Effort: medium, dominated by four-backend parity and the ABI ripple.

**Done when.** Forged approval rejected by Python and Solidity; honest
approval accepted; Rust and JS vector tests green; the EVM script asserts the
rejection; `test_03` inverted with a commit note.

---

### P0-B. B1 spend flavor and issuer binding (finding 7)

**Why here.** High severity: an A1/A2 (addressed) note opening can be pushed
through the bearer B1 entry point, avoiding addressed note-binding, and the
caller-supplied `issuer` is not tied to the note's committed issuer material.
`circuits/spend.circom` states in its own comment that it does not constrain
`flavor` and that A-flavor notes are "effectively bearer-spendable."

**Fix.**
1. Constrain the committed `flavor` to the entry point's mode. Either add a
   constrained public `mode` input to `spend.circom` that Solidity supplies
   and checks, or split into flavor-specific circuits. A flavor equality is
   cheap in-circuit.
2. In `Notes.spendCoupledB1`, bind the supplied `issuer` to the note's
   committed issuance material. Define the issuance payload and its signature
   precisely; a note cannot sign the commitment that contains its own
   signature, so use a separately defined issuance payload.
3. Constrain `predicate`: reject unsupported predicates rather than committing
   an arbitrary word.

**Files.** `circuits/spend.circom` (and any new `spend_a.circom`),
`src/Notes.sol` (`spendCoupledB1`, `_spendCoupled`, `_verifyNoteBinding`),
plus the Groth16 verifier and vectors that the circuit change forces.

**Test.** Start from the A1/A2/B1 fixtures in
`alberta_buck/sim/notes_stack.py`. Generate a spend proof for a controlled
payout and attempt the B1 route with only an A-flavor opening; assert it now
reverts. Separately substitute the issuer of a B1 note and assert rejection.
Add unsupported-predicate cases. These are semantic-substitution tests; a
mutated-bytes rejection does not cover them.

**Cost.** A fixed-flavor circuit keeps the public-input count and proof size;
the mode equality is cheap. Authenticating issuance material adds proving work.
**Requires a fresh trusted setup for the spend circuit** and a redeployed spend
verifier, so treat the artifact regeneration as a matched set. Effort:
medium-high.

**Done when.** Every note flavor is tried through every spend entry point with
fresh proofs; only the intended flavor succeeds per path; issuer substitution
is rejected; artifacts regenerated as a matched set; a honest control spend
still settles BUCK.

---

### P1-A. Membership witness binding (finding 5)

**Mechanism.** `identity_membership_g1tie.circom` proves `P = M + T` with `T`
freely witnessed. Setting `T = P - M` lets any tree member `M` be paired with
any `P`, so the membership proof's identity need not equal the identity opened
by the companion sigma. Reproduced with the committed circuit and zkey.

**Fix (preferred: mitigation B in the findings doc).** Fold certified
membership into the note-binding relation so a single circuit uses the **same**
private identity variable for the Merkle leaf, the ciphertext relation, and the
addressed recipient/issuer condition. This removes the cross-proof equality
assumption entirely.

**Fix (alternative: mitigation A).** Keep separate proofs but make `P` a
binding commitment `mG + bH` where **no party knows `log_G(H)`** (derive `H`
by hash-to-curve per RFC 9380, not by publishing a scalar), and prove
knowledge of `m, b` on both sides. Note the residual `M' = M + delta*H`
transformation for arbitrary point messages; constrain the scalar
representation.

**Files.** `circuits/identity_membership_g1tie.circom` or the note-binding
circuits; the generated `IdentityMembershipG1TieVerifier.sol` /
`NoteBinding*Verifier.sol`; the adapters; `src/Notes.sol::_verifyIdentityMembership`;
proving/verification keys and proof vectors under `build/snark/`.

**Test.** Rebuild `scripts/review/g1tie_membership_mismatch.py` against the
repaired artifacts: the `T = P - M` witness must now fail witness satisfaction
or verification, off-chain and through the deployed adapter. Preserve the
known-log double-opening (`test_05`) as an independent check. A changed public
input rejecting the old proof is **not** the same as rejecting a fresh
mismatched witness; assert both.

**Cost.** Circuit redesign, fresh trusted setup, redeployed verifier. High
effort. Benchmark mitigation B (one larger circuit, fewer verifier calls)
against mitigation A (two proofs, a maintained composition argument) before
committing.

**Done when.** The mismatched witness is rejected by the real prover/verifier;
the honest membership proof still verifies; artifacts regenerated as a matched
set; the composition argument is written down.

---

### P1-B. Certified binding and accumulator admission (finding 4)

**Mechanism.** `_bindContract` stores caller-supplied `pk`/`E`/flags with no PS
signature, no verified binder, no proof tying the identity to the target.
`register` and `bindContract` accept `identityLeaf` without proving it hashes
the registered identity, so an accepted leaf is not evidence of KYC admission,
and a deterministic leaf links registrations.

**Fix.** Separate three checks: **control** (the target authorizes the
binding), **certification** (the same credential presentation an ordinary
account needs, or an explicit certified-operator policy), and **membership**
(prove the admitted leaf contains the certified identity). For private
contract wallets do not point at the owner's registered EOA. Give accepted
roots an explicit authority and update policy.

**Files.** `src/IdentityRegistry.sol` (`_bindContract`, `_register`,
`_insertIdentityLeaf`, the `register`/`bindContract` overloads), plus wallet
helpers that build binding arguments.

**Test.** Reject unauthorized first binding, fabricated service identities,
certified-identity/leaf mismatches, and unaccepted roots. Verify two accounts
built from one credential reveal no common leaf. New EVM reproduction under
`scripts/review/`, with honest controls.

**Cost.** Mostly Solidity plus a proof obligation at bind/register time. No
new curve primitive. Medium effort. Interacts with P2-A: the private,
salted-commitment leaf design belongs to the credential redesign.

---

### P2-A. Credential harvesting and read/write separation (findings 1 and 2)

**Design-gated. Do not implement ad hoc.** The fix is a different protocol, not
a patch: stop publishing the raw rerandomized PS signature in registration
calldata, and instead publish a zero-knowledge proof of possession of a valid
credential on hidden attributes, bound to a wallet-generated holder secret
`u`. Registration must also prove control of the advertised encryption key.

**Sequencing.**
1. First write the specification: the show-proof relation, the issuance
   relation (blind issuance on `m` and a commitment to `u`), and an
   issuer-view unlinkability game covering issuance, all public registration
   inputs, repeated presentations, and later disclosures.
2. Only then implement, across all four backends, with new vectors.

Until the spec exists, the correct interim action is documentation: withdraw
the issuer-blindness and statistical-unlinkability claims for the current
registration protocol in `alberta-buck-paper.org` and `alberta-buck-identity.org`,
and mark the registration transcript as candidate-testable.

**Cost.** New presentation verifier or circuit; cost not yet measured. High,
and spec-first. This is where a less-capable implementer is most likely to ship
something unsound; keep it gated behind a written, reviewed protocol.

---

### P2-B. Observer model and participant-held receipts (finding 6)

**Fix.** Replace identity-approvals to the pool with proofs of the eligibility
the pool actually needs; route encrypted identity evidence to the actual
payer/payee under independent, authenticated per-payment keys rather than
identity-derived keys such as `m_rec*G`. Keep full receipts off-chain,
encrypted to the intended participant, with a small on-chain commitment.

**Files.** `alberta_buck/wallet/envelope.py`, `verify_receipt.py`,
`build_receipt.py`, the pool approval surface in `Notes.sol` and
`notes_stack.py`.

**Test.** Model each adversary separately: public observer, issuer, pool
operator, prior counterparty, coalitions. Give each its real records and keys;
verify revealing one receipt yields no other payment's key. Test missing
ciphertexts, malicious receiving keys, backup recovery, key rotation.

**Cost.** Mostly wallet and interface. Archive encryption adds no required
gas; enforced receipt correctness needs a proof. Medium.

---

### P3. Post-quantum track (separate axis)

Only after the correctness packages. In order of confidence: encrypt wallet
archives and delivered receipts with a reviewed KEM-plus-AEAD construction
(ML-KEM / FIPS 203) as an off-chain change; evaluate lattice anonymous
credentials (ePrint 2023/560, LaZer) as complete protocols against the same
adversary model; consider certified-commitment admission plus a hash-based
transparent proof system for end-to-end PQ soundness. Do not treat a smaller
signature as a smaller transaction. Do not assume the current curve, hash
sizes, or circuit parameters meet a PQ target unchanged. SILMARILS is not a
drop-in here (see the orthogonality section).

---

## Cross-cutting rules

- **Matched-set artifacts.** Any circuit change forces one run that regenerates
  the r1cs, zkey, committed Groth16 verifier `.sol`, and proof vectors
  together. Never hand-edit one of these. The Makefile treats them as a set.
- **Never regenerate production keys casually.** Use isolated build outputs for
  experiments. The committed `build/snark/` artifacts and `test/vectors/` are
  the reference; a package that must replace them says so explicitly.
- **Backend parity before claiming a fix.** Run the pure-Python cases, then the
  Rust/PyO3 and JS vectors on the same malicious and honest inputs. Verify one
  backend's honest proof with another. Randomized Groth16 proof bytes need not
  match; canonical scalars, points, challenges, and accept/reject results must.
- **EVM reproduction beats inspection.** Prefer an Anvil or in-process revm
  reproduction (see `alberta_buck/sim/pyrevm_backend.py` and the `evm_*` script)
  over reading equations, and always pair an attack with an honest control so a
  test cannot pass by rejecting everything.
- **Preserve, then invert.** Keep each reproduction. When its fix lands, invert
  the assertion in the same commit and note it, so the test now guards the fix.

## Definition of done for the whole program

1. Every confirmed finding has a merged fix across all four backends with
   regenerated vectors, or an explicit written deferral with its risk stated.
2. Every reproduction script asserts the attack is now rejected and the honest
   control still succeeds.
3. The paper claims are revised to match the shipped protocol, distinguishing
   implemented behavior from proposed protocol, per
   [editorial-map.md](editorial-map.md).
4. The post-quantum discussion is scoped as a separate migration, with
   SILMARILS documented as inapplicable to the on-chain credential.
