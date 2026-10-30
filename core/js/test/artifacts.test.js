import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { linkArtifact } from "../src/artifacts.js";

const vector = JSON.parse(readFileSync(new URL("../../../test/fixtures/library-linking.json", import.meta.url)));
const artifact = () => ({ abi: [], bytecode: vector.bytecode, linkReferences: structuredClone(vector.references) });

test("links the same fully qualified library vector as Python", () => {
  assert.equal(linkArtifact(artifact(), vector.libraries).bytecode, vector.linked);
});

test("rejects missing addresses, invalid offsets and unresolved placeholders", () => {
  assert.throws(() => linkArtifact(artifact(), {}), /missing or invalid library/);
  assert.throws(() => linkArtifact(artifact(), { ...vector.libraries, "src/First.sol:First": `0x${"0".repeat(40)}` }),
    /missing or invalid library/);
  const overlap = artifact();
  overlap.linkReferences["src/Second.sol"].Second[0].start = 10;
  assert.throws(() => linkArtifact(overlap, vector.libraries), /overlapping/);
  const outside = artifact();
  outside.linkReferences["src/Second.sol"].Second[0].start = 1000;
  assert.throws(() => linkArtifact(outside, vector.libraries), /invalid library location/);
  assert.throws(() => linkArtifact({ bytecode: vector.bytecode }, vector.libraries), /metadata unavailable/);
  assert.throws(() => linkArtifact({ bytecode: vector.bytecode }, {}), /unresolved/);
});
