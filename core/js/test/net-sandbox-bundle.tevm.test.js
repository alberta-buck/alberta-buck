// The actual generated browser artifact bundle must preserve linked-bytecode
// metadata. Deploy the same net stack through that browser-safe artifact API.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { tevmSession } from "../src/backends.js";
import { deployNetStack } from "../src/net-world.js";
import { loadArtifact } from "../src/nodefs.js";

const path = new URL("../artifacts/sandbox.mjs", import.meta.url);
const skip = existsSync(path) ? false : "sandbox artifacts not generated; run make nix-venv-net-demurrage-smoke";

test("browser bundle links and deploys the BuckNet stack", { skip }, async () => {
  const { artifact } = await import(path);
  assert.deepEqual(artifact("BuckNet").linkReferences, loadArtifact("BuckNet").linkReferences);
  const s = await tevmSession();
  const gov = s.account.address;
  const deploy = (name, args = []) => s.deploy(artifact(name), args, { name });
  const registry = await deploy("IdentityRegistry", [gov]);
  const credit = await deploy("BuckCredit");
  const controller = await deploy("BuckKControllerDirect", [100_000_000_000n, 643_004n, 0n,
    1800n, 0n, 950_000_000_000_000_000n, 750_000_000_000_000_000n, gov]);
  const stack = await deployNetStack(s, artifact, { registry, credit, controller, governance: gov });
  assert.equal((await s.call(registry, "buck")).toLowerCase(), stack.token.address.toLowerCase());
  assert.equal((await s.call(credit, "buck")).toLowerCase(), stack.token.address.toLowerCase());
  assert.equal((await s.call(stack.token, "jubilee")).toLowerCase(), stack.fund.address.toLowerCase());
  assert.equal((await s.call(stack.quoter, "token")).toLowerCase(), stack.token.address.toLowerCase());
  assert.deepEqual(s.mismatches, []);
});
