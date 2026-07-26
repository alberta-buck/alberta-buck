// Node entry for the buck-wallet + buck-registry kernels: loads the
// nodejs-target wasm package (the same buck_identity blob; built by
// `make nix-core-build-wasm`) and wraps it in the structured API.  The
// wrapper itself lives in wallet-core.js (environment-free); a browser
// loads the web-target package and calls wrapWallet itself.

import { createRequire } from "node:module";

import { wrapWallet } from "./wallet-core.js";

const require = createRequire(import.meta.url);
const api = wrapWallet(require("alberta-buck-kernel/identity"));

export default api;
export const {
  canonicalJson, canonicalIdentityData,
  receiptId, envelopeText, parseEnvelope,
  buildReceipt, verifyReceipt,
  mintUnilateralA2, makeReceiptA2, verifyReceiptA2,
  mintUnilateralA1, makeReceiptA1, verifyReceiptA1,
  issueCredential,
  registry,
} = api;
