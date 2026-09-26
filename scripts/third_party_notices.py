#!/usr/bin/env python3
"""Third-party notices for what Alberta Buck redistributes.

Every published package that builds third-party code into itself, and the
hosted sandbox, ships a THIRD-PARTY-NOTICES.txt: each component, the licence
it is used under, and that licence's text, taken from the component's own
licence files (never typed, never assumed).

    third_party_notices.py kernel-wasm      the WebAssembly kernel (npm alberta-buck-kernel):
                                            the crates Cargo.lock resolves for the wasm bindings
    third_party_notices.py kernel-native    the native kernel (PyPI alberta-buck-kernel), on
                                            every platform its wheels are built for
    third_party_notices.py contracts        the contracts bundle (npm and PyPI
                                            alberta-buck-contracts): what solc compiles into it
    third_party_notices.py sandbox META     the hosted sandbox: the packages esbuild bundled
                                            (META is its metafile), the contracts it deploys,
                                            and the Rust crates in its WebAssembly kernel

The notice goes to stdout.  `make third-party-notices` rewrites the committed
ones; `make third-party-notices-check` fails when one is stale; the sandbox's
is written into its dist/ at every build.

A crate or package offered under a choice of licences ("MIT OR Apache-2.0") is
used under MIT when it ships an MIT text; otherwise every licence file it ships
is reproduced.  One that ships no licence file at all gets the standard text of
its declared licence, from LICENSES/ or from another component in the same
notice that carries that text verbatim; failing both, generation stops.
"""

import json
import os
import re
import subprocess
import sys
import textwrap

ROOT                            = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUST                            = os.path.join(ROOT, "core", "rust")
REPO_URL                        = "https://github.com/alberta-buck/alberta-buck"
LICENCE_FILE                    = re.compile(r"^(licen[cs]e|copying|copyright|notice|unlicense)([-._].*)?$", re.I)
MIT_TEXT                        = "Permission is hereby granted, free of charge"
RULE                            = "-" * 79


def wrap(text, width=100, **kw):
    """Wrap prose, never breaking a word or a URL."""
    return textwrap.wrap(text, width, break_long_words=False, break_on_hyphens=False, **kw)


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def normalize(text):
    lines = [l.rstrip() for l in text.replace("\r\n", "\n").replace("﻿", "").split("\n")]
    return "\n".join(lines).strip("\n")


def licence_files(directory):
    return sorted(f for f in os.listdir(directory)
                  if LICENCE_FILE.match(f) and os.path.isfile(os.path.join(directory, f)))


def only_alternatives(expr):
    """True for a pure choice: "MIT OR Apache-2.0", "MIT/Apache-2.0" -- no AND, no WITH."""
    return bool(expr) and not re.search(r"\b(AND|WITH)\b", expr) and bool(re.search(r"\bOR\b|/", expr))


def elect(directory, expr):
    """The licence files to reproduce, as [(file name, text)]."""
    files = [(f, read(os.path.join(directory, f))) for f in licence_files(directory)]
    if only_alternatives(expr) and re.search(r"\bMIT\b", expr):
        mit = [(f, t) for f, t in files if MIT_TEXT in t or "mit" in f.lower()]
        if mit:
            return mit
    return files


def component(name, version, licence, source, texts, note=""):
    return {"name": name, "version": version, "licence": licence, "source": source,
            "texts": [normalize(t) for _, t in texts], "note": note}


def repo_url(repository):
    url = repository.get("url", "") if isinstance(repository, dict) else (repository or "")
    url = re.sub(r"^git\+", "", url)
    url = re.sub(r"^git://", "https://", url)
    url = re.sub(r"^(github|gitlab):", r"https://\1.com/", url)
    url = re.sub(r"\.git$", "", url)
    return url


# ---- Rust: the crates Cargo.lock resolves ------------------------------------------------------

def crates(roots, platform=None):
    cmd = ["cargo", "metadata", "--format-version", "1", "--locked"]
    if platform:
        cmd += ["--filter-platform", platform]
    meta = json.loads(subprocess.run(cmd, cwd=RUST, capture_output=True, text=True, check=True).stdout)
    packages = {p["id"]: p for p in meta["packages"]}
    nodes = {n["id"]: n for n in meta["resolve"]["nodes"]}
    todo = [i for i, p in packages.items() if p["name"] in roots]
    seen = set()
    while todo:
        i = todo.pop()
        if i in seen:
            continue
        seen.add(i)
        for dep in nodes[i]["deps"]:
            if any(k["kind"] is None for k in dep["dep_kinds"]):   # linked, not build- or dev-only
                todo.append(dep["pkg"])
    out = []
    for i in seen:
        p = packages[i]
        if not p["source"]:                                           # our own workspace crates
            continue
        texts = elect(os.path.dirname(p["manifest_path"]), p.get("license") or "")
        out.append(component(p["name"], p["version"], p.get("license") or "(see its files)",
                             p.get("repository") or f"https://crates.io/crates/{p['name']}", texts))
    return out


