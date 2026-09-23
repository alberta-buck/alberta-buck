//! The folded deposit gate's circuit witnesses -- mirrors
//! `alberta_buck/wallet/deposit_fold.py` (`deposit_fold_a1_witness`,
//! `deposit_fold_a2_witness`) relation for relation and refusal for refusal.
//!
//! Spending an addressed note proves two facts about two DIFFERENT secrets --
//! the receiving key `k` reads the note, the identity scalar `m_rec` owns the
//! payout account -- and proved side by side they say nothing about their
//! owner.  So the gate is one Groth16 proof over one witness, and every relation
//! the circuit constrains is checked here first: a witness that would fail
//! inside the prover fails here with a reason instead.
//!
//! The circuit works in 4x64-bit limbs because BN254's base field does not fit
//! the native one, and it folds sums of POINTS into sums of SCALARS before
//! multiplying (`eEnc.C = (m_rec + t*k)*G`), since circom's curve addition is
//! incomplete.  The witness is a document for snarkjs and rapidsnark, so its
//! words are DECIMAL strings.
//!
//! Where the reference takes a tree, this takes the membership PATH: a wallet
//! rebuilds its path from the published subtree and needs nothing else.

use serde_json::{json, Value};

use buck_identity::notes::{identity_leaf_salted, receiving_leaf, NULLIFIER_TAG_B};
use buck_identity::nums::h_pedersen;
use buck_identity::poseidon::poseidon;
use buck_identity::{
    fr_mod, g1_add, g1_generator, g1_mul, g1_neg, reduce_mod_order, w_from_fr,
};
use buck_registry::tree::fold_path;

use crate::{dec_w, w_from_u64, Ctw, G1w, IdError, Result, W256};

/// A membership path, as a wallet rebuilds it from a published subtree.
pub struct Path<'a> {
    pub siblings: &'a [W256],
    pub index_bits: &'a [u8],
}

/// What both flavours share: the recipient's own secrets, its account, the
/// spend's re-encryption, and the note handle.
pub struct FoldCommon<'a> {
    pub m_rec: &'a W256,
    pub k: &'a W256,
    pub sk_dep: &'a W256,
    pub salt: &'a W256,
    pub path: Path<'a>,
    pub t: &'a W256,
    pub r_e: &'a W256,
    pub e_dep: &'a Ctw,
    pub pk_dep: &'a G1w,
    pub e_enc: &'a Ctw,
    pub rho: &'a W256,
    pub id_hash: &'a W256,
    pub identity_root: &'a W256,
}

fn mulmod(a: &W256, b: &W256) -> W256 {
    w_from_fr(&(fr_mod(a) * fr_mod(b)))
}

fn addmod(a: &W256, b: &W256) -> W256 {
    w_from_fr(&(fr_mod(a) + fr_mod(b)))
}

fn g(k: &W256) -> Result<G1w> {
    g1_mul(&g1_generator(), k)
}

fn require(ok: bool, why: &'static str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(IdError(why))
    }
}

/// Little-endian 64-bit limbs, the encoding every circuit input uses.
fn limbs(w: &W256) -> Value {
    let v: Vec<String> = (0..4)
        .map(|i| {
            let off = 32 - 8 * (i + 1);
            u64::from_be_bytes(w[off..off + 8].try_into().expect("8 bytes")).to_string()
        })
        .collect();
    json!(v)
}

fn dec(w: &W256) -> Value {
    Value::String(dec_w(w))
}

/// A word reduced into the native field (`% F_R`; `F_R == ORDER` on BN254).
fn dec_fr(w: &W256) -> Value {
    dec(&reduce_mod_order(w))
}

fn path_json(p: &Path) -> (Value, Value) {
    let el: Vec<String> = p.siblings.iter().map(dec_w).collect();
    let ix: Vec<String> = p.index_bits.iter().map(|b| b.to_string()).collect();
    (json!(el), json!(ix))
}

/// Relations (2), (3) and (4), shared by both flavours: the account decrypts
/// to `m_rec`, the leaf commits `(m_rec, k)` under the holder's salt, and its
/// path folds to the posted root.  Returns `cd = m_rec + sk_dep*r_E`.
fn check_account_and_leaf(c: &FoldCommon, cd_why: &'static str) -> Result<W256> {
    let cd = addmod(c.m_rec, &mulmod(c.sk_dep, c.r_e));
    require(*c.pk_dep == g(c.sk_dep)?, "pk_dep != sk_dep*G")?;
    require(c.e_dep.0 == g(c.r_e)?, "E_dep.R != r_E*G")?;
    require(c.e_dep.1 == g(&cd)?, cd_why)?;
    let leaf = receiving_leaf(c.m_rec, c.k, c.salt)?;
    require(
        fold_path(&leaf, c.path.siblings, c.path.index_bits)? == *c.identity_root,
        "the witness root is not the posted identity root",
    )?;
    Ok(cd)
}

