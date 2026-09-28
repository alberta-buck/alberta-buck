# Security policy

## First: what this is

Alberta Buck is a prototype.  It is unaudited, it is at version 0.2, and it is not for anything of
real value.  Nothing is deployed at a fixed address on any chain: every world -- the tests, the
simulations, the browser sandbox, each visitor's world in the savings sandbox -- deploys its own
contracts into its own EVM.  The Groth16
verifiers behind Notes come from a *development* trusted setup whose secret is public, on purpose,
until v1.0.0.

So a vulnerability here is a flaw that *would* matter once it launched, and this is the time to find
it.  Design flaws are as welcome as code bugs.

## Reporting a vulnerability

Report privately, not in a public issue, pull request or discussion:

- **On GitHub**: the repository's **Security** tab, *Report a vulnerability* (private
  vulnerability reporting).
- **By email**: [security@albertabuck.ca](mailto:security@albertabuck.ca).  Ask for an encrypted
  channel if you need one.

Before you write, check [doc/KNOWN-ISSUES.org](doc/KNOWN-ISSUES.org): it lists what is already
known, and where each stands.  A new angle on a known issue is still welcome.

A useful report says:

- **What**: the component, and the release or commit it applies to.
- **Impact**: who can do what to whom -- mint, steal, freeze, deanonymize, evade -- and under what
  assumptions.
- **How**: a reproduction.  Every world here deploys fresh, so a failing Foundry test, a simulation
  script or a sandbox transcript is usually cheap to write, and the quickest thing to act on.
- **Anything else**: a suggested fix, whether you have told anyone else, and how you would like to
  be credited.

## What happens next

- An acknowledgement within 7 days, and an assessment -- confirmed, not reproducible, or out of
  scope, with the reasons -- within 30.  This is a small project; if it will take longer, you will
  hear why.
- A fix is prepared privately and released; the advisory is then published (as a GitHub security
  advisory), crediting you unless you prefer otherwise.  We aim to disclose within 90 days of the
  report, sooner once a fix is out, and will agree any change to that with you.
- A confirmed issue that cannot be fixed at once is added to
  [doc/KNOWN-ISSUES.org](doc/KNOWN-ISSUES.org) when it is disclosed.
- There is no bug bounty at present.

## Scope

In scope:

- **The contracts** (`src/`) and their published bundle, `alberta-buck-contracts` (npm, PyPI).
- **The circuits** (`circuits/`) and their verifiers.  The current setup is a development one, but a
  soundness or zero-knowledge flaw that a proper ceremony would not cure matters now.
- **The kernels and libraries**: the `alberta-buck-math`, `-identity`, `-registry` and `-wallet`
  crates; `alberta-buck-kernel` and `alberta-buck-core` (npm, PyPI); `alberta-buck` (PyPI).
- **The protocol**, as the papers (`alberta-buck-*.org`) specify it.
- **The hosted sandbox**, <https://sandbox.albertabuck.ca/>: anything that makes a visitor's tab do
  what they did not ask, or leak what they did not share.
- **The hosted savings sandbox**, <https://savings-sandbox.albertabuck.ca/>, and the sim server
  behind it (`alberta_buck/sim/server.py`).  Each visitor gets a world of their own, and the server
  executes only transactions its visitors sign.  Inside your own world anyone's key may be yours
  to use -- its governance and agents run on the well-known development accounts, by design (see
  [KI-7](doc/KNOWN-ISSUES.org)) -- so acting as them there is not a finding.  Reaching another
  visitor's world, reading or steering their session without its id, making the server execute a
  transaction nobody signed, or running code on the server is.
- **The release pipeline** (`.github/workflows/`): anything that would let someone publish in our
  name.

The findings we most want:

- **Money**: minting without insured value or funding; escaping demurrage; breaking the
  depreciation, jubilee or credit-limit accounting; taking or freezing someone else's BUCKs or
  Notes; spending a Note twice.
- **Identity and accountability**: registering without a valid credential; moving BUCKs past the
  identity gate; forging or suppressing transfer receipts.  "Private by default, accountable by
  law" has two halves, and evading either is a finding.
- **Privacy**: learning more than the papers say a party learns -- linking an account to a person, a
  payer to a payee, a Note's mint to its spend.
- **Cryptography**: soundness or zero-knowledge breaks in the circuits; misuse of the
  Pointcheval-Sanders, ElGamal or Chaum-Pedersen constructions; Poseidon parameters; Fiat-Shamir
  transcripts; randomness.

Out of scope:

- What [doc/KNOWN-ISSUES.org](doc/KNOWN-ISSUES.org) already lists, restated.
- Flaws in third-party code -- OpenZeppelin, Uniswap, arkworks, tevm, viem and the rest.  Report
  those upstream; do tell us if the way we use them makes things worse.
- Denial of service against a local test chain, or volumetric attacks on the hosted sandboxes --
  exhausting the savings sandbox's cap on concurrent worlds included.
- Missing best practices with no concrete impact, and social engineering.

## Supported versions

| Version | Supported                                |
|---------|------------------------------------------|
| 0.2.x   | Yes: fixes go into the next 0.2 release. |
| 0.1.x   | No.                                      |

Until 1.0.0 nothing here is production software, and fixes land in the next release rather than
as backports.

## Testing

Test on your own machine: `make nix-test`, the simulations and the sandbox all deploy fresh worlds
you control.  There is no Alberta Buck deployment on a public chain to test against -- if you come
across one claiming to be ours, please say so; it is not.  The hosted sandbox is a static page that
runs in your own tab; nothing there is shared with other visitors.  The hosted savings sandbox runs
your world on a server: test only in a world of your own there, or run the same server locally
(`make nix-venv-sim-savings`).

Research done in good faith under this policy -- on your own copies, without harming other people
or their data, and reported privately -- will not be pursued by us, legally or otherwise.