def kernel_wasm():
    return crates({"buck-math-wasm", "buck-identity-wasm"}, "wasm32-unknown-unknown")


def kernel_native():
    return crates({"buck-math-py", "buck-identity-py"})              # every platform: a superset


# ---- Solidity: what the compiler builds into the bundle ----------------------------------------

def gpl_note(where):
    return f"The GNU General Public License, version 3, is {where}."


def openzeppelin():
    """OpenZeppelin's version and licence files: from lib/, or -- in a checkout without lib/, like
    the sandbox's CI build -- from the committed contracts notice, which recorded them."""
    oz = os.path.join(ROOT, "lib", "openzeppelin-contracts")
    if os.path.isdir(oz):
        return json.loads(read(os.path.join(oz, "package.json")))["version"], elect(oz, "MIT")
    notice = read(os.path.join(ROOT, "core", "contracts", "THIRD-PARTY-NOTICES.txt"))
    m = re.search(r"^Used by: OpenZeppelin Contracts (\S+)\n-+\n\n(.*?)(?=\n\n-{79}\n|\Z)", notice, re.S | re.M)
    if not m:
        sys.exit("no lib/openzeppelin-contracts, and no OpenZeppelin text in core/contracts/THIRD-PARTY-NOTICES.txt")
    return m.group(1), [("LICENSE", m.group(2))]


def contracts(gpl_where="in this package's LICENSE"):
    oz_version, oz_texts = openzeppelin()
    cjs = os.path.join(ROOT, "node_modules", "circomlibjs")
    cjs_version = json.loads(read(os.path.join(cjs, "package.json")))["version"]
    return [
        component("OpenZeppelin Contracts", oz_version, "MIT", "https://github.com/OpenZeppelin/openzeppelin-contracts",
                  oz_texts, "Compiled into the token contracts (ERC-20, ERC-721 and utilities)."),
        component("Uniswap V3 TickMath, FullMath and OracleLibrary", "",
                  "GPL-2.0-or-later", "https://github.com/Uniswap/v3-core",
                  [], "Ported to Solidity 0.8 as src/lib/UniswapV3OracleLib.sol (Uniswap Labs); compiled into "
                      "the basket contracts; used under version 3 of the GPL. " + gpl_note(gpl_where)),
        component("circomlibjs Poseidon hashers", cjs_version, "LGPL-3.0-or-later / GPL-3.0",
                  "https://github.com/iden3/circomlibjs",
                  [], "The PoseidonT3 and PoseidonT4 bytecode, as circomlibjs's poseidon_gencontract generates it "
                      "(Copyright (c) 2018 Jordi Baylina; the generator is marked LGPL-3.0-or-later, the package "
                      "GPL-3.0). " + gpl_note(gpl_where)),
        component("snarkjs Groth16 verifier template", "", "GPL-3.0-or-later", "https://github.com/iden3/snarkjs",
                  [], "The Groth16 verifiers are generated from it (Copyright 2021 0KIMS association). "
                      + gpl_note(gpl_where)),
    ]


# ---- JavaScript: the packages esbuild bundled ---------------------------------------------------

def package_root(path):
    parts = os.path.normpath(path).split(os.sep)
    if "node_modules" not in parts:
        return None
    i = len(parts) - 1 - parts[::-1].index("node_modules")
    width = 2 if parts[i + 1].startswith("@") else 1
    return os.sep.join(parts[:i + 1 + width])


def bundled(metafile):
    base = os.path.dirname(os.path.dirname(os.path.abspath(metafile)))   # core/js: esbuild's cwd
    meta = json.loads(read(metafile))
    roots = set()
    for output in meta["outputs"].values():
        for path, used in output["inputs"].items():
            root = package_root(path) if used["bytesInOutput"] > 0 else None
            if root:
                roots.add(os.path.normpath(os.path.join(base, root)))
    out = []
    for root in roots:
        pj = json.loads(read(os.path.join(root, "package.json")))
        licence = pj.get("license") or "(see its files)"
        if isinstance(licence, dict):                                 # the old {"type": ...} form
            licence = licence.get("type", "(see its files)")
        out.append(component(pj["name"], pj.get("version", ""), licence, repo_url(pj.get("repository")),
                             elect(root, licence)))
    return out


def uniswap_deployed():
    """The Uniswap contracts the sandbox deploys, from Uniswap's packages and our vendored builds."""
    nm = os.path.join(ROOT, "node_modules", "@uniswap")
    out = []
    for pkg, what in [("v3-core", "UniswapV3Factory and UniswapV3Pool"), ("v2-periphery", "WETH9")]:
        root = os.path.join(nm, pkg)
        pj = json.loads(read(os.path.join(root, "package.json")))
        out.append(component(f"@uniswap/{pkg} ({what}, compiled)", pj["version"], pj.get("license", ""),
                             repo_url(pj.get("repository")) or f"https://github.com/Uniswap/{pkg}",
                             elect(root, pj.get("license", ""))))
    mit = read(os.path.join(ROOT, "LICENSES", "MIT.txt")).replace("<year> <copyright holders>", "2022 Uniswap Labs")
    out.append(component("Uniswap Universal Router (compiled)", "", "GPL-3.0-or-later",
                         "https://github.com/Uniswap/universal-router",
                         [("", read(os.path.join(ROOT, "LICENSES", "GPL-3.0-or-later.txt")))]))
    out.append(component("Uniswap Permit2 (compiled)", "", "MIT", "https://github.com/Uniswap/permit2", [("", mit)]))
    return out


