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


def copy_object(src: Path, dst: Path) -> None:
    """Copy a compiled object into place and make sure it is really there.

    rm before cp: overwriting a .so in place keeps its inode, and macOS
    caches code signatures by inode -- a stale cache SIGKILLs (Killed: 9)
    the next import rather than failing it.  A fresh inode per copy
    sidesteps that.

    fsync after cp: the next thing that happens to this file is a dlopen,
    and on macOS that has been observed to fail with "slice is not valid
    mach-o file" -- the error a truncated or unreadable object gives -- on
    bytes that were correct.  Forcing the data out before anyone maps it
    costs microseconds and removes the question.
    """
    dst.unlink(missing_ok=True)
    shutil.copy2(src, dst)
    fd = os.open(dst, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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
        dst = dest / f"{stem}{suffix}"
        copy_object(src, dst)
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
        copy_object(src, dst)
        staged.append(dst)
        print(f"  {src.name} -> {dst.relative_to(REPO)} "
              f"({dst.stat().st_size:,} bytes)")
    return staged


# Loads one extension by explicit path, as buck_kernel._loader does.
_IMPORT_PROBE = """
import importlib.util, sys
from importlib.machinery import ExtensionFileLoader
name, so = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location(
    name, so, loader=ExtensionFileLoader(name, so))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
"""


def describe(so: Path) -> str:
    """Whatever the platform can tell us about an object that would not load."""
    import platform
    lines = [f"  path        : {so}",
             f"  size        : {so.stat().st_size:,} bytes",
             f"  interpreter : {sys.executable}",
             f"  running as  : {platform.machine()} "
             f"({sysconfig.get_platform()})"]
    for cmd in (["file", "-b", str(so)], ["lipo", "-archs", str(so)]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            lines.append(f"  {cmd[0]:12}: {out.stdout.strip()}")
    return "\n".join(lines)


def verify(staged: list[Path], dev: bool) -> None:
    """Import every module out of what was just staged.

    Symbol inspection would need nm/dumpbin and would still not prove the
    module initialises.  Importing does, and it is the same operation the
    wheel performs on a user's machine.

    Each import runs in a SUBPROCESS, and gets one retry.  Not fastidiousness:
    on GitHub's macos-26 arm64 image this same check failed with dyld
    reporting "slice is not valid mach-o file" on an object that was
    byte-for-byte identical (483,696 bytes) to one that had loaded cleanly
    on the previous run of the same commit, same image, same rustc.  The
    bytes were not the variable, so a fresh process and a second attempt
    are the cheap mitigations; if it fails twice, describe() prints the
    object's actual architecture so the next occurrence is diagnosed rather
    than guessed at.

    Nothing is lost if this check is wrong, either way: the real gate is
    downstream, where the built wheel is installed and the conformance
    vectors run against it.
    """
    if dev:
        plan = [(p.name.split(".")[0], p) for p in staged]
    else:
        by_stem = {p.name.split(".")[0]: p for p in staged}
        plan = [("buck_math", by_stem["_math"])]
        plan += [(m, by_stem["_kernel"]) for m in KERNEL_MODULES]

    for modname, so in plan:
        last = ""
        for attempt in (1, 2):
            out = subprocess.run(
                [sys.executable, "-c", _IMPORT_PROBE, modname, str(so)],
                capture_output=True, text=True)
            if out.returncode == 0:
                note = "" if attempt == 1 else f" (on attempt {attempt})"
                print(f"  import {modname} from {so.name}: ok{note}")
                break
            last = (out.stderr or out.stdout).strip().splitlines()[-1:]
            last = last[0] if last else f"exit {out.returncode}"
            if attempt == 1:
                print(f"  import {modname} from {so.name}: failed, retrying "
                      f"-- {last}")
        else:
            print(f"::error::{so.name} would not load as {modname}: {last}")
            print(describe(so))
            sys.exit(1)


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
