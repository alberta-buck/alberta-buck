// Structured wrapper over the buck-wallet + buck-registry wasm kernel --
// the environment-free CORE.  `wrapWallet(wasm)` takes the raw wasm
// module (the same buck_identity package: one blob carries all three
// kernels) and returns the wallet/registry API:
//
//   receipt cores    canonical JSON text (the serialization IS the object)
//   structured args  plain objects in the vector-fixture shapes --
//                    "0x..." hex words, {x, y} points, {R, C} ciphertexts
//   certificates     Uint8Array wire bytes (the Python-pinned formats)
//   accumulators     the MerkleTree / Aggregator / FeatureAuthority /
//                    Regulator wasm classes; paths and envelopes as JSON
//
// Deterministic: every nonce rides inside the args (`nonces: {...}`),
// exactly the order the Python reference draws them.

export function wrapWallet(wasm) {
  const toText = (v) => (typeof v === "string" ? v : JSON.stringify(v));
  const enc = new TextEncoder();
  const toBytes = (v) => (typeof v === "string" ? enc.encode(v) : v);

  return {
    // ---- canonical dialect + envelope ---------------------------------
    canonicalJson: (v) => wasm.wallet_canonical_json(toText(v)),
    canonicalIdentityData: (v) => wasm.wallet_canonical_identity_data(toText(v)),
    receiptId: (canonical, prefixLen = 12) =>
      wasm.wallet_receipt_id(toBytes(canonical), prefixLen),
    envelopeText: (canonical, width = 64) =>
      wasm.wallet_envelope_text(toBytes(canonical), width),
    parseEnvelope: (text) => wasm.wallet_parse_envelope(text),

    // ---- receipts ------------------------------------------------------
    /** Build any receipt kind from named args; returns canonical text. */
    buildReceipt: (args) => wasm.wallet_build_receipt(toText(args)),
    /** Tier-1 offline verify -> {ok, reason, identity_M, value}. */
    verifyReceipt: (core) => JSON.parse(wasm.wallet_verify_receipt(toText(core))),

    // ---- unilateral identity-targeted Note flows -----------------------
    mintUnilateralA2: (a) => JSON.parse(wasm.wallet_mint_unilateral_a2(toText(a))),
    makeReceiptA2: (a) => JSON.parse(wasm.wallet_make_receipt_a2(toText(a))),
    verifyReceiptA2: (a) => JSON.parse(wasm.wallet_verify_receipt_a2(toText(a))),
    mintUnilateralA1: (a) => JSON.parse(wasm.wallet_mint_unilateral_a1(toText(a))),
    makeReceiptA1: (a) => JSON.parse(wasm.wallet_make_receipt_a1(toText(a))),
    verifyReceiptA1: (a) => JSON.parse(wasm.wallet_verify_receipt_a1(toText(a))),

    // ---- Notes: the mailbox key, the delivery, and the folded gate --------
    // A delivery and a fold witness are DOCUMENTS (decimal words); every other
    // argument and result is hex, as throughout this ABI.
    receivingKey: (a) => JSON.parse(wasm.wallet_receiving_key(toText(a))),
    wrapMask: (a) => JSON.parse(wasm.wallet_wrap_mask(toText(a))),
    deliverA1: (a) => JSON.parse(wasm.wallet_deliver_a1(toText(a))),
    deliverA2: (a) => JSON.parse(wasm.wallet_deliver_a2(toText(a))),
    openA1: (a) => JSON.parse(wasm.wallet_open_a1(toText(a))),
    openA2: (a) => JSON.parse(wasm.wallet_open_a2(toText(a))),
    proveReceivingBinding: (a) => JSON.parse(wasm.wallet_prove_receiving_binding(toText(a))),
    verifyReceivingBinding: (a) => JSON.parse(wasm.wallet_verify_receiving_binding(toText(a))),
    depositFoldA1Witness: (a) => JSON.parse(wasm.wallet_deposit_fold_a1_witness(toText(a))),
    depositFoldA2Witness: (a) => JSON.parse(wasm.wallet_deposit_fold_a2_witness(toText(a))),

    // ---- issuer ceremony -----------------------------------------------
    issueCredential: (a) => JSON.parse(wasm.wallet_issue_credential(toText(a))),

    // ---- registry ------------------------------------------------------
    registry: {
      /** -> [e, s, Rx, Ry] hex words. */
      schnorrSign: (sk, msgHash, registryId, chainid, k) =>
        wasm.registry_schnorr_sign(sk, msgHash, registryId, chainid, k),
      schnorrVerify: (pk, e, s, r, msgHash, registryId, chainid) =>
        wasm.registry_schnorr_verify(
          pk.x, pk.y, e, s, r.x, r.y, msgHash, registryId, chainid),
      /** -> SignedCertificate wire bytes (Python-pinned format). */
      signCertificate: (sk, registryId, canonical, serial, issuedAt, expiresAt, chainid, k) =>
        wasm.registry_sign_certificate(
          sk, registryId, canonical,
          BigInt(serial), BigInt(issuedAt), BigInt(expiresAt), chainid, k),
      verifyCertificate: (wire, chainid) =>
        wasm.registry_verify_certificate(wire, chainid),
      sealCertificate: (wire, clientPk, r) =>
        wasm.registry_seal_certificate(wire, clientPk.x, clientPk.y, r),
      unsealCertificate: (envelope, clientSk) =>
        wasm.registry_unseal_certificate(envelope, clientSk),
      /** Sub-tree + aggregator path pair -> bool. */
      verifyFullProof: (subProof, aggProof) =>
        wasm.registry_verify_full_proof(toText(subProof), toText(aggProof)),
      /** The identity Merkle accumulator (stateful wasm class). */
      MerkleTree: wasm.MerkleTree,
      /** The central sub-root aggregator, its root ring, composed paths and
       *  attribute proofs (stateful wasm class). */
      Aggregator: wasm.Aggregator,
      /** An attribute authority's subtree, private or public (wasm class). */
      FeatureAuthority: wasm.FeatureAuthority,
      /** An insurance regulator's predicate subtrees (wasm class). */
      Regulator: wasm.Regulator,

      // ---- the insurer gate ---------------------------------------------
      /** The scope the eight-argument createCredit declares. */
      GENERAL_SCOPE: "0x" + "0".repeat(64),
      /** BuckCredit's gate: "" if admitted, else its revert reason.  `face`
       *  in BuckCredit units (BigInt, number or decimal string). */
      checkIssuance: (envelope, scope, face, depType, depRate, premiumRate, now) =>
        wasm.registry_check_issuance(
          toText(envelope), scope, String(face), depType, depRate, premiumRate, now),
      bandForFace: (face) => wasm.registry_band_for_face(String(face)),
      scopeId: (name) => wasm.registry_scope_id(name),
      subtreeKey: (name) => wasm.registry_subtree_key(name),
    },
  };
}
