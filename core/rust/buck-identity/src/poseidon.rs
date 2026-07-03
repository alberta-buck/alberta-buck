//! Poseidon over BN254 -- circomlibjs's unoptimized variant, byte-identical
//! to `alberta_buck/wallet/poseidon.py` (the same vendored constants file,
//! compiled in by `build.rs`).
//!
//! State width t = arity + 1; standard Hades parameters (8 full rounds,
//! per-t partial rounds).  Inputs are reduced mod F_R (= ORDER) exactly as
//! the Python `_to_int(x) % F_R` does.

use std::sync::OnceLock;

use ark_bn254::Fr;
use ark_ff::{BigInt, Field, PrimeField, Zero};

use crate::{fr_mod, w_from_fr, IdError, Result, W256};

mod consts {
    include!(concat!(env!("OUT_DIR"), "/poseidon_constants.rs"));
}

/// Partial-round counts per state width t = 2..=17 (Poseidon paper table 2).
const N_ROUNDS_P: [usize; 16] = [
    56, 57, 56, 60, 60, 63, 64, 63, 60, 66, 60, 65, 70, 60, 64, 68,
];
const N_ROUNDS_F: usize = 8;

struct Params {
    c: Vec<Fr>,
    m: Vec<Fr>, // t x t, row-major
}

fn params() -> &'static Vec<Params> {
    static PARAMS: OnceLock<Vec<Params>> = OnceLock::new();
    PARAMS.get_or_init(|| {
        (0..16)
            .map(|i| Params {
                c: consts::C_ALL[i]
                    .iter()
                    .map(|l| {
                        Fr::from_bigint(BigInt::new(*l)).expect("poseidon constant out of field")
                    })
                    .collect(),
                m: consts::M_ALL[i]
                    .iter()
                    .map(|l| {
                        Fr::from_bigint(BigInt::new(*l)).expect("poseidon constant out of field")
                    })
                    .collect(),
            })
            .collect()
    })
}

fn pow5(x: Fr) -> Fr {
    let x2 = x.square();
    x2.square() * x
}

/// Poseidon over field elements (internal fast path).
pub(crate) fn poseidon_fr(inputs: &[Fr]) -> Result<Fr> {
    let n = inputs.len();
    if !(1..=16).contains(&n) {
        return Err(IdError("poseidon arity must be 1..16"));
    }
    let t = n + 1;
    let p = &params()[t - 2];
    let n_p = N_ROUNDS_P[t - 2];

    let mut state = vec![Fr::zero(); t];
    state[1..].copy_from_slice(inputs);

    for r in 0..(N_ROUNDS_F + n_p) {
        // ARK: add round constants.
        for (i, s) in state.iter_mut().enumerate() {
            *s += p.c[r * t + i];
        }
        // SBox: full rounds every cell, partial rounds cell 0 only.
        if r < N_ROUNDS_F / 2 || r >= N_ROUNDS_F / 2 + n_p {
            for s in state.iter_mut() {
                *s = pow5(*s);
            }
        } else {
            state[0] = pow5(state[0]);
        }
        // MIX: state <- M * state (row-major).
        let mut next = vec![Fr::zero(); t];
        for (i, nx) in next.iter_mut().enumerate() {
            let row = &p.m[i * t..(i + 1) * t];
            let mut acc = Fr::zero();
            for (j, s) in state.iter().enumerate() {
                acc += row[j] * s;
            }
            *nx = acc;
        }
        state = next;
    }
    Ok(state[0])
}

/// `Poseidon(inputs)` with each input word reduced mod F_R -- matches
/// `circomlibjs.buildPoseidon()` and the circuit Poseidon.
pub fn poseidon(inputs: &[W256]) -> Result<W256> {
    let frs: Vec<Fr> = inputs.iter().map(fr_mod).collect();
    Ok(w_from_fr(&poseidon_fr(&frs)?))
}
