"""Phase 4 Stage 4 acceptance: the canonical mini-scenario's JS and Python
journals AGREE.

Three runs of core/vectors/mini-scenario.json:

  A. Python  -> a fresh anvil   (alberta_buck.sim.mini, in-process)
  B. node    -> a fresh anvil   (core/js/bin/mini-sim.mjs --backend anvil)
  C. node    -> tevm in-process (core/js/bin/mini-sim.mjs --backend tevm)

Comparisons (per record, in order):

  A vs B: the SEMANTIC fields (i, tag, op, fn, sender, expect, outcome,
          matched, err) AND gas -- same dev account 0, same op order on a
          fresh chain means identical addresses, calldata, and execution.
          This is the cross-PLATFORM acceptance: byte-equal streams.
  B vs C: the semantic fields exactly.  Gas is compared but only
          summarized: tevm's ethereumjs EVM prices a minority of ops
          differently from anvil/revm (observed: the SimLP full-range
          liquidity mint, 321801 vs 317372) -- "anvil remains the
          fidelity anchor" (platform doc, Risks).  We assert MOST
          records' gas agrees so a wholesale drift still fails.

Skips cleanly if anvil / node / web3 / core-js deps / artifacts are missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

anvil_missing = shutil.which("anvil") is None
node_missing = shutil.which("node") is None
web3_missing = False
try:
    import web3  # noqa: F401
except Exception:
    web3_missing = True
js_deps_missing = not (_REPO / "core" / "js" / "node_modules").is_dir()
artifacts_missing = not (_REPO / "out" / "MockERC20.sol" / "MockERC20.json").exists()

SEMANTIC = ("i", "tag", "op", "fn", "sender", "expect", "outcome", "matched", "err")


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _project(rec: dict, fields) -> dict:
    return {k: rec.get(k) for k in fields}


def _diff(a: list[dict], b: list[dict], fields, label: str) -> None:
    assert len(a) == len(b), (
        f"{label}: record counts differ ({len(a)} vs {len(b)}); "
        f"tags a={[r['tag'] for r in a[:6]]}... b={[r['tag'] for r in b[:6]]}..."
    )
    for ra, rb in zip(a, b):
        pa, pb = _project(ra, fields), _project(rb, fields)
        assert pa == pb, f"{label}: record #{ra['i']} differs:\n  a={pa}\n  b={pb}"


@pytest.mark.skipif(anvil_missing or node_missing or web3_missing
                    or js_deps_missing or artifacts_missing,
                    reason="anvil/node/web3/core-js deps/artifacts unavailable")
def test_mini_scenario_journal_parity(tmp_path):
    from alberta_buck.sim.anvil import Anvil
    from alberta_buck.sim.chain import Chain
    from alberta_buck.sim.mini import load_scenario, run_mini

    sc = load_scenario()

    # ---- A: Python drives a fresh anvil --------------------------------
    j_py = tmp_path / "mini-py.jsonl"
    with Anvil() as anvil:
        chain = Chain(anvil.w3, anvil.w3.eth.accounts[0], journal=j_py)
        run_mini(chain, sc)
        assert not chain.mismatches, "python run: declared expectations must match"

    # ---- B: node drives a fresh anvil -----------------------------------
    j_js_anvil = tmp_path / "mini-js-anvil.jsonl"
    with Anvil() as anvil:
        out = subprocess.run(
            ["node", "bin/mini-sim.mjs", "--backend", "anvil",
             "--rpc", f"http://127.0.0.1:{anvil.port}",
             "--journal", str(j_js_anvil)],
            cwd=_REPO / "core" / "js", capture_output=True, text=True)
        assert out.returncode == 0, f"js/anvil run failed:\n{out.stdout}\n{out.stderr}"

    # ---- C: node drives tevm in-process ----------------------------------
    j_js_tevm = tmp_path / "mini-js-tevm.jsonl"
    out = subprocess.run(
        ["node", "bin/mini-sim.mjs", "--backend", "tevm",
         "--journal", str(j_js_tevm)],
        cwd=_REPO / "core" / "js", capture_output=True, text=True)
    assert out.returncode == 0, f"js/tevm run failed:\n{out.stdout}\n{out.stderr}"

    a = _load(j_py)
    b = _load(j_js_anvil)
    c = _load(j_js_tevm)
    assert len(a) > 20, "mini-scenario should journal a real op stream"

    # The cross-PLATFORM acceptance: python and JS on the same backend
    # agree on every semantic field AND gas.
    _diff(a, b, SEMANTIC + ("gas",), "py-anvil vs js-anvil")

    # The cross-BACKEND check: same platform, tevm vs anvil -- semantics
    # exactly; gas summarized (tevm prices a minority of ops differently).
    _diff(b, c, SEMANTIC, "js-anvil vs js-tevm")
    gas_diffs = [(rb["i"], rb["tag"], rb["gas"], rc["gas"])
                 for rb, rc in zip(b, c) if rb["gas"] != rc["gas"]]
    if gas_diffs:
        print(f"tevm gas divergence on {len(gas_diffs)}/{len(b)} records: "
              f"{gas_diffs[:6]}")
    assert len(gas_diffs) <= len(b) // 4, (
        f"tevm gas diverges from anvil on {len(gas_diffs)}/{len(b)} records"
        " -- wholesale drift, not the known minority")