fn nullifier(c: &FoldCommon) -> Result<W256> {
    poseidon(&[
        reduce_mod_order(c.rho),
        reduce_mod_order(c.id_hash),
        w_from_u64(NULLIFIER_TAG_B),
    ])
}

fn account_json(c: &FoldCommon) -> Vec<(&'static str, Value)> {
    vec![
        ("eEncRx", limbs(&c.e_enc.0 .0)),
        ("eEncRy", limbs(&c.e_enc.0 .1)),
        ("eEncCx", limbs(&c.e_enc.1 .0)),
        ("eEncCy", limbs(&c.e_enc.1 .1)),
        ("pkDepX", limbs(&c.pk_dep.0)),
        ("pkDepY", limbs(&c.pk_dep.1)),
        ("eDepRx", limbs(&c.e_dep.0 .0)),
        ("eDepRy", limbs(&c.e_dep.0 .1)),
        ("eDepCx", limbs(&c.e_dep.1 .0)),
        ("eDepCy", limbs(&c.e_dep.1 .1)),
    ]
}

fn ct4(c: &Ctw) -> Value {
    json!([
        dec_w(&reduce_mod_order(&c.0 .0)),
        dec_w(&reduce_mod_order(&c.0 .1)),
        dec_w(&reduce_mod_order(&c.1 .0)),
        dec_w(&reduce_mod_order(&c.1 .1)),
    ])
}

