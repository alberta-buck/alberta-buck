# Identity claims: evidence and consequences

Review date: 2026-09-12. Checkout HEAD:
`3a9e170047ade21355a95661d2cd68e2a02fe9ea`.
This is a working review, not a complete cryptographic audit.

The repair objective is **certified identity without general surveillance**:
participants can retain evidence of their own transactions; learning an
identity does not confer its owner's authority; unrelated users, the credential
issuer and infrastructure operators do not acquire a general transaction
opening capability. These are design targets, not properties established for
the current implementation. Establishing novelty requires a separate comparison
with prior work after the repaired construction is specified.

The recommendations below assume honest users can keep substantial encrypted
wallet archives. This favors private credentials, independent per-payment
keys and complete off-chain receipts, with small commitments and proofs on
chain. Storage capacity does not itself supply proof soundness, delivery,
permanent retention or protection after a wallet key is compromised.

Evidence labels distinguish executed counterexamples from source analysis.
**Production repairs have not been implemented or audited.** Separate review
prototypes and their execution status are recorded below. Gas statements are
structural estimates unless explicitly described as measurements.

## 1. A published PS signature tests a candidate identity

**Reproduced in the wallet verifier.** Registration publishes a rerandomized
signature `(h, (x + m*y)h)`. Given the issuer's public key `(X, Y)` and a
candidate scalar `m`, anyone can check

    e(h, X + mY) = e(sigma_2, G2).

The test accepts the correct candidate and rejects a different one. The KYC
issuer knows its issued identity records and can enumerate their scalars.
With its signing secret it can also test `sigma_2 == (x + m*y)h` directly.

Rerandomization makes the first component uniform for a fixed message. That
does not make presentations of different messages indistinguishable to someone
who knows candidate messages. The formal paper's conditional distribution
argument does not establish issuer-blind account unlinkability.

Sources: [PS implementation](../../alberta_buck/wallet/ps.py),
[registration transcript](../../alberta_buck/wallet/nizk.py),
[on-chain registration](../../src/IdentityRegistry.sol).

**Editorial consequence:** withdraw issuer-blindness and statistical identity
unlinkability for the current registration protocol. A hidden credential
presentation requires a different construction and a security argument for
the complete published transcript.

### What this does and does not establish

The attack is a candidate test, not an algorithm for recovering an arbitrary
uniform scalar. That distinction gives little protection against the issuer,
which already has the records, or a counterparty who has received one. A longer
hash or more registration randomization does not prevent testing a known
candidate. Giving each identity a secret salt could obstruct strangers' guesses,
but it would not protect against anyone who receives that salt and the record.

Nor does encrypting the registration ciphertext differently conceal the raw
signature's separate test. Privacy must cover all published components together,
including public keys, leaves, credential attributes, events and calldata.

### Mitigation: present knowledge of a credential

