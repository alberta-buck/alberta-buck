// Browser entry for the buck-wallet + buck-registry kernels: initializes the
// WEB-target wasm package (built by `make nix-core-build-wasm-web` into the
// alberta-buck-kernel package) and wraps it in the same structured API node
// code gets from wallet.js.
//
// The wallet, registry and identity kernels are one wasm module: loading
// this after loadIdentity (identity-web.js), or before it, initializes the
// module once and the two share it.  Pass the same explicit wasm source
// loadIdentity takes -- bundlers break wasm-bindgen's default
// import.meta.url-relative fetch.

import { wrapWallet } from "./wallet-core.js";

export async function loadWallet(wasmSource) {
  const mod = await import("alberta-buck-kernel/web/identity");
  await mod.default(
    wasmSource !== undefined ? { module_or_path: wasmSource } : undefined,
  );
  return wrapWallet(mod);
}
