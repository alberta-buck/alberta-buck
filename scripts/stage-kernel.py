#!/usr/bin/env python3
"""Build the Rust kernels and stage the compiled objects into a package.

This replaces the hand-written cp lines that `make core-build-py` used to
carry.  Two reasons they could not survive the wheel matrix:

  * cargo names a cdylib differently on every platform --
    libbuck_math.dylib / libbuck_math.so / buck_math.dll -- so a rule that
    hardcodes .dylib builds only on macOS;
  * Windows runners have no make, and the wheel build must run the same
    staging step there as everywhere else.

Two destinations, because the same objects serve two layouts:

  --dev     core/python/buck_core/    four files, the DEVELOPMENT layout.
            buck_math.so plus three copies of the identity cdylib named
            buck_identity/buck_wallet/buck_registry.so, so that
            `import buck_core.buck_identity` in a checkout loads the
            freshly built extension in preference to the shim.

  default   core/python-kernel/buck_kernel/   two files, the WHEEL layout.
            _math and _kernel, loaded by name through the shims.  The
            identity cdylib is stored ONCE: it defines three #[pymodule]
            entry points and CPython derives PyInit_<name> from the module
            name it is given, not from the filename.

Target selection understands the environment cibuildwheel sets, so a
macOS x86_64 wheel cross-built on an arm64 runner stages an x86_64 object.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUST = REPO / "core" / "rust"

# (cargo [lib] name, staged stem in the wheel layout)
MATH = ("buck_math", "_math")
KERNEL = ("buck_identity", "_kernel")

# The identity cdylib carries these three #[pymodule] entry points.
KERNEL_MODULES = ("buck_identity", "buck_wallet", "buck_registry")


def rust_target() -> str | None:
    """The --target triple to build for, or None to use the host default.

    cibuildwheel builds macOS wheels for both architectures from one
    runner, communicating the target through ARCHFLAGS.  Without this the
    x86_64 wheel would carry an arm64 object and fail at import on the
    only machines that need it.
    """
    if explicit := os.environ.get("BUCK_KERNEL_TARGET"):
        return explicit

    if sys.platform == "darwin":
        archflags = os.environ.get("ARCHFLAGS", "")
        want_x86 = "-arch x86_64" in archflags
        want_arm = "-arch arm64" in archflags
        if want_x86 and want_arm:
            sys.exit("::error::universal2 is not supported; build one arch "
                     "per wheel (ARCHFLAGS requested both)")
        if want_x86:
            return "x86_64-apple-darwin"
        if want_arm:
            return "aarch64-apple-darwin"
    return None


def cdylib_filename(libname: str) -> str:
    """What cargo calls the cdylib for `[lib] name = libname`."""
    if sys.platform == "win32":
        return f"{libname}.dll"
    if sys.platform == "darwin":
        return f"lib{libname}.dylib"
    return f"lib{libname}.so"


def extension_suffix() -> str:
    """What Python will accept as an extension module here.

    Not sysconfig's EXT_SUFFIX: that is version-tagged
    (.cpython-313-darwin.so) and these objects are abi3, deliberately
    loaded by explicit path rather than found on sys.path.  Only the
    platform's dynamic-loader convention matters.
    """
    return ".abi3.pyd" if sys.platform == "win32" else ".abi3.so"


# Every spelling of the two architectures we ship, across cargo triples,
# platform.machine() and wheel tags.
_ARCH_ALIASES = (
    {"aarch64", "arm64"},
    {"x86_64", "amd64", "x64"},
)


def is_cross(target: str | None) -> bool:
    """Whether `target` produces an object this interpreter cannot import.

    Only architecture matters: the OS is always the host's, since cargo is
    not being asked to build for another operating system.
    """
    if target is None:
        return False
    import platform
    host, want = platform.machine().lower(), target.split("-")[0].lower()
    for family in _ARCH_ALIASES:
        if host in family:
            return want not in family
    return host != want


def build(target: str | None) -> Path:
    cmd = ["cargo", "build", "--release",
           "-p", "buck-math-py", "-p", "buck-identity-py"]
    if target:
        cmd += ["--target", target]
    print(f"$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=RUST, check=True)

    out = RUST / "target"
    if target:
        out = out / target
    return out / "release"


def stage_wheel(built: Path) -> list[Path]:
    dest = REPO / "core" / "python-kernel" / "buck_kernel"
    suffix = extension_suffix()
    staged = []
    for libname, stem in (MATH, KERNEL):
        src = built / cdylib_filename(libname)
        if not src.exists():
            sys.exit(f"::error::cargo produced no {src}")
        # Remove before copy: overwriting a mapped .so in place can leave a
        # process holding a half-written image, and on macOS the result is
        # a SIGKILL on next import rather than an error.
        dst = dest / f"{stem}{suffix}"
        dst.unlink(missing_ok=True)
        shutil.copy2(src, dst)
        staged.append(dst)
        print(f"  {src.name} -> {dst.relative_to(REPO)} "
              f"({dst.stat().st_size:,} bytes)")
    return staged


def stage_dev(built: Path) -> list[Path]:
    dest = REPO / "core" / "python" / "buck_core"
    staged = []
    # buck_math has its own cdylib; the other three share one.
    plan = [(MATH[0], "buck_math")] + [(KERNEL[0], m) for m in KERNEL_MODULES]
    for libname, modname in plan:
        src = built / cdylib_filename(libname)
        if not src.exists():
            sys.exit(f"::error::cargo produced no {src}")
        dst = dest / (modname + (".pyd" if sys.platform == "win32" else ".so"))
        dst.unlink(missing_ok=True)
        shutil.copy2(src, dst)
        staged.append(dst)
        print(f"  {src.name} -> {dst.relative_to(REPO)} "
              f"({dst.stat().st_size:,} bytes)")
    return staged


def verify(staged: list[Path], dev: bool) -> None:
    """Import every module out of what was just staged.

    Symbol inspection would need nm/dumpbin and would still not prove the
    module initialises.  Importing does, and it is the same operation the
    wheel performs on a user's machine.
    """
    import importlib.util
    from importlib.machinery import ExtensionFileLoader

    if dev:
        plan = [(p.name.split(".")[0], p) for p in staged]
    else:
        by_stem = {p.name.split(".")[0]: p for p in staged}
        plan = [("buck_math", by_stem["_math"])]
        plan += [(m, by_stem["_kernel"]) for m in KERNEL_MODULES]

    for modname, so in plan:
        spec = importlib.util.spec_from_file_location(
            modname, str(so), loader=ExtensionFileLoader(modname, str(so)))
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except ImportError as exc:
            sys.exit(f"::error::{so.name} has no PyInit_{modname}: {exc}")
        print(f"  import {modname} from {so.name}: ok")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dev", action="store_true",
                    help="stage the development layout into "
                         "core/python/buck_core instead of the wheel layout")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the post-stage import check (cross-builds "
                         "cannot run their own output)")
    args = ap.parse_args()

    target = rust_target()
    print(f"host {sysconfig.get_platform()}, "
          f"target {target or 'default'}", flush=True)

    built = build(target)
    staged = stage_dev(built) if args.dev else stage_wheel(built)

    if args.no_verify or is_cross(target):
        print("  (import check skipped: cross-built object)")
    else:
        verify(staged, args.dev)
    return 0


if __name__ == "__main__":
    sys.exit(main())
