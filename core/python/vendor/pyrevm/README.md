# Our pyrevm build: 0.3.7 with a bounded journal

`pyrevm` (Python bindings to revm, PyPI `pyrevm`, by Georgios Konstantopoulos and others) runs the
Python simulations' EVM in process (`alberta_buck/sim/pyrevm_backend.py`). Version 0.3.7 keeps every
call's journal open: its executor skips revm's finalization so that state persists between calls,
and so the journal of state accesses and the list of logs grow with every transaction for the life
of the EVM. A five-year economy world mines about 220,000 transactions of about 86 kB of journal
each: about 20 GB, which killed three of the seven five-year worlds of 2026-11-16 (SIGKILL, out of
memory; doc/review/bucknet-economy-implementation.org, "The five-year net suite, and why three of
its worlds died"). Its API has no call that clears the journal (`commit()` decrements the journal's
depth, and underflows it at depth 0).

`pyrevm-0.3.7-journal.patch` adds `release_journal()`, run after every call: with no checkpoint
outstanding, nothing can revert into the journal, and the call's logs are already in its result, so
both return to their initial state. The state itself (accounts, storage, their warm status) is
untouched, so execution and gas are unchanged: the same 30-day lifecycle world writes byte-identical
records and vectors on 0.3.7 and on this build. The version becomes `0.3.7+alberta.1`.

## Build and install it (into the venv)

    make nix-venv-pyrevm-patched

downloads the 0.3.7 source distribution from PyPI (hash-pinned), applies the patch, builds the wheel
with maturin (its Rust crates from crates.io, pinned by its Cargo.lock) and installs it over the
venv's pyrevm. `make nix-venv-test-worlds` (scripts/world-suite.sh) refuses to start on an unpatched
pyrevm. Other runs only print a warning once their journal grows.

## Publishing it

Not published. Under our own name (as `alberta-buck-webevm` for webevm) it needs pyrevm's licence
confirmed first: the 0.3.7 source distribution carries no licence file. That, and the publication,
are the owner's steps. When upstream releases a pyrevm that finalizes or clears its journal, depend
on it and delete this directory and its make target.
