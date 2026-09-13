# Proposed audience map

All root papers have been read. This map is a working editorial decision,
not a completed reorganization. Preserve original copyright headers and
archive superseded text before replacement. Update local references and
document build targets when retiring a paper.

## Living papers

| Paper | Audience and job | Revision |
|---|---|---|
| `alberta-buck-paper.org` | Researchers and technically curious readers; the principal paper | Complete a focused account of the intended identity/payment contribution, explicit threat model, actual construction, findings, conditional results, evaluation and open requirements. Remove unsupported novelty and deployment-readiness claims. |
| `alberta-buck-identity.org` | General readers, prospective users and partners | Explain why identity, account keys and payment records are different; what participant-held evidence could offer; what the current prototype fails to guarantee. Move algebra to Proofs and code to Identity Example. |
| `alberta-buck-proofs.org` | Cryptographers and auditors | State exact relations, assumptions and security games. Keep valid algebra, narrow conditional rerandomization result, present counterexamples and withdraw invalid composition claims. Distinguish a proposed repaired protocol from shipped code. |
| `alberta-buck-identity-example.org` | Implementers learning the identity API | One compact, reproducible ceremony plus adversarial examples. Remove obsolete ABI examples, duplicate proofs, and claims that distinct coordinates demonstrate unlinkability. |
| `alberta-buck-notes-flow.org` | Wallet and contract implementers | One accurate mint/delivery/spend walkthrough with current units and APIs, a disclosure table, precise verifier relations and executable examples. Remove repeated architecture and security essays. |
| `alberta-buck-receipt.org` | Wallet integrators and auditors | Specify the actual JSON envelope, data retention, verification tiers and disclosure consequences. One useful worked receipt; no claim that a passing offline check proves a completed payment or personal intent. |
| `alberta-buck-ethereum.org` | Contract integrators | Concise current contract map, signed balances, credit activation/release, demurrage, current Notes interfaces, trust and governance boundaries. Remove obsolete variants and duplicated cryptographic derivations. |
| `alberta-buck-ethereum-basket.org` | General readers interested in the economic proposal | Short explanation of basket ownership, market exposure, fees and conditional rebalancing rationale. Remove physics-based return guarantees and unconditional liquidity/solvency claims. |
| `alberta-buck-ethereum-routing.org` | Simulation users and integration developers | Consolidate reproducible routing and economic scenario entry points. Distinguish the routing-only experiment from closed-loop equilibrium worlds. |
| `alberta-buck-demo.org` | A visitor trying the demonstration | Keep the farmer story and controls; explain simulated headroom versus money received, ongoing obligations, and the counterfactual. Avoid universal ROI claims. |
| `alberta-buck-rebalance.org` | Quantitative researchers and keeper implementers | Keep policy definitions, measured comparisons, sample-and-hold catch-up and implementation map. Separate synthetic reversion assumptions from historical evidence. Move speculative analogies out of the main argument. |
| `alberta-buck-operations.org` | Researchers studying monetary interventions | Lead with flow, inventory and cumulative-budget bounds. Separate toy model, external agent and contract results. Correct the geometric-versus-arithmetic basket distinction and avoid guaranteed stabilization/profit. |

## Candidates for retirement or relocation

| Paper | Destination of useful content | Reason |
|---|---|---|
| `alberta-buck-notes.org` | Main paper for architecture; Notes Flow for implementation | Extensive overlap with both, including repeated invariants and obsolete status claims. Preserve the design history in `doc/historical/`. |
| `alberta-buck-ethereum-direct.org` | Ethereum reference and Basket introduction | Older implementation plan largely superseded by the contract reference; distinguish original redemption policy from later pro-rata variants. |
| `alberta-buck-ethereum-example.org` | Routing/simulation guide | Unfinished opening, obsolete lifecycle and inconsistent numerical arbitrage example. Keep useful scenario commands after checking them. |
| `alberta-buck-demurrage.org` | Ethereum reference | Describes old `_demurrage`/OZ state and supply treatment, inconsistent with later signed-balance/buck-seconds implementation. Preserve as an old design rather than a competing living specification. |
| `alberta-buck-platform.org` | Concise developer architecture reference under `doc/` | Useful architecture buried in a chronological phase log; package and port status superseded by later work. Archive the log. |
| `alberta-buck-deployment.org` | Concise development/release reference under `doc/` | A release diary, not a monetary-system paper. Preserve build lessons; treat published versions as dated records unless independently checked. Archive the diary. |
| `alberta-buck-notes-serialized.org` | Historical research proposal with explicit unresolved issues | Unimplemented, nonessential to the main paper, and contains errors requiring redesign; not a validated optimization. |

## Corrections beyond identity

- **Serialized notes:** the proposed unchanged mint commits value `v`, while
  the diagram funds `N*v` and permits `N` redemptions. Conservation and
  exclusion of ordinary parent redemption need an explicit relation. Raw
  serials as Merkle leaves disclose the sibling leaf through the path;
  claims that a holder knows no sibling serial need correction. The document
  both hides and publicly emits the serial index. Family linkage is a real
  privacy cost for bearer holders. Per-spend gas is not proportional to
  private Merkle depth in a fixed-public-input Groth16 verifier.
- **Notes Flow:** current BUCK has six decimals, not eighteen. Mint public
  input cost grows with batch size. Zero padding is not redeemable through
  an entry point requiring positive face. Reverted transactions still expose
  calldata and consume fees. Different verifier addresses alone do not
  prevent proof replay; domains must actually be bound. Receipt reconstruction
  needs the opening and authenticated chain data, not a transaction ID alone.
- **Rebalancing:** correlation-lag searches do not establish causality or
  forecastable reversion. Normalized value shares already cancel a common
  numeraire multiplier. Catch-up is exact for an assumed held input and
  costs logarithmic work in the gap, not an unconditional constant. Simulation
  performance and mathematical finance results require their assumptions.
- **Operations:** mean log price is a geometric index, not the arithmetic
  fixed-quantity `basketValueInBuck`. Common commodity shocks can move it
  without being monetary mispricing. A range order can remain converted;
  its unwind is price-dependent, unlike a contractually maturing repo.
- **Ethereum:** resolve mutually inconsistent supply identities and stale
  mint/burn examples against code. Insurance yield of 10% is an assumption,
  not a guaranteed perpetual policy. K limits credit; it does not itself
  compel all debtors to buy back money or prove convergence.
- **Receipts:** base64 is disclosure, not encryption. Full identity data
  makes `m` available to every receipt reader. Printed status, event existence,
  matching embedded keys, issuer certification, and authentic payment binding
  are separate checks.

## Editing discipline

Lead with one concrete question per paper. Define the few necessary terms
once. Prefer a short example to repeated reassurance. Keep implementation
details in the implementer papers and security assumptions beside the claims
they qualify. Replace grand conclusions with precise, falsifiable statements.
Keep the motivation and the author's contribution visible without presenting
an unproved property as a breakthrough.
