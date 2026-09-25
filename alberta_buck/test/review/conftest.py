# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in expensive evidence checks fail on missing tools when requested."""
import os
import shutil
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parents[3]


def pytest_addoption(parser):
    parser.addoption("--review-integration", action="store_true",
                     help="Run local Anvil, WASM and real Groth16 review examples")
    parser.addoption("--review-pq", action="store_true",
                     help="Run real ML-KEM/AES-GCM receipt prototype (review extra)")


@pytest.fixture(params=["py", "kernel"])
def backend(request, monkeypatch):
    monkeypatch.setenv("BUCK_IDENTITY_BACKEND", request.param)
    if request.param == "kernel":
        from alberta_buck.wallet._kernel import kernel
        try:
            kernel()
        except ImportError:
            if request.config.getoption("--review-integration"):
                pytest.fail("Rust/PyO3 kernel required: make core-build-py")
            pytest.skip("Rust/PyO3 kernel not built: make core-build-py")
    return request.param


@pytest.fixture(scope="session")
def integration(request):
    if not request.config.getoption("--review-integration"):
        pytest.skip("opt in with --review-integration")
    for name in ("node", "anvil", "circom"):
        if not shutil.which(name):
            pytest.fail(f"{name} missing; run in the Nix dev shell")
    return REPO


@pytest.fixture
def evm(integration):
    from alberta_buck.sim.anvil import Anvil
    from alberta_buck.sim.chain import Chain
    with Anvil(chain_id=1, auto_impersonate=True) as anvil:
        yield anvil, Chain(anvil.w3, anvil.w3.eth.accounts[0])