Keep the reusable signature in the encrypted wallet. Publish a zero-knowledge
proof of possessing a valid credential on hidden attributes, tied to the same
identity and key used by registration. A signature possession presentation is
a different protocol from publishing a normally verifiable rerandomized
signature and adding a proof about its message. Pointcheval and Sanders supply
a relevant signature construction and protocols; use a reviewed anonymous
credential presentation rather than infer anonymity from randomization alone.
[Primary paper](https://eprint.iacr.org/2015/525).

Two implementation directions deserve comparison:

- **A complete pairing-based presentation.** Retain useful BN254 precompiles
  and the present small-contract approach, but redesign issuance and showing
  together. The proof must hide attributes even from an issuer that knows its
  signing key and issuance records. Credential validity, ciphertext consistency
  and holder authorization must share witnesses. Its gas cost needs a concrete
  transcript and benchmark; the current signature size is not its total cost.
- **Hidden membership in certified credential commitments.** Admit randomized
  commitments to an issuer-authorized tree, then prove membership and the
  registration relation in one circuit. Solidity verifies a proof and accepted
  root. This can avoid verifying a credential signature inside every circuit,
  at the cost of root governance and wallet membership-witness maintenance.
  Section 4 describes what makes admission certified.

Issuance should use common issuer keys and sufficiently broad validity groups;
unique issuer keys, serial numbers or expiry values disclosed at presentation
can identify a credential even when its signature is hidden. Batched enrollment
and delayed first use reduce timing correlation. They cannot guarantee an
anonymity set where only one person is eligible.

**Required validation:** define an issuer-view unlinkability experiment that
includes issuance, all public registration inputs, repeated presentations and
later identity disclosures. Demonstrate that the existing candidate test has
no corresponding public signature to test, then supply a security argument
for the replacement transcript. Failed dictionary tests alone are not a proof
of unlinkability.

## 2. Disclosing the identity scalar enables a new registration

**Reproduced in the wallet verifier.** An attacker takes the public signature
from a registration, learns `m` from a disclosed identity record, rerandomizes
that signature, encrypts `mG` under a fresh attacker key, and generates a new
registration proof bound to the attacker's address. The verifier accepts.

This needs neither the victim's account key nor the original private
signature. It is a fresh proof, so the existing rejection of replay under a
different address does not address it. A rerandomized signature remains a
credential with the same signing authority.

Sources: [registration prover/verifier](../../alberta_buck/wallet/nizk.py),
[PS rerandomization](../../alberta_buck/wallet/ps.py).

**Editorial consequence:** the claimed separation of a disclosed read
capability `m` from a secret registration capability `sigma` is absent.
This counterexample establishes unauthorized account creation under the named
identity; it is not yet an end-to-end demonstration of theft of a Notes balance.
Addressed-note theft would additionally require the note opening and its
other spend witnesses.

### Mitigation: separate identity information from authorization

Use distinct objects with distinct disclosure rules:

| Object | Purpose | Who may learn it |
| --- | --- | --- |
| Identity record and its digest `m` | Name the certified subject | Issuer and authorized receipt readers; treat it as sensitive information, never as an authorization secret |
| Random holder secret `u` | Authorize credential presentations | Holder and deliberately chosen recovery mechanism |
| Account secret `sk_a` | Control one account's cryptographic key | That account's wallet |
| Independent note/receipt secret `d_i` | Decrypt a particular payment's evidence | Its intended participant; selective disclosure can be limited to that payment |
| Credential signature and issuance state | Support future presentations | Encrypted wallet; no raw reusable credential in receipts or calldata |

One candidate is blind issuance of a credential on both `m` and a commitment
to a wallet-generated secret `u`. The issuer certifies the identity but does
not learn `u`. A registration proof establishes possession of that credential
and secret, and authorizes the chosen account key and address. Do not disclose
a stable public commitment to `u` on each account; that would link accounts.
The anonymous presentation must hide it along with the identity.

Alternatively, an issuer can admit a salted commitment to `(m, u, policy)`
after checking a proof that it contains the identity it certified. Later
membership proofs demonstrate knowledge of its opening and authorize a fresh
account. This needs a proper issuance relation: accepting an opaque commitment
without connecting it to the certified record merely relocates the gap in
section 4.

Hiding the signature already removes the demonstrated harvesting path against
an honest issuer. Adding a holder secret makes the intended separation explicit
and can support recovery and scoped pseudonyms. Neither measure prevents a
malicious trusted issuer from certifying a second credential for someone else's
name. Preventing that requires a defined enrollment policy, additional trust
assumptions or independently checked enrollment evidence.

The registration should also prove control of the advertised encryption key,
or explicitly define a supported delegation mechanism. Knowledge of encryption
randomness does not establish knowledge of the recipient key's secret. That
omission is now a separate reproduced finding (section 9). Bind
the action, account, chain, registry, protocol version and relevant expiry or
nonce into its authorization context. Registration Fiat–Shamir currently
omits `chainId` and the registry address; approve at least binds `chainId`.

**Storage, privacy and gas:** large archives comfortably hold credentials,
independent secrets and renewal history. They do not require per-transaction
on-chain storage of those objects. Extra hidden attributes may increase proving
work without increasing a fixed-size SNARK proof; a direct sigma/pairing design
has its own costs. Independent recovery keys and versioned backups prevent a
lost device from unnecessarily forcing identity disclosure. A single backup
master key remains a compromise concentration; optional compartments reduce
the scope of one key's exposure.

**Required validation:** after releasing a complete ordinary receipt and every
public registration transcript, an attacker must still be unable to register
a new account without `u` and its credential. Test key substitution, delegated
registration, recovery and revocation separately. A holder can intentionally
copy software secrets to another person; do not call ordinary proof of secret
possession unconditional human non-transferability.

## 3. The approval proof does not bind its witness to the account key

**Reproduced in the wallet verifier and on a real EVM.** The deployed
`IdentityRegistry` bytecode accepts the false-identity approval, and Bob
decrypts it to the third party's identity, not the sender's; the honest
control is accepted and decrypts to the sender's identity. See
`scripts/review/evm_approval_forgery.py` (in-process revm via `PyrevmAnvil`).
Let the sender's registration be

    pk_a = sk*G, R_a = r*G, C_a = m*G + sk*R_a.

The sender knows the nonzero encryption randomness `r`. To make an approval
name another scalar `m_v`, choose

    s = sk + (m - m_v)/r mod q
    E_b = (r_prime*G, m_v*G + r_prime*pk_b).

Then `C_a - C_b = s*R_a - r_prime*pk_b`, the relation the implemented
approval proof actually checks. The ordinary prover accepts witness `s`,
and the verifier accepts its proof, although `s*G != pk_a`. Bob decrypts
the approval to `m_v*G`, not the sender's registered identity.

Hashing `pk_a` into Fiat–Shamir binds that public value to the transcript;
it does not enforce the missing equation `sk*G = pk_a`.

Sources: [approval proof](../../alberta_buck/wallet/chaum_pedersen.py),
[`_verifyApprove`](../../src/IdentityRegistry.sol). The formal paper's
three-relation proof includes key ownership and proves a stronger protocol.

**Editorial consequence:** withdraw equal-plaintext soundness and anti-framing
for the implemented approval. Receipts that rely on this approval inherit
the problem. A protocol repair must update the Solidity, Rust, Python and JS
transcripts and their fixtures together; changing the paper alone cannot fix it.

### Mitigation: prove all three relations with the same witnesses

For valid prime-order group elements, the intended statement is knowledge of
`sk` and `r_prime` satisfying all three equations:

```text
pk_a       = sk*G
R_b        = r_prime*G
C_a - C_b  = sk*R_a - r_prime*pk_b
```

A compact candidate uses fresh random scalars `a, b` and commitments

```text
T_key  = a*G
T_R    = b*G
T_diff = a*R_a - b*pk_b

e = FiatShamir(domain, complete statement, T_key, T_R, T_diff)
u = a + e*sk
v = b + e*r_prime
```

The verifier checks

```text
u*G                   = T_key  + e*pk_a
v*G                   = T_R    + e*R_b
u*R_a - v*pk_b         = T_diff + e*(C_a - C_b)
```

Two accepting transcripts with the same commitments and different challenges
extract the *same* `sk` and `r_prime` across all three relations. In particular,
the forged `s` in the counterexample fails the first relation. This explains
the missing binding; a full argument must also cover Fiat–Shamir, encodings,
group validation and the surrounding authorization protocol.

This candidate keeps **three G1 commitments and two responses**, plus the
challenge if the existing encoding retains it. Repurposing the current three
point fields can preserve calldata length, but changes their meanings and
requires a new transcript version. The straightforward verifier uses seven
scalar multiplications instead of five. At the EIP-1108 BN254 schedule, those
two extra multiplications cost **12,000 gas in precompile charges**, with
addition, hashing and call overhead to measure separately. This is an estimate
of the increment, not a measured transaction total or a guarantee for another
chain. [EIP-1108](https://eips.ethereum.org/EIPS/eip-1108).

A further encoding option sends only `(e, u, v)`. The verifier reconstructs
`T_key = uG - e*pk_a`, `T_R = vG - e*R_b` and
`T_diff = uR_a - v*pk_b - e*(C_a - C_b)`, then recomputes the challenge.
All three commitments in this *new* transcript are reconstructible. Omitting
their coordinates would save **192 calldata bytes** relative to three
uncompressed G1 points, with the same seven scalar multiplications. This is
not valid compression of the old transcript, which hashes `T1` and `T2`
separately but only constrains their difference. Compare both new encodings
after a Fiat–Shamir review; calldata savings may offset part of the additional
arithmetic cost, with the amount depending on the target chain's fee rules.

The challenge should bind both ciphertexts and account keys, parties,
`chainId`, registry address, action and version. If the proof is also meant to
authorize an allowance amount or an independently submitted request, bind its
amount, nonce and deadline in that authorization; distinguish this from an
ordinary transaction whose signature already authorizes its calldata. Reject
invalid encodings and disallowed infinity/zero keys. Require secure nonces and
reject identity-exposing registration ciphertexts such as `R_a = O` where
privacy requires nonzero encryption randomness.

**Privacy and storage:** this adds a hidden-key relation, not a public identity
or an opening authority. It needs no additional retained receipt data beyond
the updated transcript. It does not repair the public credential in section 1
or the admission policy in section 4.

**Required validation:** the exact freshly generated false-identity proof must
fail in the real Solidity verifier; honest proofs must agree across backends.
Test adversarial keys/ciphertexts, changed domains and repeated nonces, rather
than relying only on bit flips in an honest proof. Measure the repaired path
with the repository's pinned compiler and actual calldata.

## 4. Contract binding and accumulator admission lack certification

**Reproduced on a real EVM.** `_bindContract` requires
deployed code and an unbound address, then stores caller-supplied keys,
ciphertexts and flags. It requires no PS signature, verified binder, or
proof tying the identity to the operator. Atomic deploy-and-bind prevents
a competing first binder; it does not certify the supplied identity.
See `scripts/review/evm_uncertified_bind.py`: a fabricated `(pk, E)` and
an unrelated `identityLeaf = 12345` are stored, `isVerified(target)` is
true, and `identityRoot` advances. The same script registers a real PS
credential with `identityLeaf = Poseidon(M) xor 1`; the registry accepts
it.

The registration and contract-binding overloads also accept `identityLeaf`
without proving it hashes the identity in the registered ciphertext.
Consequently an accepted accumulator leaf is not, by itself, evidence of
KYC admission. An honest deterministic leaf additionally permits equality
linking between registrations that publish it, and candidate identity tests.

Source: [`_register`, `_bindContract`, `_insertIdentityLeaf`](../../src/IdentityRegistry.sol).

**Editorial consequence:** distinguish a stored binding, a verified
credential, and a certified accumulator member. The public-identity flag
records a policy assertion; it does not publish or authenticate a human name.

### Mitigation: separate control, certification and membership

These are three independent checks:

1. **Control:** the target contract authorizes the binding. A contract whose
   authority is external may instead use a narrow, governance-audited adapter
   that verifies canonical provenance and the protocol's actual governance
   identity. An existing contract otherwise needs an explicit
   controller/contract authorization mechanism. Do not assume every contract
   has an `owner()` or grant arbitrary-call deployment helpers this privilege.
2. **Certification:** the supplied identity and key are covered by the same
   credential presentation required for an ordinary account, or by an explicit
   certified service-operator policy. Deployed code alone supplies no identity
   evidence. Simply restricting the binder to a registered EOA is insufficient
   unless the proof also ties the target binding to the certified identity.
3. **Membership:** the admitted leaf is proven to contain that certified
   identity and the required holder secret/policy. A successful storage write
   cannot substitute for this relationship.

For an intentionally public service, a visible certified operator association
may be acceptable. For a private contract wallet, publicly pointing to the
owner's existing registered EOA defeats account unlinkability; use an independent
hidden presentation and an appropriate target authorization instead. Public
disclosure must contain authenticated identity material, not just a Boolean.

There are two ways to handle the tree. The smaller conceptual patch proves
the leaf's relationship during registration. It closes unbound admission but
retains linking if the leaf is still the deterministic identity hash. A more
private design uses issuer-authorized **randomized credential commitments**:

```text
leaf = Hash(domain, canonical identity digest, holder-secret commitment,
            credential policy, fresh high-entropy salt)
```

This is a schema sketch, not a finalized hash encoding. Enrollment proves
the committed fields match the issuer's certification. Public tree updates
can contain opaque salted leaves; account registration later proves membership
without revealing which leaf was used. A user can register several accounts
from one credential without publishing the same leaf on each account. Wallets
retain the opening and paths and update witnesses from public tree data.

The issuer may know which enrollment leaf belongs to whom. Privacy depends on
the presentation hiding the selected leaf and on avoiding timing or tiny-root
identification. Salt prevents outsiders' candidate tests when unknown to them;
it does not by itself hide an enrollment from the issuer.

Accepted roots must have an explicit authority and update policy. If governance
can substitute an arbitrary root, it can admit identities outside the claimed
policy. If it can force a root containing one selected user, it can also shrink
privacy. Revocation and expiry need hidden validity/nonrevocation proofs against
accepted current epochs, with adequate witness availability. A stable public
revocation handle would create a new linking channel. The current
`isVerified()` tests that a key is stored; it does not establish these policies.

**Gas and simplicity:** contract authorization adds an enrollment-time check.
An issuer-published aggregate root can amortize tree updates; moving paths into
a proof keeps them out of Solidity storage. It moves trust into certified root
admission and witness availability, which the paper must state. It does not
automatically reduce prover complexity or remove issuer governance.

**Required validation:** reject unauthorized first binding, fabricated service
identities, certified-identity/leaf mismatches and unaccepted roots. Verify that
two accounts using one credential reveal no common leaf. Exercise key rotation,
expired credentials and revocation without silently reverting to a stable
public identity identifier.

## 5. Sharing the public point does not bind the membership witness

**Reproduced with real Groth16 artifacts.** Using the committed circuit and
proving key (`build/snark/g1tie`), a proof was generated and verified for a
witness whose public `P` is opened by an identity that is **not** the tree
member `M`, by supplying `T = P - M`. An honest control proof also verifies.
See `scripts/review/g1tie_membership_mismatch.py`.
[`identity_membership_g1tie.circom`](../../circuits/identity_membership_g1tie.circom)
checks membership of `M` and the point addition `P = M + T`. It accepts the
point `T` as a witness; it does not prove knowledge of a scalar `b` such
that `T = bH`.

For any known member `M` and a suitable chosen public point `P`, a prover
can compute `T = P - M`. Thus the membership proof's underlying identity
need not be the identity opened by the accompanying sigma proof. Passing
the same `P` to both verifiers prevents changing `P`; it does not establish
equality of their hidden identities.

There is a second, independent mismatch:
[`issuer_reenc.py`](../../alberta_buck/wallet/issuer_reenc.py) defines the
public `H_SCALAR` and `H_POINT = H_SCALAR*G`, also used by the note-binding
wallet. The paper assumes an unknown discrete logarithm. For scalar-known
identities, two openings of one commitment are easy:

    m1*G + b1*H = m2*G + b2*H
    b2 = b1 + (m1 - m2)/H_SCALAR mod q.

There is a third, independent underconstraint (**5c, reproduced with real
Groth16 artifacts**). The circuit reconstructs `Mx_mod` as
`Mx[0] + Mx[1]·2^64 + Mx[2]·2^128 + Mx[3]·2^192` with **no 64-bit range
check** on the limbs, hashes `Mx_mod` for the Merkle leaf, and feeds the
raw limbs to `EllipticCurveAddOptimised`. A carry `Mx[0] += 2^64`,
`Mx[1] -= 1` keeps the Poseidon leaf identical and changes the EC-add
representation of `M`. `scripts/review/g1tie_limb_alias.py` proves and
verifies this witness against the committed zkey. The addition gadget is
also incomplete (`λ = (y2-y1)/(x2-x1)` is unconstrained at `x1 = x2`:
doubling, negation, infinity). These become load-bearing the moment 5a
is patched by proving `T = bH` without also pinning limbs and complete
addition.

**Editorial consequence:** withdraw the claimed composition theorem for the
current membership and commitment scheme. A fix must address witness
knowledge, binding of `H`, limb/range constraints and certified tree
admission. Unauthorized redemption of an addressed note is a separate
result (findings 7 and 8) and does not need this circuit: membership is
skippable.

### Mitigation A: repair the commitment and both proof statements

If separate proofs are retained, specify a binding commitment to a well-defined
message. For scalar identities, one candidate is `P = mG + bH`, with both proofs
establishing knowledge of `m, b` and the required relationships to `mG`, the
credential and ciphertexts. Merely supplying a point labeled `bH` is insufficient.

Choose `H` by an independently reproducible hash-to-curve construction for
which no participant knows `log_G(H)`. Hashing a string to a scalar and then
multiplying `G` publishes precisely the logarithm that must be unavailable.
The hash-to-curve design guidance in RFC 9380 is relevant; this is not a claim
that it supplies a ready-made BN254 suite for this repository.
[RFC 9380](https://www.rfc-editor.org/rfc/rfc9380.html).

There is a further composition detail: for an *arbitrary point* message `M`,
`P = M + bH` admits the transformation `M' = M + delta*H`,
`b' = b - delta`. Thus changing `H` and proving knowledge of `b` is not by
itself a general binding theorem for unrestricted point messages. Require the
appropriate scalar representation knowledge on both sides, or use a binding
commitment to a canonical message encoding whose opening both proofs establish.
The complete argument must show why an extracted alternative identity opening
contradicts an actual assumption. Valid group points and canonical limb/range
constraints are part of that statement.

**Cost:** changing the fixed generator need not add Solidity arithmetic, but
requires new constants, fixed-base tables, circuits and proof artifacts.
Constraining `bH` or `mG` inside a circuit can be expensive in prover time and
memory. Its Groth16 verification cost need not grow with constraint count when
the public-input count stays fixed. Keeping separate proofs still pays for
separate verifications and leaves a composition argument to maintain.

### Mitigation B: use the same private witness in one proof

The more attractive candidate for simple Solidity is to fold certified
membership into the note-binding relation. One circuit should use the *same*
private identity variable for the credential leaf, ciphertext relation and
addressed recipient/issuer condition. This removes the need to infer their
equality merely from two uses of `P`.

The existing note-binding circuits already perform substantial non-native
curve arithmetic. Adding a Merkle path may be a better trade than preserving
an additional verifier and an unsound commitment bridge. It increases witness
work and setup artifacts; it may reduce verifier calls. Benchmark both rather
than assume the larger circuit is more expensive on chain.

**B1 has no note-binding.** `spendCoupledB1` never calls `_verifyNoteBinding`.
Folding membership into the A1/A2 note-binding circuit leaves B1 still
depending on g1-tie (or on the empty-proof skip in section 8). A sound
repair is either a B1-specific combined circuit that uses the same private
`m_dep` for the depositor binding, `P_dep`, and the Merkle leaf, or a
repaired standalone membership circuit that B1 actually invokes. Do not
delete g1-tie until that B1 path exists.

This must cover every boundary. Putting membership into note binding alone
does not connect a separately verified sigma proof to the same identity unless
that connection is sound too. Either retain a correctly binding common
commitment with the necessary knowledge relations, or include the relevant
registered-key, ciphertext and key-ownership relations in the combined proof.
Use one hash-to-curve `H` everywhere a Pedersen generator is required; do
not keep `H_SCALAR·G` “for issuer_reenc” alongside a second unknown-log `H`.
Nor does any circuit fusion certify an arbitrarily admitted leaf: section 4
still applies. Section 7's note flavor and issuer checks must also be enforced.
Range-check every 64-bit limb and use a complete (or explicitly handled)
addition formula.

**Privacy and storage:** both directions can keep the selected identity and
membership path hidden. Wallets store salts, secret openings, paths and proving
material. Solidity can remain a verifier plus roots and nullifiers. A single
proof may simplify that contract while making the circuit a larger and more
important review target.

**Required validation:** construct the existing `T = P - M` witness, the
limb-carry alias, and the known-log double opening against the actual
artifacts. The repaired verifier must reject all three and any cross-proof
identity substitution. Prove the composed relation formally, and regenerate
circuit, proving key, verification key, Solidity verifier and fixtures as a
matched version. Changing only the wallet does not stop a malicious prover.
B1 must still have a sound membership path after any fold into note-binding.

## 6. The observer model excludes actors who have useful information

**Source-established.** Notes issuers and depositors identity-approve the
pool. Those approval ciphertexts are decryptable with the pool operator's
key. The operator can identify pool users from these records; the review
has not established that this alone identifies every mint-to-spend edge.

Identity scalars derived from canonical records are known to their KYC issuer
and to counterparties receiving those records. They cannot simultaneously
serve as decryption keys exclusive to their subjects. Disclosure of a
receipt's identity records may enable tests against other public transcripts.

Public metadata also includes registration signatures and optional leaves,
mint accounts and batch totals, commitments and issuer mode, A2 mint
ciphertexts, spend faces, payout accounts and timing. Hiding an opening
does not eliminate inference from this metadata. A singleton compatible
anonymity set offers no uncertainty about the source.

Sources: [Notes](../../src/Notes.sol),
[lifecycle setup and pool approvals](../../alberta_buck/sim/notes_stack.py),
[receipt envelope](../../alberta_buck/wallet/envelope.py).

**Editorial consequence:** specify adversaries separately: public observer,
credential issuer, pool operator, previous counterparty, colluding users,
and compromised wallet. Replace absolute or everlasting confidentiality
claims with conditional computational claims about a defined view.

### Mitigation: make evidence access specific to the payment parties

First separate the infrastructure service from the economic counterparty.
A pool holding funds need not receive the identities of everyone using it.
Replace identity-approvals to the pool with proofs of the eligibility and
payment conditions the pool actually needs. Route encrypted identity evidence
to the actual payer/payee under independently authenticated keys. This changes
the present token/pool approval interface; removing an approval without replacing
its admission and receipt guarantees would lose intended safeguards.

Replace identity-derived encryption keys such as `m_rec*G` with independent
wallet-generated encryption keys. A hidden credential proof must bind a fresh
receiving key to its certified holder and to this payment. Otherwise an attacker
can substitute its own key, or a participant can encrypt deliberately false
evidence. Do not publish a stable receiving key beside every note if cross-note
unlinkability is required. Interactive authenticated key exchange and privately
obtained prekeys are possible delivery designs, with different availability
and directory-trust costs.

Cheap encrypted storage supports a stronger receipt design:

- Each participant keeps the payment-specific identity evidence, certificate
  or transaction-bound presentation evidence, signed payment context, encryption
  openings needed for selective verification, and chain anchors. Keep reusable
  credential and spending secrets in a separate wallet compartment.
- Bind the receipt to the note/payment, amount, parties' roles, network,
  contracts and protocol version. Where a public anchor is needed, commit to
  a canonical receipt with fresh secret salt, or incorporate that commitment
  into the note statement. An unsalted hash of predictable personal data is
  itself a candidate-testing interface.
- Encrypt the full receipt to its intended participant with authenticated
  encryption. A store, backup provider or relayer receives ciphertext. Public
  content addresses and retrieval logs can still expose access patterns.
- For a later dispute, disclose that payment's authenticated evidence and
  necessary opening, or a selective proof about it. Do not disclose a master
  key, holder secret or reusable credential. A scoped decryption proof is
  preferable to revealing a key reused across transactions.

This preserves the useful target: **participants retain verifiable evidence
without a pool-wide identity-opening key**. A recipient can still copy or
publish identity information it legitimately learned. Cryptography can limit
what the receipt opens elsewhere, but cannot make already disclosed facts
secret from that recipient.

### Availability and retention are separate guarantees

A commitment proves neither receipt delivery nor readable contents. An
interactive recipient can decrypt, verify and acknowledge the receipt before
authorizing acceptance. A noninteractive flow instead needs a proof that the
committed evidence is correctly encrypted to the intended key and matches the
certified transaction. To claim that an accepting participant can recover it,
also establish appropriate key possession and ciphertext availability. Merely
proving that some key exists does not ensure the intended person received it.

Large wallet archives make honest retention inexpensive. They cannot prevent
colluding parties from deleting data or secrets, or compel a future disclosure
after deletion. State separately: evidence was correctly formed; evidence was
available/decryptable at acceptance; an honest wallet retained it. Permanent
recoverability without participant cooperation would need additional retained
copies or an opening authority and a different privacy/trust model. Do not
silently introduce such an authority as the repair.

### Residual metadata and costs

Fresh accounts/keys, private relay submission, delayed/batched redemption and
larger common root sets can reduce correlations. Reused payout addresses,
distinctive amounts and publicly visible funding paths can still identify
participants. Fixed denominations or confidential-value proofs address some
amount leakage but change liquidity, transaction count or circuit cost. State
which metadata the intended privacy experiment allows the adversary to see.

Moving full receipts off chain can avoid their calldata/storage cost. It does
not remove the proof needed to enforce their contents. If correctness is only
checked by an honest recipient off chain, the guarantee against colluding users
is weaker and must be described that way. A combined payment proof can enforce
the relation with small public inputs, at increased proving cost. Archive
encryption by itself usually needs no additional Solidity work.

**Required validation:** consider the public observer, issuer, pool operator,
previous counterparty and their coalitions separately. Give each its actual
records and keys, then test identity candidates and transaction correlations.
Verify that revealing one receipt does not yield another payment's decryption
or authorization key. Test missing ciphertexts, malicious receiving keys,
incorrect evidence, backup recovery and key rotation.

## 7. The B1 spend path does not prove the note's flavor or issuer

**Reproduced on a real EVM with a real spend SNARK.**
[`spend.circom`](../../circuits/spend.circom) accepts a private `flavor` without
constraining it to the entry point's mode. Its comment explicitly acknowledges
acceptance of A1/A2/B1 openings. In
[`spendCoupledB1`](../../src/Notes.sol), the common spend proof establishes
membership, value and nullifier, while the additional checks concern the payout
account and a caller-supplied `issuer`. They do not establish that the spent
note is B1 or that this `issuer` is the issuer committed by the note.

`scripts/review/evm_a1_via_b1.py` mints the committed A1 e2e note, then a
third party Mallory — given only the A1 opening — generates a fresh spend
proof that pays her address (public inputs omit flavor), produces a B1
depositor binding on her keys, and calls `spendCoupledB1` with an empty
membership proof. The pool pays Mallory the note's face; the addressee
receives nothing; the nullifier is consumed. A depositor binding naming a
**substitute** registered issuer also verifies. This is unauthorized
redemption of an addressed note, not a public-observer spend without an
opening. Fixing sections 1–5 alone would not add these missing relationships;
section 8 is what made the G1-tie check unnecessary for this spend.

### Mitigation: bind entry-point policy and issuance evidence to the note

Constrain the committed flavor to the flavor accepted by the chosen verifier.
Either use a flavor-specific circuit/key, or expose a constrained mode input
and have Solidity supply/check the expected value. The existing entry point
already reveals the selected mode. Never let an unproved wallet label choose
which authorization checks the contract omits.

The B1 proof must also connect the supplied issuer's authenticated key/identity
to the note's committed issuance material. Define the issuance payload and its
signature verification precisely. If a note commits a signature, that signature
cannot straightforwardly sign the final commitment containing itself: use an
independently defined issuance payload and, where needed, a separate signature
over the completed batch. Merely adding an `issuer` public input without a
constraint connecting it to `idHash` does nothing.

Apply the same review to `predicate`: committing an arbitrary word is not
enforcing its spending condition. Constrain the currently supported default
and reject unsupported predicates, or implement and prove their semantics.

**Gas and simplicity:** a fixed-flavor circuit can retain the same public-input
count and proof size; a mode equality is cheap in the circuit. A generic
verifier with more public inputs has additional verification cost. Authenticating
issuance material may add significant proving work, so benchmark the complete
statement. Neither fix inherently requires public disclosure of an A2 issuer.
For B1, the issuer is already intended to be public.

**Required validation:** attempt every note flavor through every spend entry
point using freshly generated proofs, substitute the issuer while retaining
the opening, and exercise unsupported predicates. Check all enabled legacy
paths as well as the advertised coupled paths. These are semantic substitution
tests; rejection of a mutated proof's bytes does not cover them.

**Editorial consequence:** addressed-only redemption and bilateral evidence
must remain design goals until every enabled redemption path enforces them.

## 8. Empty proofs and unset verifiers skip membership and note-binding

**Reproduced on a real EVM; also tested as intended behaviour in Foundry.**
[`_verifyIdentityMembership`](../../src/Notes.sol) and
[`_verifyNoteBinding`](../../src/Notes.sol) return if the verifier is
`address(0)` **or** `proof.length == 0`. The skip is documented as
backward-compat and asserted by
`test_coupledA2_emptyMembershipProof_skips` /
`test_coupledA2_emptyNoteBinding_skips`. Governance can also wire
`StubIdentityMembershipVerifier` (`return enabled`).

The skip fires **even when the real G1-tie adapter is wired**. Combined
with section 7, an A1 opening plus any registered identity redeems through
B1 without a membership proof, and BUCK moves (same script as section 7).
Circuit repairs in sections 5 and 7 have no on-chain effect until these
paths fail closed.

Sources: [`Notes.sol`](../../src/Notes.sol) (`_verifyIdentityMembership`,
`_verifyNoteBinding`), [`StubIdentityMembershipVerifier.sol`](../../src/StubIdentityMembershipVerifier.sol),
[`test/NotesCoupledA2.t.sol`](../../test/NotesCoupledA2.t.sol).

**Editorial consequence:** do not describe coupled spends as enforcing
membership or addressed-binding while empty proofs succeed. State the
migration skip as a currently enabled spend surface.

### Mitigation: fail closed

When the corresponding verifier is set, require a nonempty proof and
let the verifier reject it. Refuse `address(0)` verifiers on coupled
paths in any production constructor or governance call; do not ship
`StubIdentityMembershipVerifier` outside tests. Invert the two Foundry
“empty proof skips” tests in the same commit as the Solidity change.

This is a one-require change, no trusted setup, and it is a prerequisite
for sections 5 and 7 having any on-chain effect. After it lands, the
section 7 reproduction must be re-run: the same A1-via-B1 call should
revert, and an honest B1 control with a real membership proof should
still pay.

**Required validation:** empty membership on B1, empty note-binding on
A1/A2, unset verifier, stub verifier, and a nonempty invalid proof each
revert; honest coupled spends with real proofs still settle.

## 9. Registration does not prove knowledge of the account key

**Reproduced in the wallet verifier and on a real EVM.** The registration
NIZK proves that a rerandomized PS signature and an ElGamal ciphertext
share a message `m`. It never proves `pk = sk·G`.
`scripts/review/evm_uncontrolled_register.py` registers under a
hash-to-point public key with no known discrete log; `isVerified` is
true. Sibling protocols already prove key ownership (deposit coupling E4,
depositor binding E4, issuer re-encryption L4). Approve (section 3) and
register are the ones that omit it.

Harvesting (section 2) already uses an attacker-chosen key the attacker
does hold, so this gap is not required for that attack. It does allow
registering a credential under a key the holder cannot decrypt, and it
leaves registration Fiat–Shamir without `chainId` or registry address
(cross-chain replay if issuer keys are reused).

Sources: [`nizk.py`](../../alberta_buck/wallet/nizk.py),
[`_register`](../../src/IdentityRegistry.sol).

**Editorial consequence:** “this account is controlled by the credential
holder” is not a property of the implemented registration proof.

### Mitigation: add the missing key relation, with the same witnesses

Extend the registration sigma with `pk = sk·G`, exactly as section 3
adds it to approve (and as deposit coupling already does). Reject
infinity/zero keys. Bind `chainId`, registry address, protocol version
and a domain separator into the Fiat–Shamir transcript. A one-relation
Schnorr prototype is `prove_key_ownership` /
`verify_key_ownership` in
[`mitigations.py`](../../alberta_buck/review/mitigations.py).

Do not wait for the hidden-credential redesign (sections 1–2) to add
this check. It is the same missing equation as approve, and it should
land in the same work package.

**Required validation:** NUMS and third-party public keys rejected;
honest key accepted; replay under a different chain or registry rejected;
four-backend vectors regenerated together.

## Other conclusions that must be narrowed

### Canonical identity, uniqueness and intent

Hashing canonical records binds bytes under a collision-resistance assumption.
It does not establish one immutable identifier per human. A renewal, changed
name, new issuer or different issuance timestamp can create a different record
for the same person. Two issuers may disagree; a malicious issuer may certify
false data. See the [identity encoding](../../alberta_buck/wallet/identity.py)
and the [identity paper](../../alberta-buck-identity.org).

**Mitigation:** define separately the subject, credential instance, certified
attributes, issuer policy and validity interval. Preserve exact historical
bytes in receipts so renewals do not change the meaning of past evidence.
If a particular application needs one action per eligible person, define its
enrollment/deduplication trust and use a scope-specific pseudonym/nullifier
derived from a certified holder secret. That deliberately links actions within
the scope. It does not establish cross-issuer human uniqueness unless admission
enforces it, and a globally reused nullifier would sacrifice account privacy.

A valid signature or proof establishes use of a key/secret under stated
assumptions, not informed consent or absence of coercion. Secure wallet review
screens, clear transaction context and recovery policies reduce misuse; they
do not turn a cryptographic theorem into a theorem about human intent. Receipts
should say what was authenticated and by whom, leaving real-world attribution
to the enrollment evidence and surrounding facts.

**Cost and validation:** most of this is schema, wallet and policy work. A
public scoped nullifier adds state only where the application actually needs
uniqueness. Test renewed records, duplicate enrollment, multiple issuers and
cross-scope linking. Do not add global tracking merely to simplify deduplication.

### B2: identify the exact information restriction

The B2 argument concerns an issuer secret hidden from previous holders of an
unchanged, copyable bearer opening but recoverable by its final holder. If both
possess the same relevant secret state and public information, any decryption
the final holder can perform is available to the previous holder too. Naming
one holder “final” does not give it a new cryptographic capability. This is
narrower than ruling out bearer payments with a private issuer.

**Mitigation:** choose which privacy requirement is needed. An issuer identity
encrypted under a bearer secret carried with the note can be private from
nonholders while readable by every holder. This can exploit encrypted wallet
storage and need little additional on-chain data, but it gives up issuer
secrecy from intermediate holders and may need proofs of correct evidence.

If only a later holder should receive new evidence, change the information
available: an issuer-assisted exchange, fresh recipient key or private
spend-and-reissue protocol can deliver a new capability. That changes
interaction, availability and possibly on-chain cost. Reissue must invalidate
the old spend capability; handing over a copyable opening cannot make earlier
copies unusable. Large local storage helps honest custody but cannot prove
the previous holder erased its copy.

**Required validation:** enumerate each holder's state and what changes during
transfer. State separately issuer privacy from outsiders, from prior holders,
and from the final recipient. Evaluate any revised B2 proposal against the
chosen property rather than promote a stronger impossibility claim.

### Anonymous membership does not imply a particular proof system

If the requirement is to prove membership without naming the member, some
mechanism must establish that relation. This does not prove a lower bound
requiring a Merkle tree, Groth16 or any SNARK. A private credential presentation
may demonstrate eligibility directly; another construction may prove membership
in certified commitments. The trust in issuance/admission remains in either
case.

**Mitigation:** describe the chosen construction as an engineering choice.
Compare direct anonymous credentials, combined circuit membership proofs and
post-quantum alternatives against the *same* statement and adversary model.
Include verification gas and calldata, proving time/memory, setup assumptions,
revocation, witness updates and issuer-view privacy. A smaller signature is
not automatically a smaller complete transaction.

For this project, the existing precompiles and circuits make a repaired
classical construction a reasonable baseline to benchmark. Large user storage
can accommodate membership paths and credentials, potentially avoiding some
on-chain storage. It does not establish that a specific accumulator is either
necessary or optimal. The PQ alternatives below are candidates for comparison,
not established drop-in replacements.

### Receipts: encoding, authenticity and historical context

The implemented wire encoding is canonical JSON, not the CBOR described in
the receipt paper. Offline verification checks embedded data; without an
independently authenticated trust anchor it cannot establish that an embedded
registry key was ever registered or that a claimed transaction occurred. The
paper's three printed RPC checks do not implement its complete proposed tier-2
procedure. Sources: [envelope](../../alberta_buck/wallet/envelope.py),
[receipt verifier](../../alberta_buck/wallet/verify_receipt.py),
[receipt paper](../../alberta-buck-receipt.org).

**Mitigation:** document the actual JSON encoding and version it. Specify
canonical handling of numbers, strings and unknown fields; distinguish the
short receipt identifier from an authentication mechanism. A hash detects a
change relative to an independently trusted hash, not who authored the data.
Canonicalization does not authenticate a name.

Implement the promised anchored verifier explicitly. It should establish the
network and intended deployed contracts; successful transaction and correct
event/log; matching amount, recipient and nullifier/commitment; and relevant
registry keys, credential validity and mint evidence at the required historical
state. State the block finality and RPC/light-client trust assumptions. A
current registry lookup can be misleading after key rotation, and a chain ID
alone does not distinguish every fork or local replay environment.

Wallet archives can retain exact identity records, full transcripts, block
hashes and authenticated historical evidence. Offline verification can become
stronger if that evidence is anchored to a trusted checkpoint and includes the
necessary proofs; self-contained data is not automatically self-authenticating.
Distinguish algebraic consistency, certified attribution and chain occurrence
in the verifier's result. The public-identity flag alone establishes none of
the missing attribution relationships.

**Cost and validation:** corrected documentation and wallet-side anchoring add
no required Solidity gas. Test invented registry keys, unrelated events,
reverted transactions, wrong networks/contracts, reorged blocks, expired
credentials and historical rotation, not only malformed serialization. End-to-end
claims must identify test harnesses: the current
[Notes fixture stack](../../alberta_buck/sim/notes_stack.py) uses
`BuckCreditHarness` and contract-binding setup, so its successful lifecycle
does not by itself demonstrate authentic PS enrollment of every participant.

### Solvency is independent of identity and proof soundness

A proof can conserve token amounts while the credited assets have inadequate
economic backing. The [Ethereum paper](../../alberta-buck-ethereum.org) itself
identifies unvetted self-issued insurance credits as a potential unbacked-issuance
path. The exact reachability and extent in the current credit/issuance contracts
still require focused verification; this review has not executed that economic
counterexample.

**Mitigation:** specify who can create eligible backing, how it is valued, who
bears default losses, and what enforceable resource supports an insurer's
promise. Depending on the intended monetary design, candidates include
authenticated insurer admission, exposure caps, independently valued collateral
or constrained acceptance of credit. Each changes governance, capital use or
market participation; identity certification alone cannot supply missing assets.
Requiring a named insurer may improve accountability without guaranteeing its
solvency.

Cheap wallet archives can preserve contracts and historical backing evidence,
but they cannot make an insolvent promise redeemable. Keep private transaction
identities separate from whatever aggregate backing information users need to
assess risk. Simple eligibility/cap checks may be inexpensive; price feeds,
collateral liquidation and discretionary insurance introduce larger dependencies.
Do not promise that a cryptographic fix preserves all economic assumptions.

**Required validation:** trace every admission, valuation and mint path; test
self-insurance, circular backing, related-party issuers, valuation shocks and
default. Distinguish conservation invariants from claims about purchasing
power, redemption and loss absorption in the papers.

## Post-quantum options and what they would preserve

Post-quantum alternatives are worth evaluating, especially for evidence that
must remain private for decades. “PQC versions of ElGamal and rerandomizable
signatures” should be treated as a request for equivalent *functions*, not an
assumption that the existing equations survive an algorithm substitution.
Changing the hardness assumption does not repair any missing relation above.

### 1. Protect wallet archives and delivered receipts first

ML-KEM establishes a shared secret for use with symmetric encryption. It is
a useful candidate for encrypting receipt packages; it is not specified as
ElGamal with its additive relations and Chaum–Pedersen proofs. ML-DSA supplies
digital signatures, not an anonymous rerandomizable credential presentation.
[FIPS 203](https://csrc.nist.gov/pubs/fips/203/final),
[FIPS 204](https://csrc.nist.gov/pubs/fips/204/final).

Use a reviewed KEM-plus-authenticated-encryption construction, with authenticated
recipient keys, explicit algorithm versions and downgrade protection. A reviewed
hybrid classical/PQ construction is an option during migration; its combiner
and security model must be specified, rather than improvised. Larger keys,
ciphertexts and signatures are comparatively easy to accommodate in the
assumed wallet storage. Receipt authenticity can use a separately certified
PQ signing key without publishing the identity to the chain.

Check receiver anonymity separately: message confidentiality does not by
itself promise that a ciphertext hides which public key it targets. Public
key identifiers, prekey retrieval and ciphertext structure belong in that
analysis. Authenticated encryption also intentionally resists modification;
do not assume its ciphertexts support ElGamal-style public rerandomization.

This can leave Solidity unchanged **for archive protection alone**. If the
contract must enforce correct formation of a PQ receipt against malicious or
colluding participants, the proof must verify the relevant encryption,
certification and transaction binding. That is additional circuit work, not
something achieved merely by wrapping the old receipt in encryption.

Local backup encryption is also distinct from transport encryption. Encrypting
a backup with a strong symmetric key does not protect a receipt whose network
ciphertext or public on-chain identity ciphertext is later broken. Independent
payment keys limit selective-disclosure scope; forward secrecy after compromise
also requires an appropriate key lifecycle. Retaining all old private keys
under one recoverable master key gives that master key access to the archive.

### 2. Evaluate PQ anonymous credentials as complete protocols

Lattice-based anonymous credential constructions exist. For example, Bootle,
Lyubashevsky, Nguyen and Sorniotti give a framework for blind signatures and
anonymous credentials based on explicitly stated lattice assumptions. It is
a relevant research candidate, not evidence that this repository already has
a PQ credential implementation.
[Primary paper](https://eprint.iacr.org/2023/560).
The LaZer project supplies lattice-proof, blind-signature and anonymous
credential examples useful for prototyping and measurements.
[LaZer implementation](https://github.com/lazer-crypto/lazer).

A candidate must supply issuer-view unlinkability, hidden holder-secret
binding, attribute certification, repeated showing, revocation and the needed
encryption/plaintext relations. Lattice relations require their own bounds,
noise management, correctness and zero-knowledge arguments. A rerandomization
operation by itself supplies none of those composition guarantees. Repeated
rerandomization may also have scheme-specific limits that need measurement.

The Solidity tradeoff is unresolved. The current BN254 precompile calls do not
verify lattice proofs. A direct verifier needs a new implementation and actual
gas/calldata measurements; a general proof of its verification shifts work to
the prover. Large wallet storage absorbs artifacts but does not reduce proof
bandwidth or EVM verification instructions. Compare complete transactions,
including worst-case valid inputs, rather than signature size alone.

### 3. Consider avoiding on-chain signature rerandomization altogether

The certified-commitment approach in section 4 is another PQ migration route.
An issuer certifies enrollment, a root authorizes randomized credential
commitments, and users prove hidden membership plus holder authorization and
payment relations. The on-chain proof need not expose or rerandomize a
credential signature. PQ signatures can authenticate enrollment evidence or
root authorization, subject to a precise trust and verification design.

For end-to-end PQ proof soundness, the proof system must also change. Hash-based
transparent proof systems are a direction to investigate; the original STARK
work provides a starting point. Select an actual zero-knowledge construction
and quantum-security parameters rather than assume every transparent proof
has the required privacy properties.
[Primary STARK paper](https://eprint.iacr.org/2018/046).

This can preserve a conceptually small contract interface—verify a proof,
check roots/nullifiers, update state—while increasing verifier code, calldata
and gas. Batching or a validity rollup may amortize those costs, with explicit
data-availability and exit assumptions. Wrapping a PQ proof in BN254 Groth16
can make verification cheaper, but the chain's acceptance then still relies
on a classical pairing proof. It is not an end-to-end PQ soundness solution.

Hash commitments, nullifiers, encryption, credential/root authentication,
account authorization and the host chain all need a compatible security target.
Do not assume the current curve, hash output sizes or circuit parameters meet
it unchanged. A PQ receipt system does not migrate Ethereum's account and
consensus authentication for it.

### 4. Separate future confidentiality from future forgery resistance

Published BN254 ElGamal ciphertexts can be decrypted by an adversary capable
of solving the relevant discrete logarithms. Reencrypting a wallet backup or
switching tomorrow's signatures cannot erase yesterday's public ciphertexts.
Avoid publishing new long-lived identity information under a classical key
if confidentiality against that future adversary is a requirement.

Future proof-forgery resistance and confidentiality of old proof transcripts
are distinct questions. A later break of proof soundness does not by itself
prove that every historical zero-knowledge transcript reveals its witness.
Audit the exact encryption and proof transcript privacy assumptions separately.
To promise durable privacy, the public transcript must contain no other
identity-recovery path after the proposed upgrade.

Migration therefore needs versioned credentials, receiving keys, roots, proofs
and receipt formats; an explicit policy for old notes; and archived decoding
and verification context. Do not interpret an old classical receipt as PQ
authenticated merely because it is stored in a PQ-encrypted archive.

## Recommended order and cost comparison

Start by specifying the complete security statement and preserving the current
counterexamples as executable evidence. Then compare a minimally repaired
classical path with a combined-proof design before undertaking a full PQ
replacement. The wallet-storage assumption makes private evidence and key
separation especially attractive in either design.

| Change | Privacy benefit | Solidity/gas implication | Main remaining work |
| --- | --- | --- | --- |
| Fail-closed membership/note-binding (§8) | Stops skipping the checks the paper claims to enforce | One extra `require`; no new proof | Invert Foundry skip tests; re-run A1-via-B1 (must revert) |
| Complete approval relation (§3) plus registration key relation (§9) | Prevents false-identity approvals and keyless registration without exposing the identity | Same-size or 192-byte-smaller candidate encoding; estimated +12,000 gas for extra multiplications before other cost changes | Formal transcript, all backends, EVM adversarial tests and benchmark |
| Bind spend flavor/issuer (§7) | Addressed notes stay addressed; caller cannot pick which checks to skip | Public flavor/mode input is cheap; issuer authentication may add proving work; fresh spend setup | All-entry-point tests with real proofs |
| Hidden credential plus holder secret (§1–2) | Removes raw credential tests/harvesting and separates disclosure from authority | New presentation verifier or circuit; cost not yet measured | Issuance, showing, recovery and issuer-view analysis |
| Certified private admission (§4) | Removes arbitrary leaves and repeated public identity leaves | Enrollment authorization and root checks; aggregation can amortize updates | Root trust, revocation and witness availability |
| Combined membership/payment relations (§5, §7), including a B1 path and limb/range checks | Enforces one identity and the correct note policy without extra identity disclosure | Potentially fewer verifier calls, more proving work | Full statement, new artifacts, B1 combined circuit, all-entry-point tests |
| Participant-specific encrypted receipts (§6) | Removes infrastructure-wide evidence access and limits disclosure scope | Archive encryption alone adds no required gas; enforced receipt correctness needs a proof | Authenticated keys, delivery, retention and selective verification |
| PQ archive/receipt encryption | Protects the upgraded ciphertexts under the selected PQ assumptions | Off-chain only unless encryption correctness is enforced on chain | Reviewed suite, key lifecycle and migration |
| Full PQ credential/payment proof | Targets future quantum forgery resistance and transcript privacy | New verifier economics; likely needs batching evaluation | Complete construction, implementation, parameters and host-chain assumptions |

Do not preserve a low gas figure by omitting the very relationship the paper
claims to prove. Conversely, avoid placing large archives, identity records or
full credentials on chain when a small proof can establish the needed fact.
Report enrollment, approval, mint and redemption costs separately, alongside
wallet proving time/memory, archive size, calldata and any added operator trust.

## Work completed and remaining tasks

Updated 2026-09-13. Preserve the implementations already written. Production
protocol source is still unchanged; the reproductions below assert the
current failures. The costed repair order is in
[mitigation-implementation-guide.md](mitigation-implementation-guide.md).

Existing work:

- [Wallet counterexamples](../../alberta_buck/review/examples.py) generate fresh
  registration/approval attacks, commitment double openings, a mismatched
  membership witness, a limb-carry alias, and a registration under a NUMS
  public key.
- [Mitigation prototypes](../../alberta_buck/review/mitigations.py) include the
  compact three-relation approval, an illustrative independent generator,
  salted credential/payment commitment helpers, a one-relation key-ownership
  Schnorr, and the fail-closed membership predicate. These are research examples.
- [Wallet tests](../../alberta_buck/test/review/test_wallet_failures.py) cover
  these examples, identity-derived decryption, copied bearer secrets, identity
  renewal, offline receipt verification, A1 spend public inputs omitting
  flavor, and the empty-proof model. Both `py` and `kernel` backends pass.
- [Python integration helpers](../../alberta_buck/review/integration.py),
  [Node proof helper](../../scripts/review/groth16.cjs),
  [test fixtures](../../alberta_buck/test/review/conftest.py) and the
  [credential/payment circuit](circuits/credential_payment.circom) are preserved
  scaffolding. The circuit has not been compiled or proved in this review.
- [Executed reproductions](../../scripts/review/):
  - `evm_approval_forgery.py` — finding 3 on deployed `IdentityRegistry`
  - `evm_uncertified_bind.py` — finding 4 (fabricated bind + unmatched leaf)
  - `g1tie_membership_mismatch.py` — finding 5a (`T = P - M`)
  - `g1tie_limb_alias.py` — finding 5c (limb carry accepted)
  - `evm_a1_via_b1.py` — findings 7 and 8 (A1 opening spent via B1 by a
    non-addressee with empty membership; BUCK moved)
  - `evm_uncontrolled_register.py` — finding 9 (NUMS `pk`, no `sk`)
  - `silmarils_model.py` — SILMARILS orthogonality

Remaining tasks, in dependency order:

1. **Land P0-0 (fail closed)** in `Notes.sol`. Invert the Foundry empty-proof
   skip tests. Re-run `evm_a1_via_b1.py`: the attack must revert; an honest
   B1 control with a real membership proof must still pay.

2. **Land P0-A** (three-relation approval + registration `pk = sk·G` + domain
   / registry / nonce / infinity checks) across the four backends. Invert
   `test_03`, `test_09`, `evm_approval_forgery.py` and
   `evm_uncontrolled_register.py` in the same commits.

3. **Land P0-B** (public flavor/mode on spend, issuer bound to issuance
   material, predicate pinned). Re-run A1-via-B1 with nonempty membership:
   an A-opening through B1 must revert.

4. **Complete cross-backend verification** of the repaired transcripts.
   Replay the same malicious and honest inputs through Rust/PyO3 and JS.
   Randomized Groth16 proof bytes need not match; accept/reject must.

5. **P1-A circuit work**, including a B1 combined path, hash-to-curve `H`,
   64-bit limb range checks and complete addition. Rebuild
   `g1tie_membership_mismatch.py` and `g1tie_limb_alias.py` against the
   new artifacts (both must fail). Matched-set keys.

6. **P1-B / P2-A specification** before implementation. Meanwhile withdraw
   the paper claims listed in the editorial consequences.

7. **Validate the compact approval repair on EVM** and measure gas with the
   pinned compiler; the current 12,000-gas increment is arithmetic
   estimation.

8. **Complete the larger credential/payment relation experiments.** Compile
   the preserved review circuit using isolated development artifacts. Its
   current statement is schematic: it does not implement PS/blind issuance,
   B1 semantics or full Notes spending.

9. **Prototype participant-held receipts and PQ protection** as a separate
   axis (section 6 and the post-quantum section). Keep archive protection
   distinct from credential/proof security.

10. **Finish receipt anchoring and the non-cryptographic checks.** Treat
    human uniqueness, intent, permanent retention and economic solvency as
    claims with explicit external assumptions.

11. **Publish the evidence and resume the paper edits** per
    [editorial-map.md](editorial-map.md).

## Verification limits

Wallet findings use the local Python environment with backends `py` and
`kernel`. Pairing checks dispatch to the compiled backend. EVM
reproductions use in-process revm (`PyrevmAnvil`), not a public network.
Groth16 reproductions use the committed `build/snark/` artifacts and
`snarkjs`. No production protocol source was changed.

The compact approval prototype and the 32-example algebra check remain
what they were: a candidate repair, not a production transcript. Real
EVM/Groth16 **attacks** for findings 3, 4, 5a, 5c, 7, 8 and 9 have been
executed; WASM replay of those transcripts, circuit-mitigation execution
and PQ examples remain pending. Finding 5a has not been driven through
the deployed G1-tie **adapter** (off-chain snarkjs verify only). The
paper revision must distinguish implemented behaviour, review prototypes
and remaining proof obligations.

Residual items not elevated to numbered findings: the spend SNARK
ghost-binds `recipient` and `chainId` but not the Notes contract address
(clone replay if a root is copied); `_verifyApprove` relies on EIP-196
rejecting off-curve points rather than an explicit `isOnCurve`; Groth16
trusted-setup toxic waste is an operational assumption.
