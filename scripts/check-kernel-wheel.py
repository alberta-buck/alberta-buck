#!/usr/bin/env python3
"""Release gates for alberta-buck-kernel.

Two modes, both used by release-pypi.yml:

  --wheel PATH   structural checks on a built wheel, before it is tested
  --installed    behavioural check on the environment it was installed into

The second exists because the vector suite uses pytest.importorskip, so a
kernel that fails to load makes the conformance tests SKIP rather than
fail: `pytest core/python/tests` would report success on a wheel that
imports nothing.  This asserts the modules really came from the installed
alberta-buck-kernel and really are compiled objects, and must run before
the suite for the suite's result to mean anything.
"""

from __future__ import annotations

import argparse
import sys
import sysconfig
import zipfile
from pathlib import Path

MODULES = ("buck_math", "buck_identity", "buck_wallet", "buck_registry")

# Two compiled objects, never four: buck-identity, buck-wallet and
# buck-registry are one cdylib with three #[pymodule] entry points.
OBJECTS = ("_math", "_kernel")

PY_MEMBERS = ("__init__.py", "_loader.py") + tuple(f"{m}.py" for m in MODULES)


def fail(msg: str) -> None:
    print(f"::error::{msg}")
    sys.exit(1)


def check_wheel(path: Path, expect_platform: str | None = None) -> None:
    name = path.name
    print(f"checking {name}")

    if not name.endswith(".whl"):
        fail(f"{name} is not a wheel")

    # abi3-py311: ONE wheel per platform serves every CPython from 3.11 on.
    # A cp313-cp313 tag means py_limited_api was lost from setup.cfg and we
    # would silently owe a wheel per Python version forever after.
    if "-cp311-abi3-" not in name:
        fail(f"{name} is not tagged cp311-abi3; "
             f"py_limited_api is missing from setup.cfg")

    plat = name.rsplit("-", 1)[-1][: -len(".whl")]

    # A bare linux_* tag is what setuptools produces inside the manylinux
    # container; PyPI rejects it on upload.  Catching it here names the
    # cause (auditwheel repair did not run) instead of failing at the very
    # last step of a five-platform matrix.
    if plat.startswith("linux_"):
        fail(f"{name} carries the unrepaired platform tag {plat}; "
             f"auditwheel repair did not run")
    if plat == "any":
        fail(f"{name} is tagged 'any'; the compiled objects are missing "
             f"and setup.py's BinaryDistribution did not take effect")

    # The macOS tag is the trap worth an explicit assertion.  It is NOT
    # chosen by the packaging step: `wheel` takes the greater of the
    # building interpreter's sysconfig platform and the highest Mach-O
    # LC_BUILD_VERSION minos among the objects.  Left alone on a macOS 26
    # runner that yields macosx_26_0_arm64 -- a wheel that uploads happily
    # and installs almost nowhere.  Both inputs are pinned (the link flags
    # in core/rust/.cargo/config.toml and _PYTHON_HOST_PLATFORM in the
    # workflow); this catches either one coming loose.
    if expect_platform and plat != expect_platform:
        fail(f"{name} is tagged {plat}, expected {expect_platform}.  "
             f"On macOS check both the -mmacosx-version-min link flag and "
             f"_PYTHON_HOST_PLATFORM; on Linux check that auditwheel "
             f"repair produced the manylinux tag.")

    members = zipfile.ZipFile(path).namelist()
    got = {m.split("/", 1)[1] for m in members if m.startswith("buck_kernel/")}

    binaries = sorted(m for m in got if m.endswith((".so", ".pyd", ".dylib")))
    if len(binaries) != len(OBJECTS):
        fail(f"{name} carries {len(binaries)} compiled objects "
             f"{binaries}, expected {len(OBJECTS)}: {OBJECTS}.  Shipping the "
             f"identity cdylib once per module would triple 1.5 MB.")
    for stem in OBJECTS:
        if not any(b.startswith(stem + ".") for b in binaries):
            fail(f"{name} has no {stem} object (found {binaries})")

    missing = [p for p in PY_MEMBERS if p not in got]
    if missing:
        fail(f"{name} is missing {missing}")

    size = sum(zipfile.ZipFile(path).getinfo(m).file_size
               for m in members if m.startswith("buck_kernel/"))
    print(f"  platform tag : {plat}")
    print(f"  objects      : {', '.join(binaries)}")
    print(f"  unpacked     : {size:,} bytes")


def check_no_sdist(dist: Path) -> None:
    """The kernel ships wheels only.

    Its sdist would contain whatever .so the build host happened to
    produce and none of the Rust that made it -- so pip on an unmatched
    platform would "build" it and install a foreign object.  A clean "no
    matching distribution" is a far better outcome, and that is what PyPI
    reports when only wheels exist.
    """
    sdists = sorted(dist.glob("*.tar.gz"))
    if sdists:
        fail(f"refusing to publish kernel sdists {[s.name for s in sdists]}: "
             f"they carry the build host's compiled object and no Rust "
             f"source.  Wheels only.")
    print(f"no sdist in {dist}: wheels only, as intended")


def check_installed() -> None:
    site = sysconfig.get_paths()["purelib"].replace("\\", "/")
    for name in MODULES:
        try:
            mod = __import__(f"buck_core.{name}", fromlist=[name])
        except ImportError as exc:
            fail(f"buck_core.{name} did not import: {exc}")
        f = (getattr(mod, "__file__", "") or "").replace("\\", "/")
        if not f.endswith((".so", ".pyd", ".dylib")):
            fail(f"buck_core.{name} resolved to {f or '<none>'}, "
                 f"which is not a compiled object -- the pure-Python "
                 f"fallback is in use and the vector suite would prove "
                 f"nothing about this wheel")
        if "buck_kernel" not in f:
            fail(f"buck_core.{name} loaded {f}, not the installed "
                 f"alberta-buck-kernel")
        print(f"  {name:14} -> {f.replace(site + '/', '')}")
    print("all four modules resolve to compiled objects in the installed "
          "alberta-buck-kernel")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wheel", type=Path, help="wheel to check structurally")
    ap.add_argument("--expect-platform", metavar="TAG",
                    help="require this exact wheel platform tag, e.g. "
                         "macosx_11_0_arm64 or manylinux_2_28_x86_64")
    ap.add_argument("--no-sdist", type=Path, metavar="DIR",
                    help="assert DIR contains no sdist")
    ap.add_argument("--installed", action="store_true",
                    help="check the kernel loaded into this interpreter")
    args = ap.parse_args()

    if not (args.wheel or args.installed or args.no_sdist):
        ap.error("nothing to do: pass --wheel, --no-sdist or --installed")
    if args.wheel:
        check_wheel(args.wheel, args.expect_platform)
    if args.no_sdist:
        check_no_sdist(args.no_sdist)
    if args.installed:
        check_installed()
    return 0


if __name__ == "__main__":
    sys.exit(main())
