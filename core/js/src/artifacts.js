/** Link Foundry creation bytecode using fully qualified source:Library names.
 * The offsets refer to bytes, not hex characters. Reject missing, overlapping,
 * out-of-range or malformed references before a deployment is attempted.
 * This module is browser-safe; it does not load artifacts from the filesystem.
 */
export function linkArtifact(artifact, libraries = {}) {
  const bytecode = artifact.bytecode;
  if (typeof bytecode !== "string" || !bytecode.startsWith("0x") || bytecode.length % 2 !== 0) {
    throw new Error("invalid creation bytecode");
  }
  if (Object.keys(libraries).length && !Object.keys(artifact.linkReferences ?? {}).length) {
    throw new Error("library link-reference metadata unavailable");
  }
  let data = bytecode.slice(2);
  const occupied = [];
  for (const [source, named] of Object.entries(artifact.linkReferences ?? {})) {
    for (const [name, locations] of Object.entries(named)) {
      const qualified = `${source}:${name}`;
      const address = libraries[qualified];
      if (typeof address !== "string" || !/^0x[0-9a-fA-F]{40}$/.test(address) || /^0x0{40}$/.test(address)) {
        throw new Error(`missing or invalid library address: ${qualified}`);
      }
      for (const { start, length } of locations) {
        if (!Number.isSafeInteger(start) || !Number.isSafeInteger(length) || start < 0 || length !== 20
            || 2 * (start + length) > data.length) {
          throw new Error(`invalid library location: ${qualified}`);
        }
        const end = start + length;
        if (occupied.some(([a, b]) => start < b && a < end)) {
          throw new Error(`overlapping library location: ${qualified}`);
        }
        occupied.push([start, end]);
        data = data.slice(0, 2 * start) + address.slice(2).toLowerCase() + data.slice(2 * end);
      }
    }
  }
  if (!/^[0-9a-fA-F]*$/.test(data)) throw new Error("unresolved or invalid creation bytecode");
  return { ...artifact, bytecode: `0x${data}` };
}
