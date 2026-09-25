// JSON for values that hold BigInts: the one codec every persisted and
// exported sandbox value goes through (world records, credential cards,
// wallet handles, chain snapshots).
//
// A BigInt becomes {"$bigint": "<decimal>"} and a Uint8Array becomes
// {"$bytes": "0x<hex>"}; decode reverses exactly that, so a string that
// merely looks numeric stays a string.

const BIG = "$bigint";
const BYTES = "$bytes";

const hexOf = (u8) => "0x" + Array.from(u8, (b) => b.toString(16).padStart(2, "0")).join("");

function bytesOf(hex) {
  const h = hex.slice(2);
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.slice(2 * i, 2 * i + 2), 16);
  return out;
}

/** The JSON-safe image of `value` (a tree of plain objects and arrays). */
export function toPlain(value) {
  if (typeof value === "bigint") return { [BIG]: value.toString(10) };
  if (value instanceof Uint8Array) return { [BYTES]: hexOf(value) };
  if (Array.isArray(value)) return value.map(toPlain);
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      if (v !== undefined && typeof v !== "function") out[k] = toPlain(v);
    }
    return out;
  }
  return value;
}

/** The inverse of toPlain. */
export function fromPlain(value) {
  if (Array.isArray(value)) return value.map(fromPlain);
  if (value && typeof value === "object") {
    const keys = Object.keys(value);
    if (keys.length === 1 && keys[0] === BIG && typeof value[BIG] === "string") {
      return BigInt(value[BIG]);
    }
    if (keys.length === 1 && keys[0] === BYTES && typeof value[BYTES] === "string") {
      return bytesOf(value[BYTES]);
    }
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = fromPlain(v);
    return out;
  }
  return value;
}

/** JSON text for a value holding BigInts. */
export const encodeJSON = (value, space) => JSON.stringify(toPlain(value), null, space);

/** The value encodeJSON wrote. */
export const decodeJSON = (text) => fromPlain(JSON.parse(text));
