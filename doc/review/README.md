# Alberta Buck paper review

The review covers all 19 root `alberta-buck-*.org` papers, read in full.
It is ongoing; the papers themselves have not yet been revised.

- [Reading and working record](2026-09-12-editorial-audit.md)
- [Identity findings, evidence and mitigation options](identity-findings.md)
- [Audience map and editing decisions](editorial-map.md)
- [Mitigation implementation and testing guide](mitigation-implementation-guide.md) -- costed, prioritized handoff for landing the repairs
- [Executed reproductions](../../scripts/review/) -- EVM approval forgery, real-Groth16 membership mismatch, SILMARILS model

The immediate priority is to make the identity claims accurate. Three
counterexamples have been reproduced with the wallet code. Further findings
come from source inspection and must not be reported as completed end-to-end
exploits. Successful example transcripts and cross-language parity establish
agreement on those examples, not security against all malicious witnesses.

The findings now expand each weakness with repair options, privacy and gas
tradeoffs, encrypted-wallet storage implications and required validation.
They also cover a source-established cross-flavor/issuer-binding spend gap
and distinguish PQ receipt protection from a complete PQ protocol migration.
Production protocol code has not been changed. Wallet counterexamples and
separate mitigation prototypes now accompany the findings; the last
kernel/receipt selection passed 11 tests. Integration helpers and a review
circuit are preserved as unfinished scaffolding. See the findings' **Work
completed and remaining tasks** section for execution status and next steps.

This review does not change the project's ownership, license, or publication
status. It does not establish or dismiss the novelty of a repaired construction.
