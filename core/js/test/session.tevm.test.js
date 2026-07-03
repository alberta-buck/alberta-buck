// Session semantics on the standalone Tevm backend: deploy, sends, both
// expectation outcomes, journal contents.  Mirrors the Python suite's
// StubSession tests, but against a real in-process EVM.
// Skips when the Foundry artifacts (out/) are not built.

import { test } from "node:test";
import assert from "node:assert/strict";

import { JournalWriter, parseJournal, mismatches } from "../src/journal.js";
import { tevmSession, devAccount } from "../src/backends.js";
import { artifactsAvailable, loadArtifact } from "../src/nodefs.js";

const HAVE = artifactsAvailable();
const UNIT = 10n ** 18n;

test("erc20 lifecycle with expectations + journal", { skip: !HAVE }, async () => {
  const lines = [];
  const session = await tevmSession({
    journal: new JournalWriter((l) => lines.push(l)),
  });
  const me = session.account.address;
  const other = devAccount(1);

  const tok = await session.deploy(loadArtifact("MockERC20"),
    ["Test Token", "TOK", 18], { name: "MockERC20", gas: 10_000_000n });
  await session.send(tok, "mint", [me, 100n * UNIT], { tag: "mint" });
  await session.send(tok, "transfer", [other.address, 40n * UNIT],
                     { tag: "xfer" });
  assert.equal(await session.call(tok, "balanceOf", [me]), 60n * UNIT);
  assert.equal(await session.call(tok, "balanceOf", [other.address]),
               40n * UNIT);

  // A DECLARED failure: the recipient overdraws, expect: "revert".
  await session.send(tok, "transfer", [me, 999n * UNIT],
                     { account: other, expect: "revert", tag: "overdraw" });
  assert.equal(session.mismatches.length, 0);
  assert.match(session.lastRevertReason, /insufficient|exceeds|ERC20/i);

  // An UNdeclared failure throws (and is counted).
  await assert.rejects(
    session.send(tok, "transfer", [other.address, 10n ** 30n],
                 { tag: "boom" }),
    /tx reverted: transfer/);
  assert.equal(session.mismatches.length, 1);

  const entries = parseJournal(lines.join(""));
  assert.equal(entries.length, 5);            // deploy, mint, xfer, overdraw, boom
  assert.deepEqual(entries.map((e) => e.op),
                   ["deploy", "send", "send", "send", "send"]);
  assert.deepEqual(mismatches(entries).map((e) => e.tag), ["boom"]);
  const overdraw = entries.find((e) => e.tag === "overdraw");
  assert.equal(overdraw.matched, true);
  assert.equal(overdraw.outcome, "revert");
  assert.ok(overdraw.err.length > 0);
});