def sandbox(metafile):
    gpl = [("", read(os.path.join(ROOT, "LICENSES", "GPL-3.0-or-later.txt")))]
    ours = contracts(gpl_where="reproduced below")
    for c in ours:
        if c["licence"] != "MIT":
            c["texts"] = [normalize(t) for _, t in gpl]
    return bundled(metafile) + uniswap_deployed() + ours + kernel_wasm()


# ---- the notice -----------------------------------------------------------------------------------

def standard_texts(components):
    """Fill in a component that ships no licence file, or stop."""
    by_licence = {}
    for c in components:
        if len(c["texts"]) == 1:
            by_licence.setdefault(c["licence"], c["texts"][0])
    for c in components:
        if c["texts"] or "GPL" in c["licence"] and c["note"]:
            continue
        spdx = os.path.join(ROOT, "LICENSES", f"{c['licence']}.txt")
        if os.path.exists(spdx):
            text = read(spdx)
            if c["licence"] == "MIT":
                text = text.replace("<year> <copyright holders>", f"the {c['name']} authors")
            c["texts"] = [normalize(text)]
        elif c["licence"] in by_licence and c["licence"] != "MIT":
            c["texts"] = [by_licence[c["licence"]]]
        else:
            sys.exit(f"{c['name']} {c['version']}: no licence file, and no standard {c['licence']} text")
        c["note"] = (c["note"] + " " if c["note"] else "") + "It ships no licence file; the standard text applies."


def render(title, intro, components):
    components = sorted(components, key=lambda c: (c["name"].lower(), c["version"]))
    standard_texts(components)
    lines = [f"Third-party software in {title}", "", *wrap(" ".join(intro)), "",
             "Generated by scripts/third_party_notices.py from the components' own licence files; do not edit.",
             "", RULE, "COMPONENTS", RULE]
    for c in components:
        lines += ["", " ".join(x for x in (c["name"], c["version"], "--", c["licence"]) if x)]
        lines.append("    " + (c["source"] or "(no source repository given)"))
        if c["note"]:
            lines += wrap(c["note"], initial_indent="    ", subsequent_indent="    ")
    groups = {}
    for c in components:
        for text in c["texts"]:
            groups.setdefault(text, []).append(f"{c['name']} {c['version']}".strip())
    lines += ["", RULE, "LICENCE TEXTS", RULE]
    for text, users in groups.items():
        lines += ["", RULE, *wrap("Used by: " + ", ".join(users), subsequent_indent="  "), RULE,
                  "", text]
    return "\n".join(lines) + "\n"


def head():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "kernel-wasm":
        text = render("alberta-buck-kernel, the WebAssembly builds (npm)",
                      ["alberta-buck-kernel is licensed CAL-1.0 (LICENSE).  The Rust crates below are compiled",
                       "into its .wasm files; each is used under the licence shown, whose text follows."],
                      kernel_wasm())
    elif mode == "kernel-native":
        text = render("alberta-buck-kernel, the native builds (PyPI)",
                      ["alberta-buck-kernel is licensed CAL-1.0 (LICENSE).  The Rust crates below are compiled",
                       "into its native extension, on every platform the wheels are built for; each is used",
                       "under the licence shown, whose text follows."],
                      kernel_native())
    elif mode == "contracts":
        text = render("alberta-buck-contracts",
                      ["alberta-buck-contracts is licensed GPL-3.0-or-later (LICENSE).  The Solidity compiler",
                       "builds the third-party code below into its bytecode; Uniswap's own contracts are not",
                       "bundled."],
                      contracts())
    elif mode == "sandbox" and len(argv) == 3:
        commit = head()
        text = render("the Alberta Buck sandbox (https://sandbox.albertabuck.ca/)",
                      [f"Built from {REPO_URL} at {commit}; that is its source:",
                       f"{REPO_URL}/tree/{commit}.  Its own code is licensed CAL-1.0 (core/js) and",
                       "GPL-3.0-or-later (the contracts).  Below: every third-party package in app.js, the",
                       "contracts it deploys, and the Rust crates in its WebAssembly kernel, each with the",
                       "licence it is used under and where its source is; the texts follow."],
                      sandbox(argv[2]))
    else:
        sys.exit(__doc__)
    sys.stdout.write(text)


if __name__ == "__main__":
    main(sys.argv)
