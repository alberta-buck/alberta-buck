// Wallet-kernel conformance, JS side: replay
// core/vectors/wallet-kernel-vectors.json (emitted by the Python
// reference via alberta_buck.wallet.wallet_kernel_vectors, nonces
// included) through the buck-wallet wasm kernel: canonical dialect,
// AB-RCPT/1 envelope, every receipt build, the tier-1 verifier, the
// unilateral A1/A2 flows and the issuer ceremony.  The Rust and Python
// suites assert the same file.
//
// Build the kernel first:  make nix-core-build-wasm

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

let w = null;
try {
  w = await import("../src/wallet.js");
} catch {
  // wasm not built; tests below skip
}
const skip = w ? false : "kernel not built (make nix-core-build-wasm)";

const WV = JSON.parse(readFileSync(
  fileURLToPath(new URL("../../vectors/wallet-kernel-vectors.json", import.meta.url)),
  "utf8"));

const utf8 = (s) => new TextEncoder().encode(s);

test("canonical JSON dialect", { skip }, () => {
  for (const row of WV.canonical_json) {
    assert.equal(w.canonicalJson(row.input), row.canonical);
  }
  assert.throws(() => w.canonicalJson('{"x": 1.5}'));
});

test("envelope mechanics", { skip }, () => {
  for (const row of WV.envelope) {
    const blob = utf8(row.canonical);
    assert.equal(w.receiptId(blob), row.id12);
    assert.equal(w.receiptId(blob, 20), row.id20);
    assert.equal(w.envelopeText(blob), row.envelope64);
    assert.equal(w.envelopeText(blob, 8), row.envelope8);
    assert.deepEqual(w.parseEnvelope(row.envelope64), blob);
    assert.deepEqual(w.parseEnvelope(row.noisy), blob);
  }
  assert.throws(() => w.parseEnvelope("no header .END"));
});

function partyArgs(name) {
  const p = WV.parties[name];
  return { addr: p.addr, identity: p.identity, M: p.M, pk: p.pk, E: p.E, sk: p.sk };
}

function receiptArgs(row) {
  const kind = row.kind;
  const args = {
    kind: kind === "note-a2-unbound" ? "note-a2" : kind,
    role: row.role,
    chainid: WV.chainid,
    contracts: WV.contracts,
    payer: partyArgs(row.payer),
    payee: partyArgs(row.payee),
    txn: row.txn,
    nonces: row.nonces,
  };
  if (kind === "eoa-priv") {
    args.E_for_payee = row.E_for_payee;
    args.cp_proof = row.cp_proof;
  }
  if (kind.startsWith("note-")) {
    const mint = row.mint;
    args.opening = mint.opening;
    args.cms = mint.cms;
    args.nullifier = mint.nullifier;
    args.face = mint.opening.v;
    if (kind === "note-b1" || kind === "note-a1") {
      args.issuer_sig = mint.issuer_sig;
      args.sigma_R = mint.sigma_R;
      args.sigma_s = mint.sigma_s;
    }
    if (kind === "note-b1") args.eDepForIss = mint.eDepForIss;
    if (kind === "note-a1") { args.eNote = mint.eNote; args.eRec = mint.eRec; }
    if (kind.startsWith("note-a2")) {
      args.eNote = mint.eNote;
      args.eIss = mint.eIss;
      if (kind === "note-a2") args.binding = mint.binding;
    }
    if (kind.startsWith("note-a1") || kind.startsWith("note-a2")) {
      // The addressed legs: the mailbox key, and whichever evidence the
      // generating role could produce.  The recipient holds k; the issuer
      // holds the randomness it encrypted with.  Neither holds the other's.
      const alice = WV.parties[row.payee];
      args.pk_recv = alice.pk_recv;
      args.mailbox_binding = row.mailboxBinding ?? null;
      if (row.role === "recipient") {
        args.k_recv = alice.k_recv;
      } else {
        args.r_note = mint.nonces.r_note;
        args.r_id = kind === "note-a1" ? mint.nonces.r_rec : mint.nonces.r_prime;
      }
    }
  }
  return args;
}

function checkVerify(got, want) {
  assert.equal(got.ok, want.ok);
  assert.equal(got.reason, want.reason);
  if (want.identity_M) {
    assert.equal(BigInt(got.identity_M.x), BigInt(want.identity_M.x));
    assert.equal(BigInt(got.identity_M.y), BigInt(want.identity_M.y));
  }
  if (want.value !== null && want.value !== undefined) {
    assert.equal(BigInt(got.value), BigInt(want.value));
  }
}

test("receipt builds + tier-1 verify (all kinds, both roles)", { skip }, () => {
  for (const row of WV.receipts) {
    const canonical = w.buildReceipt(receiptArgs(row));
    assert.equal(canonical, row.canonical, `${row.kind} ${row.role}`);
    assert.equal(w.receiptId(canonical), row.receipt_id);
    assert.equal(w.envelopeText(canonical), row.envelope);
    checkVerify(w.verifyReceipt(canonical), row.verify);
  }
});

test("tampered receipts reject", { skip }, () => {
  for (const row of WV.tampered) {
    const got = w.verifyReceipt(row.canonical);
    assert.equal(got.ok, false, row.note);
    assert.equal(got.reason, row.verify.reason, row.note);
  }
});

