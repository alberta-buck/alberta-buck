"""Mixed-language sim smoke: JS agents join a Python-deployed anvil.

The Phase 1 acceptance for the JS ChainSession (alberta-buck-platform.org):
Python deploys the real routing stack onto anvil, then the JS walking-
skeleton agents (core/js: PinWhale + RoundTripTrader) connect over HTTP
and act on the SAME pools -- the whale snaps TOKEN/USDC to a price path,
the trader round-trips through it -- writing the shared JSONL journal,
which this test validates with the Python reader.

Skips cleanly when anvil, node, the core/js dependencies, or the Foundry
artifacts are unavailable.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

anvil_missing = shutil.which("anvil") is None
node_missing = shutil.which("node") is None
web3_missing = False
try:
    import web3  # noqa: F401
except Exception:
    web3_missing = True

REPO = Path(__file__).resolve().parents[2]
js_deps_missing = not (REPO / "core" / "js" / "node_modules" / "viem").is_dir()
artifacts_missing = not (REPO / "out" / "MockERC20.sol" / "MockERC20.json").exists()


@pytest.mark.skipif(anvil_missing or node_missing or web3_missing
                    or js_deps_missing or artifacts_missing,
                    reason="anvil/node/web3/core-js-deps/artifacts unavailable")
def test_js_agents_join_python_sim(tmp_path):
    from buck_core.session import Journal
    from alberta_buck.sim import identity as idmod
    from alberta_buck.sim.anvil import Anvil
    from alberta_buck.sim.chain import Chain
    from alberta_buck.sim.deploy import deploy
    from alberta_buck.sim.scenario import SCENARIOS

    sc = SCENARIOS["routing"]
    with Anvil() as anvil:
        chain = Chain(anvil.w3, anvil.w3.eth.accounts[0])
        d = deploy(chain, anvil, sc, idmod.seeded_rng(sc.seed), verbose=False)

        # The whale path: day 0 at the deployed reference, then +5%, -5%.
        ref0 = sc.prices.ref(0, 0)             # USDC-micro per whole token
        targets = [ref0, ref0 * 105 // 100, ref0 * 95 // 100]
        journal = tmp_path / "join.jsonl"
        cfg = tmp_path / "join.json"
        cfg.write_text(json.dumps({
            "rpc": f"http://127.0.0.1:{anvil.port}",
            "simlp": d.simlp.address,
            "pool": d.pool_usdc[0],
            "token": d.tokens[0].address,
            "usdc": d.usdc.address,
            "tokenDec": d.dec[0],
            "targets": [str(t) for t in targets],
            "days": 3,
            "tradeTokens": "25",
            "journal": str(journal),
        }))
        proc = subprocess.run(
            ["node", str(REPO / "core" / "js" / "bin" / "join-sim.mjs"),
             "--config", str(cfg)],
            capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, f"join-sim failed:\n{proc.stderr[-2000:]}"
        summary = json.loads(proc.stdout.strip().splitlines()[-1])

        # The JS run really happened, with no expectation mismatches.
        assert summary["mismatches"] == 0
        assert summary["sessionMismatches"] == 0
        assert summary["ops"]["send"] >= 3 * 4      # trader legs alone
        assert summary["reverts"] == 1              # the declared SPL demo

        # The Python journal reader agrees with the JS summary.
        entries = Journal.load(journal)
        assert len(entries) == summary["entries"]
        assert Journal.mismatches(entries) == []
        spl = [e for e in entries if e["tag"] == "trader:spl-demo:d0"]
        assert len(spl) == 1 and spl[0]["err"] == "SPL"

        # The JS whale really moved the PYTHON-deployed pool: final spot
        # within 2% of the last target (trader fees nudge it slightly).
        spot = int(summary["spot"])
        target = targets[2]
        assert abs(spot - target) * 100 < target * 2, (spot, target)
