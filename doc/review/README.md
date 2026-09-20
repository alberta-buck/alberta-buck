# Alberta Buck paper review

The review covers all 19 root `alberta-buck-*.org` papers, read in full.
It is ongoing; the papers themselves have not yet been revised.

- [Reading and working record](2026-09-12-editorial-audit.md)
- [Identity findings, evidence and mitigation options](identity-findings.md)
- [Audience map and editing decisions](editorial-map.md)
- [Mitigation implementation and testing guide](mitigation-implementation-guide.md) -- costed, prioritized handoff for landing the repairs
- [Issuer-view follow-up (org)](identity-findings-2.org) -- R1-R6 with executable recipes, the launch-gate framing (pre-launch, nothing to migrate), and the unreviewed Pathway A' candidate
- [A' security argument](../../alberta-buck-proofs.org) -- lives in the proofs paper: Part I (assumptions with the PS-CM key form, the hiding, unforgeability and issuer-blindness games, Theorems 1' to R-blind with proofs, randomness breaks, verifier-check rationale, eight-step sanity block) and the closing section "Review Status and Remaining Work for Part I"; awaiting independent review
- [Accumulator specification (org)](accumulator-spec.org) -- v0.4, unreviewed: two leaf functions, private and public tree classes, the root ring, admission, salt derivation and recovery, revocation and re-association, the attribute vocabulary, both proof statements, the insurer envelope, delegated authority for community sub-regulators, deployment and governance with an accountable aggregator, seven security requirements, fourteen conformance tests, and the resolved design questions and parameters
- [Accumulator plan (org)](accumulator-plan.org) -- plan of record for the certified-attribute trees: salted leaves where membership is private and public leaves where it is advertised, the epoch reinterpretation that keeps every identity scalar stable without regenerating a vector, the insurance regulator as a periodic attestor gating BuckCredit issuance, and the seven-interface blast radius; branch feature/identity-accumulator
- [A' plan (org)](a-prime-plan.org) -- refined hiding-presentation design, per-document revision catalogues (paper, identity, identity-example, proofs), phased implementation plan with acceptance tests; branch feature/a-prime: phases 1-4 (Python, Solidity, Rust/wasm/JS, simulation) implemented and green as of 2026-09-20, phase 5 (documents) done, phase 6's argument written into the proofs paper with only the independent review outstanding; closes with the next program, the certified-attribute accumulator: salted per-tree leaves, authority-side admission off the registration path, holder-produced attribute proofs in receipts, a lifetime-stable identity scalar with mutable facts outsourced to tree membership, and insurer authorization for BuckCredit, with R5 deferred behind it
- [Executed reproductions](../../scripts/review/) -- EVM approval forgery, uncertified bind and uncontrolled registration (ported to the A' registry, still inverting their findings), real-Groth16 membership mismatch, SILMARILS model, issuer-linking demo (Part B refuted by R4) and hiding-presentation probe (R2 on production; Pathway A' battery), both pinned to 75104a8
  uncertified bind, uncontrolled registration, A1-via-B1 spend (flavor +
  fail-open), real-Groth16 membership mismatch and limb-carry alias,
  SILMARILS model

The immediate priority is to make the identity claims accurate. Wallet
counterexamples and EVM/Groth16 reproductions now cover findings 1--5 and
7--9. Findings 7 and 8 together moved BUCK: an A1 opening redeemed through
`spendCoupledB1` by a non-addressee with an empty membership proof.
Successful example transcripts and cross-language parity establish
agreement on those examples, not security against all malicious witnesses.

The findings now expand each weakness with repair options, privacy and gas
tradeoffs, encrypted-wallet storage implications and required validation.
They also cover the fail-open empty-proof skip, registration without
key-ownership, G1-tie limb underconstraint, and distinguish PQ receipt
protection from a complete PQ protocol migration. Production protocol code
has not been changed. Wallet counterexamples and separate mitigation
prototypes accompany the findings. See the findings' **Work completed and
remaining tasks** section for execution status and next steps.

This review does not change the project's ownership, license, or publication
status. It does not establish or dismiss the novelty of a repaired construction.