test("unilateral A2 flow", { skip }, () => {
  const u = WV.unilateral_a2;
  const minted = w.mintUnilateralA2({
    sk_iss: u.sk_iss, E_reg: u.E_reg, pk_recv: u.pk_recv,
    v: u.v, rho: u.rho, issuer: u.issuer, chainid: u.chainid,
    predicate: u.predicate,
    nonces: {
      r_prime: u.r_prime, r_note: u.r_note, beta: u.beta, gamma: u.gamma,
      k_r: u.k_r, k_b: u.k_b, k_s: u.k_s, k_g: u.k_g,
    },
  });
  for (const key of ["eNote", "eIss", "M_I", "idHash", "cm", "opening", "binding"]) {
    assert.deepEqual(minted[key], u.minted[key], key);
  }

  const tree = { depth: u.tree.depth, leaves: u.tree.leaves };
  const rcpt = w.makeReceiptA2({
    k_recv: u.k_recv, M_rec: u.M_rec, minted,
    issuer: u.issuer, chainid: u.chainid, tree, t_vd: u.t_vd,
  });
  assert.deepEqual(rcpt.M_I, u.receipt.M_I);
  assert.deepEqual(rcpt.M_rec, u.receipt.M_rec);
  assert.deepEqual(rcpt.pk_recv, u.receipt.pk_recv);
  assert.deepEqual(rcpt.vd, u.receipt.vd);
  assert.equal(rcpt.M_I_member, u.receipt.M_I_member);
  assert.equal(rcpt.M_rec_member, u.receipt.M_rec_member);

  const res = w.verifyReceiptA2({
    receipt: rcpt, pk_iss: WV.parties.bob.pk, E_reg: u.E_reg,
    identity_root: u.tree.root, tree,
  });
  assert.equal(res.valid, u.verify.valid);
  assert.equal(res.reason, u.verify.reason);

  const resBad = w.verifyReceiptA2({
    receipt: rcpt, pk_iss: WV.parties.bob.pk, E_reg: u.E_reg,
    identity_root: u.wrong_root_tree.root,
    tree: { depth: 10, leaves: u.wrong_root_tree.leaves },
  });
  assert.equal(resBad.valid, u.wrong_root_verify.valid);
  assert.equal(resBad.reason, u.wrong_root_verify.reason);
});

test("unilateral A1 flow", { skip }, () => {
  const u = WV.unilateral_a1;
  const tree = { depth: WV.unilateral_a2.tree.depth, leaves: WV.unilateral_a2.tree.leaves };
  const minted = w.mintUnilateralA1({
    M_rec: u.M_rec, pk_recv: u.pk_recv,
    v: u.v, rho: u.rho, m_issuer: u.m_issuer,
    sigma_R: u.sigma_R, sigma_s: u.sigma_s, predicate: u.predicate,
    nonces: { r_prime: u.r_prime, r_note: u.r_note },
  });
  for (const key of ["eNote", "eRec", "idHash", "cm", "opening"]) {
    assert.deepEqual(minted[key], u.minted[key], key);
  }
  const rcpt = w.makeReceiptA1({
    k_recv: u.k_recv, M_rec: u.M_rec, minted, M_iss: u.M_iss,
    issuer: u.issuer, chainid: u.chainid, tree, t_vd: u.t_vd,
  });
  assert.deepEqual(rcpt.M_iss, u.receipt.M_iss);
  assert.deepEqual(rcpt.M_rec, u.receipt.M_rec);
  assert.deepEqual(rcpt.pk_recv, u.receipt.pk_recv);
  assert.deepEqual(rcpt.vd, u.receipt.vd);

  const res = w.verifyReceiptA1({
    receipt: rcpt, identity_root: WV.unilateral_a2.tree.root, tree,
  });
  assert.equal(res.valid, u.verify.valid);
  assert.equal(res.reason, u.verify.reason);
});

test("issuer ceremony", { skip }, () => {
  const i = WV.issuer;
  const cred = w.issueCredential({
    sk_x: i.sk_x, sk_y: i.sk_y, issuer_id: i.issuer_id,
    fields: i.fields, t_sig: i.t_sig,
    applicant_pk: i.applicant_pk, r_delivery: i.r_delivery,
  });
  assert.equal(cred.canonical, i.canonical);
  assert.equal(BigInt(cred.m), BigInt(i.m));
  assert.deepEqual(cred.sigma_1, i.sigma_1);
  assert.deepEqual(cred.sigma_2, i.sigma_2);
  assert.deepEqual(cred.delivery, i.delivery);
});

test("notes: receiving key, delivery, binding, fold witnesses", { skip }, () => {
  const camel = (s) => s.replace(/_([a-z0-9])/g, (_, c) => c.toUpperCase());
  const name = { deposit_fold_a1_witness: "depositFoldA1Witness",
                 deposit_fold_a2_witness: "depositFoldA2Witness" };
  assert.ok(WV.notes.length >= 11, "the notes section lost rows");
  for (const row of WV.notes) {
    const f = name[row.fn] ?? camel(row.fn);
    assert.deepEqual(w[f](row.args), row.want, `${row.fn} diverges from the Python reference`);
  }
});