fn object(pairs: Vec<(&'static str, Value)>) -> Value {
    Value::Object(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

/// The A1 witness: `eNote` is pinned against the PUBLIC face, which is what
/// makes the addressed Identity unique, and no curve addition is needed at all.
#[allow(clippy::too_many_arguments)]
pub fn deposit_fold_a1_witness(
    c: &FoldCommon,
    e_note: &Ctw,
    v: &W256,
    m_issuer: &W256,
    sigma_r: &G1w,
    sigma_s: &W256,
    r_note: &W256,
) -> Result<Value> {
    let u = addmod(v, &mulmod(r_note, c.k)); // eNote.C = u*G
    let w = addmod(c.m_rec, &mulmod(c.t, c.k)); // eEnc.C  = w*G
    require(e_note.0 == g(r_note)?, "eNote.R != rn*G")?;
    require(e_note.1 == g(&u)?, "eNote.C != u*G (u = v + rn*k)")?;
    require(c.e_enc.0 == g(c.t)?, "eEnc.R != t*G")?;
    require(c.e_enc.1 == g(&w)?, "eEnc.C != w*G (w = m_rec + t*k)")?;
    let cd = check_account_and_leaf(c, "E_dep.C != cd*G (cd = m_rec + sk*r_E)")?;
    let (pe, pi) = path_json(&c.path);

    let mut pairs = vec![
        ("nullifier", dec(&nullifier(c)?)),
        ("v", dec(v)),
        ("identityRoot", dec(c.identity_root)),
    ];
    pairs.extend(account_json(c));
    pairs.extend(vec![
        ("rho", dec_fr(c.rho)),
        ("idHash", dec_fr(c.id_hash)),
        ("eNote", ct4(e_note)),
        ("mIss", dec_fr(m_issuer)),
        ("sigR", json!([dec_w(&reduce_mod_order(&sigma_r.0)), dec_w(&reduce_mod_order(&sigma_r.1))])),
        ("sigS", dec_fr(sigma_s)),
        ("rn", limbs(&reduce_mod_order(r_note))),
        ("m_rec", limbs(&reduce_mod_order(c.m_rec))),
        ("k_recv", limbs(&reduce_mod_order(c.k))),
        ("u", limbs(&u)),
        ("t", limbs(&reduce_mod_order(c.t))),
        ("w", limbs(&w)),
        ("sk_dep", limbs(&reduce_mod_order(c.sk_dep))),
        ("r_E", limbs(&reduce_mod_order(c.r_e))),
        ("cd", limbs(&cd)),
        ("salt", dec(c.salt)),
        ("pathElements", pe),
        ("pathIndices", pi),
    ]);
    Ok(object(pairs))
}

/// The A2 witness.  The note decrypts to the ISSUER's Identity, a point the
/// spender holds no scalar for, so it enters as witnessed coordinates -- and
/// since circom offers only incomplete addition, the precondition is checked
/// at each addition: the two x-coordinates must differ, which excludes the
/// doubling and the negation together.  Relation (5): that Identity is itself
/// registered, under the salt of the issuer's naming association.
#[allow(clippy::too_many_arguments)]
/// `t` is the mint binding's `T`, which `idHash` commits, and `gamma` its blind:
/// the key tie `T = rm*G + gamma*H` says the binding's key is the spender's own.
#[allow(clippy::too_many_arguments)]
pub fn deposit_fold_a2_witness(
    c: &FoldCommon,
    e_note: &Ctw,
    e_iss: &Ctw,
    r_prime: &W256,
    salt_iss: &W256,
    iss_path: Path,
    t: &G1w,
    gamma: &W256,
) -> Result<Value> {
    // What k decrypts the spend's re-encryption to: the issuer Identity.
    let m_i = g1_add(&c.e_enc.1, &g1_neg(&g1_mul(&c.e_enc.0, c.k)?)?)?;
    let rm = mulmod(r_prime, c.k); // eIss.C = M_I + rm*G
    let tk = mulmod(c.t, c.k); // eEnc.C = M_I + tk*G
    let rm_g = g(&rm)?;
    let tk_g = g(&tk)?;
    require(e_iss.0 == g(r_prime)?, "eIss.R != r'*G")?;
    require(e_iss.1 == g1_add(&m_i, &rm_g)?, "eIss.C != M_I + rm*G")?;
    require(c.e_enc.0 == g(c.t)?, "eEnc.R != t*G")?;
    require(c.e_enc.1 == g1_add(&m_i, &tk_g)?, "eEnc.C != M_I + tk*G")?;
    let gamma = reduce_mod_order(gamma);
    let g_h = g1_mul(&h_pedersen(), &gamma)?;
    require(
        *t == g1_add(&rm_g, &g_h)?,
        "T != rm*G + gamma*H (keyed to another mailbox)",
    )?;
    let mx = reduce_mod_order(&m_i.0);
    require(
        mx != reduce_mod_order(&rm_g.0),
        "eIss.C: the addends share an x-coordinate mod F_R, so the incomplete addition \
         would land on a doubling or the identity",
    )?;
    require(
        mx != reduce_mod_order(&tk_g.0),
        "eEnc.C: the addends share an x-coordinate mod F_R, so the incomplete addition \
         would land on a doubling or the identity",
    )?;
    require(
        reduce_mod_order(&rm_g.0) != reduce_mod_order(&g_h.0),
        "T: the addends share an x-coordinate mod F_R, so the incomplete addition \
         would land on a doubling or the identity",
    )?;
    let cd = check_account_and_leaf(c, "E_dep.C != cd*G")?;
    let iss_leaf = identity_leaf_salted(&m_i, salt_iss)?;
    require(
        fold_path(&iss_leaf, iss_path.siblings, iss_path.index_bits)? == *c.identity_root,
        "the shipped issuer salt does not open a leaf under the posted root",
    )?;
    let (pe, pi) = path_json(&c.path);
    let (ipe, ipi) = path_json(&iss_path);

    let mut pairs = vec![
        ("nullifier", dec(&nullifier(c)?)),
        ("identityRoot", dec(c.identity_root)),
    ];
    pairs.extend(account_json(c));
    pairs.extend(vec![
        ("rho", dec_fr(c.rho)),
        ("idHash", dec_fr(c.id_hash)),
        ("eNote", ct4(e_note)),
        ("eIss0", ct4(e_iss)),
        ("T", json!([dec_fr(&t.0), dec_fr(&t.1)])),
        ("r", limbs(&reduce_mod_order(r_prime))),
        ("k_recv", limbs(&reduce_mod_order(c.k))),
        ("rm", limbs(&rm)),
        ("t", limbs(&reduce_mod_order(c.t))),
        ("tk", limbs(&tk)),
        ("m_rec", limbs(&reduce_mod_order(c.m_rec))),
        ("sk_dep", limbs(&reduce_mod_order(c.sk_dep))),
        ("r_E", limbs(&reduce_mod_order(c.r_e))),
        ("cd", limbs(&cd)),
        ("gamma", limbs(&gamma)),
        ("MI", json!([limbs(&m_i.0), limbs(&m_i.1)])),
        ("salt", dec(c.salt)),
        ("saltIss", dec(salt_iss)),
        ("pathElements", pe),
        ("pathIndices", pi),
        ("issPathElements", ipe),
        ("issPathIndices", ipi),
    ]);
    Ok(object(pairs))
}
