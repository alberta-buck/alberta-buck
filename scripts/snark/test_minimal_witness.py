#!/usr/bin/env python3
"""Minimal witness test: Poseidon hashes + cross-constraints only, no EC ops.

Uses the same witness values as the full note_binding circuit to isolate
whether the failure is in the EC operations or the Poseidon/cross-constraint
logic.
"""
import json, subprocess, sys, os

sys.path.insert(0, '/Users/perry/src/alberta-buck')
from alberta_buck.wallet.poseidon import poseidon, F_R
from alberta_buck.wallet.notes import NULLIFIER_TAG_B

REPO = '/Users/perry/src/alberta-buck'
BUILD = os.path.join(REPO, 'build', 'snark', 'test_nb_min')

def main():
    # Read values from the full note_binding input
    with open(os.path.join(REPO, 'build/snark/note_binding/input.json')) as f:
        d = json.load(f)

    rho = int(d['rho'])
    idHash = int(d['idHash'])
    nullifier = int(d['nullifier'])
    eNote = [int(x) for x in d['eNote']]
    eIss0 = [int(x) for x in d['eIss0']]
    R0x_limb = [int(x) for x in d['R0_limb'][0]]
    R0y_limb = [int(x) for x in d['R0_limb'][1]]

    # Verify off-chain
    assert nullifier == poseidon([rho, idHash, NULLIFIER_TAG_B]) % F_R
    assert idHash == poseidon(eNote + eIss0) % F_R
    R0x_full = sum(R0x_limb[i] << (64*i) for i in range(4))
    assert R0x_full % F_R == eIss0[0], f'{R0x_full % F_R} != {eIss0[0]}'
    R0y_full = sum(R0y_limb[i] << (64*i) for i in range(4))
    assert R0y_full % F_R == eIss0[1], f'{R0y_full % F_R} != {eIss0[1]}'
    print("Python: all cross-constraints verified OK")

    # Write minimal input
    witness = {
        'nullifier': str(nullifier),
        'rho': str(rho),
        'idHash': str(idHash),
        'eNote': [str(x) for x in eNote],
        'eIss0': [str(x) for x in eIss0],
        'R0x_limb': [str(x) for x in R0x_limb],
        'R0y_limb': [str(x) for x in R0y_limb],
    }
    os.makedirs(BUILD, exist_ok=True)
    with open(os.path.join(BUILD, 'input.json'), 'w') as f:
        json.dump(witness, f, indent=2)
    print(f"Input written to {BUILD}/input.json")

    # Compile circuit
    circom_cmd = [
        'circom', os.path.join(REPO, 'circuits/test_nb_minimal.circom'),
        '--c', '--no_asm', '--output', BUILD,
    ]
    subprocess.run(circom_cmd, check=True, cwd=REPO)

    # Build C++ witness
    cpp_dir = os.path.join(BUILD, 'test_nb_minimal_cpp')
    subprocess.run(['make', 'clean'], cwd=cpp_dir, capture_output=True)
    subprocess.run(['make',
        'CC=g++',
        f'CFLAGS=-std=c++11 -O3 -I. -Wno-deprecated-declarations -fpermissive',
    ], cwd=cpp_dir, check=True)

    # Run witness
    exe = os.path.join(cpp_dir, 'test_nb_minimal')
    result = subprocess.run([exe,
        os.path.join(BUILD, 'input.json'),
        os.path.join(BUILD, 'witness.wtns'),
    ], capture_output=True, text=True)
    print("stdout:", result.stdout)
    print("stderr:", result.stderr)
    print(f"Exit: {result.returncode}")
    if result.returncode == 0:
        print("SUCCESS: Poseidon + cross-constraints passed!")
    else:
        print("FAILED: issue is in Poseidon or cross-constraints, not EC ops")


if __name__ == '__main__':
    main()
