// Node entry for the buck-identity kernel: loads the nodejs-target wasm
// package (built by `make nix-core-build-wasm`) and wraps it in the
// BigInt-native API.  The wrapper itself lives in identity-core.js
// (environment-free); the browser loads the web-target package through
// identity-web.js instead.

import { createRequire } from "node:module";

import { wrapIdentity } from "./identity-core.js";

const require = createRequire(import.meta.url);
const api = wrapIdentity(require("alberta-buck-kernel/identity"));

export default api;
export const {
  hex, big, canonicalIdentity, randScalar,
  ORDER, F_R, FIELD_MODULUS, G1, G2, H_POINT,
  FLAVOR_A1, FLAVOR_A2, FLAVOR_B1,
  g1Add, g1Mul, g1Neg, g2Mul, pairingCheck,
  keccakScalar, identityScalar, reduceModOrder, poseidon,
  elgamalEncrypt, elgamalDecrypt,
  psSign, psVerify, psRerandomize, psPresent, psKeyConsistent,
  batchCommitment, issuerSchnorrSign, issuerSchnorrVerify,
  registrationProve, registrationVerify,
  chaumPedersenProve, chaumPedersenVerify,
  verifiableDecryptProve, verifiableDecryptVerify,
  issuerReencProve, issuerReencVerify,
  b1BindProve, b1BindVerify,
  noteCommitment, nullifierB, nullifierA,
  idHashB1, idHashA1, idHashA2, identityLeaf,
} = api;
