// Browser entry for the buck-identity kernel: initializes the WEB-target
// wasm package (built by `make nix-core-build-wasm-web` into
// the alberta-buck-kernel package) and wraps it in the same BigInt-native API node code
// gets from identity.js.
//
// Always pass an explicit wasm source -- a URL string the page serves
// (e.g. "./wasm-web/buck_identity_bg.wasm"), a Response/Promise thereof,
// or a BufferSource -- because bundlers break wasm-bindgen's default
// import.meta.url-relative fetch.

import { wrapIdentity } from "./identity-core.js";

export async function loadIdentity(wasmSource) {
  const mod = await import("alberta-buck-kernel/web/identity");
  await mod.default(
    wasmSource !== undefined ? { module_or_path: wasmSource } : undefined,
  );
  return wrapIdentity(mod);
}
