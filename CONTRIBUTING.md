# Contributing

Thanks for your interest. This file covers the legal side of contributing; for
build and test instructions see `README.org`.

## Licensing of contributions

This project is published under two licences by layer — see `LICENSING.md`.
Contributions are accepted under a **Contributor Licence Agreement (CLA)**
rather than a Developer Certificate of Origin (DCO), and it is worth being
explicit about why, because the choice has consequences for you.

A DCO is an attestation that you wrote the code and have the right to submit
it. It conveys **no relicensing right**. Under a DCO, once contributions from
several people are merged, nobody — including the original author — can change
the project's licence without tracking down every contributor and obtaining
individual consent. In practice that makes relicensing impossible.

That matters here for two specific reasons:

1. **There is a known licence-compatibility issue to fix.** `alberta_buck/`
   (GPL-3.0-or-later) depends on `core/` (CAL-1.0). The intended remedy is a
   narrowly scoped CAL §4.5 Combined Work Exception at the API boundary. That
   remedy requires the ability to adjust licence terms on the affected files.
2. **Dual-licensing may be needed later.** Keeping that option open requires
   unified rights.

So the CLA exists to preserve the ability to *fix* licensing, not to take
anything from you. Under it:

- **You keep your copyright.** You are not assigning it.
- You grant a licence broad enough to relicense your contribution as part of
  this project.
- You confirm you have the right to make the contribution (that it is your own
  work, or that you have permission).
- Your contribution is published under the project's licences, so you and
  everyone else get it back on those terms.

If you are contributing on behalf of an employer, please make sure whoever owns
your work product has agreed.

## How to sign

Add a `Signed-off-by:` line to your commits:

    git commit -s

and state in your first pull request that you agree to the CLA terms above. For
substantial contributions a signed CLA document may be requested.

## Trademarks

The CLA covers copyright only. It grants no rights in the project's name — see
`TRADEMARKS.md`.

## Third-party code

Do not paste code from other projects unless you are certain of its licence and
its compatibility with the layer you are adding it to. If you vendor anything,
add it to `NOTICE` in the same commit. If you add or upgrade a dependency that a
published package or the sandbox builds in, regenerate the third-party notices
(`make nix-third-party-notices`; `make nix-third-party-notices-check` fails while
they are stale). Note that `core/` is CAL-1.0 and cannot
absorb GPL-licensed code; there is already precedent for this in the repo —
the Poseidon constants were regenerated specifically to avoid shipping GPL
circomlib data inside a CAL crate.

## Source headers

New source files must carry an SPDX identifier matching the layer they are in,
per the table in `LICENSING.md`:

<!-- REUSE-IgnoreStart -->

    // SPDX-License-Identifier: CAL-1.0          (core/)
    // SPDX-License-Identifier: GPL-3.0-or-later (src/, alberta_buck/)

<!-- REUSE-IgnoreEnd -->

---

Nothing in this file is legal advice.
