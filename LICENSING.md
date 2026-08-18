# Licensing

Copyright (c) 2026 Perry Kundert.

Two licences, applied by layer.  The reasoning is the decision of record in
`alberta-buck-deployment.org`, section D2; this file is the map.

| path                     | licence            | published as                                     |
|--------------------------|--------------------|--------------------------------------------------|
| `core/rust/buck-*`       | CAL-1.0            | crates.io `alberta-buck-{math,identity,registry,wallet}` |
| `core/rust/bindings/*`   | CAL-1.0            | (build artifacts; not published as crates)       |
| `core/python/`           | CAL-1.0            | PyPI `alberta-buck-core`, `alberta-buck-kernel`  |
| `core/js/`               | CAL-1.0            | npm `alberta-buck-core`, `alberta-buck-kernel`   |
| `src/*.sol`              | GPL-3.0-or-later   | `alberta-buck-contracts` (npm, PyPI)             |
| `alberta_buck/`          | GPL-3.0-or-later   | PyPI `alberta-buck`                              |

Full texts: `core/LICENSE` (CAL-1.0), `LICENSE` (GPL-3.0).  Third-party
components and their licences: `NOTICE`.  The name is **not** covered by
either licence: `TRADEMARKS.md`.  Contributing: `CONTRIBUTING.md`.

## How licensing is declared

Machine-readably in `REUSE.toml`, per the REUSE Specification, which is the
supported alternative to stamping every file with an SPDX header.  That file
and this table are the same map and must be kept in step.

Source files are **not** uniformly headered — as of this writing 657 of 797
tracked files carry no SPDX identifier, which is why the declaration is
central.  New files should carry one anyway (`CONTRIBUTING.md`), because a
header is the only thing that travels with a file copied *out* of the repo.

## Design documents

The `*.org` design writing and `doc/` are **not** under the licences above.
All rights reserved, declared explicitly rather than left as an undeclared
default.  This is the reversible direction: relaxing to an open licence later
is one line; an open grant cannot be withdrawn.

## Generated verifiers

The Groth16 verifiers under `src/` are emitted by snarkjs and carry the
header its template writes.  The SPDX tag on its line 1, `GPL-3.0`, is the
deprecated, ambiguous SPDX identifier, but the licence notice
in the same header -- written by 0KIMS association, the copyright holder --
grants "either version 3 of the License, or (at your option) any later
version": the standard GPL-3.0-**or-later** grant.  The notice is the
operative grant; the tag is metadata.  So the verifiers do not narrow the
contract set: everything under `src/` is GPL-3.0-or-later.

The files are left byte-for-byte as generated (the headers are snarkjs's to
write, and byte-stock output is what the regeneration runbook verifies);
`REUSE.toml` records the or-later reading with the rationale.

## Why CAL for the kernels

The Cryptographic Autonomy License restricts *architecture*, not purpose.
No OSI-approved licence may discriminate against a field of endeavour, so
"do not use this to build a system that takes away people's autonomy" is not
something a licence can say.  CAL says something enforceable instead: if you
provide this software's functionality to a third party, you must give that
party their own data (4.2.1), you may not use cryptographic or technological
measures to limit their access to functionality or control of their data
(4.2.2), and you may not contractually restrict them from exercising the
same permissions independently (4.2.3).

A system that holds users' keys, can freeze their balances, or withholds
their records cannot satisfy 4.2 while using this code.  It is out of
licence the day it ships, with no need to prove anyone's intent.

The kernels are where this matters: anyone building such a system needs the
identity, wallet and Notes mathematics.  CAL applied anywhere else would be
decorative.

## Combined Work Exception

CAL 4.5 lets individual files be marked with the SPDX identifier
`CAL-1.0-Combined-Work-Exception`, permitting
combination into a larger work under other terms -- provided recipients
still receive the notices, the source access, and full control of their own
data.

**No file in this repository currently carries that exception.**  Granting it
is one-way for any version already published, so it will be applied narrowly
and late -- at the binding and API surface, if and when a specific
integrator needs it -- never across the kernels wholesale.

## File headers

Most source files do **not** carry an SPDX header -- 657 of 797 tracked files
have none.  Licensing is therefore declared centrally in `REUSE.toml` (see
"How licensing is declared" above), which is the REUSE Specification's
supported alternative and is what compliance scanners read.

New files should carry a header anyway, matching the table above, because it
is the only thing that travels with a file copied *out* of this repository:

<!-- REUSE-IgnoreStart -->

    // SPDX-License-Identifier: CAL-1.0          (core/)
    // SPDX-License-Identifier: GPL-3.0-or-later (src/*.sol, alberta_buck/)

<!-- REUSE-IgnoreEnd -->

## Contributions

Dual licensing -- granting a separate licence to a party who cannot comply
with CAL -- works only while the copyright is held in one place.  Before this
repository accepts outside contributions it needs a contributor licence
agreement; a Developer Certificate of Origin is not sufficient, because it
conveys no relicensing right.
