//! Insurance regulator: a periodic attestor whose public subtrees gate credit
//! issuance -- mirrors `alberta_buck/registry/regulator.py` (accumulator
//! specification, section 12).
//!
//! The regulator owns one PUBLIC subtree per predicate it attests: standing,
//! the face band, each depreciation model, the maximum depreciation and premium
//! rates, the general scope, and each named asset scope.  An insurer's envelope
//! is its membership in those subtrees; `BuckCredit.attestInsurer` verifies the
//! paths once per review period and caches the result, and `check_issuance` is
//! the gate it then applies, reasons and all.  There is no asset taxonomy: the
//! gate checks the credit's own parameters and a scope the insurer declares.

use std::collections::BTreeSet;

use crate::tree::{identity_leaf, IdentityMerkleTree, MembershipProof, EMPTY_LEAF};
use buck_identity::keccak::keccak_raw;
use buck_identity::{G1w, IdError, Result, W256, ZERO_W};

/// Mirrors `BuckCredit.DepreciationType`.
pub const DEP_NONE: u8 = 0;
pub const DEP_LINEAR: u8 = 1;
pub const DEP_DECLINING_BALANCE: u8 = 2;

/// BuckCredit stores monetary quantities with six decimals.
pub const BUCK_DECIMALS: u32 = 6;
/// Bands are powers of ten of face, from ten BUCK to a hundred million.
pub const FACE_BAND_MAX: u8 = 8;
/// The scope the eight-argument `createCredit` declares.
pub const GENERAL_SCOPE: W256 = ZERO_W;
/// Default depth of a regulator's subtrees (`FEATURE_SUBTREE_DEPTH`).
pub const FEATURE_SUBTREE_DEPTH: usize = 10;

/// The exclusive face ceiling of `band`, in BuckCredit units.
pub fn band_ceiling(band: u8) -> Result<u128> {
    if !(1..=FACE_BAND_MAX).contains(&band) {
        return Err(IdError("band must be in [1, FACE_BAND_MAX]"));
    }
    Ok(10u128.pow(band as u32 + BUCK_DECIMALS))
}

/// The smallest band admitting `face_units`, or FACE_BAND_MAX + 1 if none.
pub fn band_for_face(face_units: u128) -> u8 {
    for band in 1..=FACE_BAND_MAX {
        if face_units < 10u128.pow(band as u32 + BUCK_DECIMALS) {
            return band;
        }
    }
    FACE_BAND_MAX + 1
}

/// The identifier of a scope: keccak256 of its namespaced name.  Names, not
/// codes, so refinement needs no version.
pub fn scope_id(name: &str) -> Result<W256> {
    if name.is_empty() {
        return Err(IdError("scope name must be a non-empty string"));
    }
    Ok(keccak_raw(name.as_bytes()))
}

/// The on-chain key of a subtree: keccak of its namespaced name.
pub fn subtree_key(name: &str) -> Result<W256> {
    scope_id(name)
}

/// What a regulator attested, and what BuckCredit caches and checks.  The
/// underwriter risk estimate is deliberately absent.
#[derive(Debug, Clone, PartialEq)]
pub struct InsurerEnvelope {
    pub standing: bool,
    pub face_band: u8,
    pub dep_types: BTreeSet<u8>,
    pub max_dep_rate: u32,
    pub max_premium_rate: u32,
    pub expires_at: f64,
    pub scopes: BTreeSet<W256>,
}

impl InsurerEnvelope {
    pub fn new(
        standing: bool,
        face_band: u8,
        dep_types: BTreeSet<u8>,
        max_dep_rate: u32,
        max_premium_rate: u32,
        expires_at: f64,
        scopes: BTreeSet<W256>,
    ) -> Result<Self> {
        if !(1..=FACE_BAND_MAX).contains(&face_band) {
            return Err(IdError("face_band must be in [1, FACE_BAND_MAX]"));
        }
        if dep_types.iter().any(|d| *d > DEP_DECLINING_BALANCE) {
            return Err(IdError("unknown depreciation type"));
        }
        Ok(InsurerEnvelope { standing, face_band, dep_types, max_dep_rate, max_premium_rate, expires_at, scopes })
    }
}

/// The gate, as `BuckCredit.createCredit` performs it: `Ok` if the credit is
/// admitted, otherwise the reason, which is BuckCredit's revert string.
#[allow(clippy::too_many_arguments)]
pub fn check_issuance(
    env: &InsurerEnvelope,
    scope: &W256,
    face_units: u128,
    dep_type: u8,
    dep_rate: u32,
    premium_rate: u32,
    now: f64,
) -> Result<()> {
    if !env.standing {
        return Err(IdError("insurer not in good standing"));
    }
    if now > env.expires_at {
        return Err(IdError("attestation expired"));
    }
    if !env.scopes.contains(scope) {
        return Err(IdError("scope not attested"));
    }
    if band_for_face(face_units) > env.face_band {
        return Err(IdError("face above attested band"));
    }
    if !env.dep_types.contains(&dep_type) {
        return Err(IdError("depreciation model not attested"));
    }
    if dep_rate > env.max_dep_rate {
        return Err(IdError("depreciation rate above attested maximum"));
    }
    if premium_rate > env.max_premium_rate {
        return Err(IdError("premium rate above attested maximum"));
    }
    Ok(())
}

