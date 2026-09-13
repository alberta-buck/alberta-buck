# SPDX-License-Identifier: GPL-3.0-or-later
"""Local-only proof/artifact helpers; never regenerate production keys or vectors."""
from dataclasses import fields
import json
from pathlib import Path
import subprocess

from eth_abi import encode
from alberta_buck.wallet.bn254 import point_to_words, g2_to_words

REPO = Path(__file__).resolve().parents[2]


def run(args, *, cwd=REPO, timeout=240, check=True):
    result = subprocess.run([str(x) for x in args], cwd=cwd, capture_output=True,
                            text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}):\n"
                           f"{result.stdout[-2000:]}\n{result.stderr[-4000:]}")
    return result


def decimal(obj):
    if isinstance(obj, dict):
        return {k: decimal(v) for k, v in obj.items()}
    if isinstance(obj, (tuple, list)):
        return [decimal(x) for x in obj]
    return str(obj) if isinstance(obj, int) else obj


def prove(tmp, witness, wasm, key):
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    ip = tmp / 'input.json'
    ip.write_text(json.dumps(decimal(witness)))
    for p in (wasm, key):
        if not Path(p).is_file():
            raise FileNotFoundError(f"Missing matched SNARK artifact: {p}; see doc/review/executable-examples.md")
    run(['node', REPO/'scripts/review/groth16.cjs', 'prove', ip, wasm, key, tmp])
    result = json.loads((tmp/'proof.json').read_text())
    p = result['proof']
    a = [int(x) for x in p['pi_a'][:2]]
    b = [[int(x) for x in row[:2][::-1]] for row in p['pi_b'][:2]]
    c = [int(x) for x in p['pi_c'][:2]]
    result['proofBytes'] = encode(['uint256[2]', 'uint256[2][2]', 'uint256[2]'], [a,b,c])
    return result


def g1tie_prove(tmp, witness):
    base = REPO/'build/snark/g1tie'
    return prove(tmp, witness, base/'identity_membership_g1tie_js/identity_membership_g1tie.wasm',
                 base/'g1tie_0001.zkey')


def spend_prove(tmp, witness):
    base = REPO/'build/snark/spend'
    return prove(tmp, witness, base/'spend_js/spend.wasm', base/'spend_final.zkey')


def ct_tuple(ct):
    return point_to_words(ct.R), point_to_words(ct.C)


def proof_tuple(proof):
    return tuple(point_to_words(v) if isinstance(v, tuple) or v is None else v
                 for f in fields(proof) for v in [getattr(proof, f.name)])


def ps_key_tuple(key):
    # Wallet FQ2 order is (real, imaginary); Ethereum uses (imaginary, real).
    return tuple(tuple(tuple(reversed(c)) for c in g2_to_words(p))
                 for p in (key.pk_X, key.pk_Y))


def register(chain, registry, account, issuer_addr, sigma, proof, registrant, leaf=None):
    args = (issuer_addr, point_to_words(account.pk), ct_tuple(account.E),
            proof_tuple(sigma), proof_tuple(proof))
    return chain.send(registry.functions.register(*args, *(() if leaf is None else (leaf,))),
                      sender=registrant)


def deploy_poseidon(chain):
    import re
    src = (REPO/'src/PoseidonT3Bytecode.sol').read_text()
    code = re.search(r'hex"([0-9a-fA-F]+)"', src).group(1)
    tx = chain.w3.eth.send_transaction(dict(from_=chain.deployer)) if False else None
    tx = chain.w3.eth.send_transaction({'from': chain.deployer, 'data': '0x'+code,
                                        'gas': 10000000, 'gasPrice': 0})
    receipt = chain.w3.eth.wait_for_transaction_receipt(tx)
    assert receipt['status'] == 1
    return receipt['contractAddress']
