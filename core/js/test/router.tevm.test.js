// Risk-kill gate: the REAL Universal Router deploys and routes on Tevm.
//
// The SimLP-vs-real-AMM question (platform doc, Risks): the pools were
// always real; only the periphery was simulated.  This gate proves the
// Python sim's router recipe (deploy.py: dummy Permit2 + pre-fund route,
// local pool-init-code-hash, WETH9) works on the in-browser EVM too --
// so JS worlds can route agent/user swaps through the real periphery,
// and real JS AMM tooling has a genuine router+pools to talk to.
//
// Skips cleanly when forge artifacts or the vendored UR artifact are
// missing.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

import { tevmSession } from "../src/backends.js";
import { loadArtifact, artifactsAvailable, repoRoot } from "../src/nodefs.js";
import { buildOnePool } from "../src/scenarios/onepool.js";
import { deployUniversalRouter, encodePath, urExecArgs } from "../src/router.js";
import { JournalWriter } from "../src/journal.js";

const urPath = (() => {
  try {
    return join(repoRoot(), "alberta_buck", "sim", "artifacts",
                "UniversalRouter.json");
  } catch { return null; }
})();
const skip = !artifactsAvailable()
  ? "forge artifacts not built (make nix-build)"
  : !(urPath && existsSync(urPath))
    ? "vendored UniversalRouter artifact missing"
    : false;

test("router: the real Universal Router deploys and routes on tevm", { skip }, async () => {
  const lines = [];
  const session = await tevmSession(
    { journal: new JournalWriter((l) => lines.push(l)) });
  const world = await buildOnePool(session, loadArtifact);   // $2.50/TOK
  const { token, usdc, factory } = world;
  const me = session.account.address;

  const weth = await session.deploy(loadArtifact("WETH9"), [],
    { name: "WETH9", gas: 15_000_000n });
  const router = await deployUniversalRouter(
    session, JSON.parse(readFileSync(urPath, "utf8")),
    { weth: weth.address, v3Factory: factory.address,
      poolInitCode: loadArtifact("UniswapV3Pool").bytecode });

  // The deploy must fit tevm's default block gas budget (the risk).
  const dep = lines.map(JSON.parse).find((r) => r.tag === "deploy:UniversalRouter");
  assert.ok(dep && dep.outcome === "ok", "UR deploy journaled ok");
  assert.ok(dep.gas < 15_000_000, `UR deploy gas ${dep.gas} fits tevm`);
  console.log(`UniversalRouter deploy gasUsed on tevm: ${dep.gas}`);

  // Pre-fund route: 5 TOK -> USDC through the router, no SimLP anywhere.
  const amount = 5n * 10n ** 18n;
  await session.send(token, "mint", [me, amount], { tag: "router:stock" });
  await session.send(token, "transfer", [router.address, amount],
                     { tag: "router:fund" });
  const before = await session.call(usdc, "balanceOf", [me]);
  const [commands, inputs] = urExecArgs(
    me, amount, encodePath([token.address, 3000, usdc.address]));
  await session.send(router, "execute", [commands, inputs],
                     { tag: "router:swap", gas: 1_000_000n });
  const proceeds = (await session.call(usdc, "balanceOf", [me])) - before;

  // 5 TOK at $2.50 less the 0.30% fee (and dust of slippage in a $10M
  // pool): ~12.46 USDC.
  assert.ok(proceeds > 12_400_000n && proceeds < 12_500_000n,
    `router swap must deliver ~12.46 USDC, got ${proceeds}`);

  // A multi-hop path through the same encoding surface must also parse:
  // TOK -> USDC is the only pool, so just assert the encoder's shape.
  assert.equal(encodePath([token.address, 3000, usdc.address]).length,
               2 + (20 + 3 + 20) * 2, "packed path is 43 bytes");
});
