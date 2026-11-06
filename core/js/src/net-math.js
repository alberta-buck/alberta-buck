// The same integer kernel for Node and browsers. Inject the initialized math
// WASM module; no platform loader or fallback arithmetic. Only bounded bytes
// use Number; monetary values never narrow from BigInt.
const MAX = (1n << 256n) - 1n;

function uint256(value) {
  if (typeof value !== "bigint") throw new TypeError("uint256 requires BigInt");
  if (value < 0n || value > MAX) throw new RangeError("value outside uint256");
  const bytes = new Uint8Array(32);
  for (let i = 31; i >= 0; i--, value >>= 8n) bytes[i] = Number(value & 255n);
  return bytes;
}

function integer(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length !== 32) throw new Error("invalid uint256 kernel result");
  return bytes.reduce((value, byte) => (value << 8n) | BigInt(byte), 0n);
}

export function createNetMath(kernel) {
  for (const name of ["net_decay", "net_fund_after", "net_target_relief", "mul_div_256"]) {
    if (typeof kernel?.[name] !== "function") throw new Error(`math kernel missing ${name}; rebuild the kernel`);
  }
  return Object.freeze({
    scale: 10n ** 36n,
    decay: (amount, elapsed) => integer(kernel.net_decay(uint256(amount), uint256(elapsed))),
    fundNetAfter: (issuance, fund, elapsed) => integer(kernel.net_fund_after(uint256(issuance), uint256(fund), uint256(elapsed))),
    targetRelief: (principal, buckSeconds, reduction) => integer(kernel.net_target_relief(uint256(principal), uint256(buckSeconds), uint256(reduction))),
    mulDiv(a, b, denominator, roundUp = false) {
      if (typeof roundUp !== "boolean") throw new TypeError("roundUp requires boolean");
      return integer(kernel.mul_div_256(uint256(a), uint256(b), uint256(denominator), roundUp));
    },
  });
}