/// A jurisdiction's insurance regulator, as a public attribute authority.
#[derive(Debug, Clone)]
pub struct InsuranceRegulator {
    pub jurisdiction: String,
    pub tree_depth: usize,
    /// (suffix, tree) in creation order, as the Python dict keeps them.
    trees: Vec<(String, IdentityMerkleTree)>,
    envelopes: Vec<(G1w, InsurerEnvelope)>,
}

impl InsuranceRegulator {
    pub fn new(jurisdiction: &str, tree_depth: usize) -> Result<Self> {
        if jurisdiction.is_empty() {
            return Err(IdError("jurisdiction must be a non-empty string"));
        }
        Ok(InsuranceRegulator {
            jurisdiction: jurisdiction.to_string(),
            tree_depth,
            trees: Vec::new(),
            envelopes: Vec::new(),
        })
    }

    /// The namespaced identifier of one of this regulator's subtrees.
    pub fn subtree_id(&self, suffix: &str) -> String {
        format!("regulator:{}:{}", self.jurisdiction, suffix)
    }

    /// The namespaced name of an asset scope, e.g. "asset:vehicle:car".
    pub fn scope_name(&self, asset_path: &str) -> String {
        self.subtree_id(&format!("scope:{asset_path}"))
    }

    fn tree_mut(&mut self, suffix: &str) -> Result<&mut IdentityMerkleTree> {
        if let Some(i) = self.trees.iter().position(|(s, _)| s == suffix) {
            return Ok(&mut self.trees[i].1);
        }
        self.trees.push((suffix.to_string(), IdentityMerkleTree::new(self.tree_depth)?));
        Ok(&mut self.trees.last_mut().unwrap().1)
    }

    /// Each predicate the envelope asserts, as a subtree suffix, in the order
    /// `BuckCredit.attestInsurer` takes their paths.
    fn suffixes(env: &InsurerEnvelope, scope_names: &[&str]) -> Vec<String> {
        let mut out = Vec::new();
        if env.standing {
            out.push("insurer".to_string());
        }
        out.push(format!("insurer:face:{}", env.face_band));
        for d in &env.dep_types {
            out.push(format!("insurer:dep:{d}"));
        }
        out.push(format!("insurer:depRate:{}", env.max_dep_rate));
        out.push(format!("insurer:premium:{}", env.max_premium_rate));
        if env.scopes.contains(&GENERAL_SCOPE) {
            out.push("insurer:general".to_string());
        }
        for n in scope_names {
            out.push(format!("scope:{n}"));
        }
        out
    }

    /// The namespaced subtree names an attestation proves membership in.
    pub fn predicate_names(&self, env: &InsurerEnvelope, scope_names: &[&str]) -> Vec<String> {
        Self::suffixes(env, scope_names).iter().map(|s| self.subtree_id(s)).collect()
    }

    /// Attest an insurer's envelope for this review period.
    pub fn attest(
        &mut self,
        m_point: &G1w,
        env: &InsurerEnvelope,
        scope_names: &[&str],
        general: bool,
    ) -> Result<InsurerEnvelope> {
        let mut scopes = BTreeSet::new();
        for n in scope_names {
            scopes.insert(scope_id(&self.scope_name(n))?);
        }
        if general {
            scopes.insert(GENERAL_SCOPE);
        }
        if !env.scopes.is_empty() && env.scopes != scopes {
            return Err(IdError("env.scopes must match scope_names and general, or be empty"));
        }
        let env = InsurerEnvelope { scopes, ..env.clone() };
        let leaf = identity_leaf(m_point)?;
        for sfx in Self::suffixes(&env, scope_names) {
            let t = self.tree_mut(&sfx)?;
            if !t.contains(&leaf) {
                t.insert_leaf(leaf);
            }
        }
        self.envelopes.retain(|(m, _)| m != m_point);
        self.envelopes.push((*m_point, env.clone()));
        Ok(env)
    }

    /// Clear an insurer from every subtree it is in; the number cleared.
    pub fn revoke(&mut self, m_point: &G1w) -> Result<usize> {
        let leaf = identity_leaf(m_point)?;
        let mut cleared = 0;
        for (_, t) in self.trees.iter_mut() {
            if let Ok(i) = t.index_of_leaf(&leaf) {
                t.set_leaf(i, EMPTY_LEAF)?;
                cleared += 1;
            }
        }
        self.envelopes.retain(|(m, _)| m != m_point);
        Ok(cleared)
    }

    pub fn envelope_of(&self, m_point: &G1w) -> Option<&InsurerEnvelope> {
        self.envelopes.iter().find(|(m, _)| m == m_point).map(|(_, e)| e)
    }

    /// A plain Merkle path proving `M` is in this regulator's `suffix` subtree.
    pub fn membership_proof(&self, m_point: &G1w, suffix: &str) -> Result<Option<MembershipProof>> {
        let Some((_, t)) = self.trees.iter().find(|(s, _)| s == suffix) else {
            return Ok(None);
        };
        match t.index_of_leaf(&identity_leaf(m_point)?) {
            Ok(i) => Ok(Some(t.path(i)?)),
            Err(_) => Ok(None),
        }
    }

    /// Every subtree's identifier and current root, for the aggregator.
    pub fn sub_roots(&self) -> Result<Vec<(String, W256)>> {
        self.trees.iter().map(|(s, t)| Ok((self.subtree_id(s), t.root()?))).collect()
    }
}
