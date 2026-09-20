# Alberta Buck paper review

The review covers all 19 root `alberta-buck-*.org` papers, read in full.
It is ongoing; the papers themselves have not yet been revised.

- [Reading and working record](2026-09-12-editorial-audit.md)
- [Identity findings, evidence and mitigation options](identity-findings.md)
- [Audience map and editing decisions](editorial-map.md)
- [Mitigation implementation and testing guide](mitigation-implementation-guide.md) -- costed, prioritized handoff for landing the repairs
- [Issuer-view follow-up (org)](identity-findings-2.org) -- R1-R6 with executable recipes, the launch-gate framing (pre-launch, nothing to migrate), and the unreviewed Pathway A' candidate
- [A' plan (org)](a-prime-plan.org) -- refined hiding-presentation design, per-document revision catalogues (paper, identity, identity-example, proofs), phased implementation plan with acceptance tests; branch feature/a-prime
- [Executed reproductions](../../scripts/review/) -- EVM approval forgery, real-Groth16 membership mismatch, SILMARILS model, issuer-linking demo (Part B refuted by R4), hiding-presentation probe (R2 on production; Pathway A' battery)
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
