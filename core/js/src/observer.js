// The chain as the public sees it: every transaction and event since a
// block, decoded against the world's contracts, each field marked as what
// an observer can read ("public") or cannot ("opaque": identity material --
// ciphertexts, hiding presentations, proofs -- with a note on what opening
// it would take).  The privacy argument, made visible: an observer sees
// addresses, amounts and credit terms, and never a name.

import {
  decodeEventLog, decodeFunctionData, getAddress, toEventSelector, toFunctionSelector,
} from "viem";

// Identity material, by its Solidity struct.
const OPAQUE_STRUCT = {
  "struct BN254.G1Point":
    "a public key: a random curve point, tied to no name",
  "struct IdentityRegistry.ElGamalCT":
    "an identity point, encrypted: only the matching secret key opens it, and the point " +
    "names no one without the issuer's records",
  "struct IdentityRegistry.PSPresentation":
    "a re-randomized issuer signature: shows a trusted issuer vouched for this key's " +
    "owner, not who they are",
  "struct IdentityRegistry.RegistrationProof":
    "a zero-knowledge proof: convinces the registry, reveals nothing else",
  "struct IdentityRegistry.CPProof":
    "a zero-knowledge proof that two ciphertexts hold the same identity; reveals " +
    "nothing else",
};

// Where a field's role says more than its type does.
const OPAQUE_FIELD = {
  "IdentityRegistry.register.E":
    "the holder's identity, encrypted to their own key: only the holder can open it",
  "Buck.approve.E_bob":
    "the payer's identity, encrypted to the payee's key: only the payee can open it",
  "Buck.ApproveReceipt.receiptHash":
    "a hash of the handshake's ciphertext: links later payments to it, opens nothing",
  "Buck.BuckTransferReceipt.fromCipherHash":
    "a hash of the payer's identity ciphertext: links the payment to the handshake, " +
    "opens nothing",
  "Buck.BuckTransferReceipt.toCipherHash":
    "a hash of the payee's identity ciphertext: links the payment to the handshake, " +
    "opens nothing",
};

function field(role, where, param, value) {
  const key = `${role}.${where}.${param.name}`;
  const note = OPAQUE_FIELD[key] ?? OPAQUE_STRUCT[param.internalType];
  return {
    name: param.name, type: param.internalType ?? param.type,
    kind: note ? "opaque" : "public", value, ...(note ? { note } : {}),
  };
}

/** The world's contracts by address: {name (artifact), role (contract kind), abi}. */
export function knownContracts(world, extra = {}) {
  const names = world.names ?? { reg: "IdentityRegistry", credit: "BuckCredit" };
  const known = {
    [world.reg.address]: { name: names.reg, role: "IdentityRegistry", abi: world.reg.abi },
    [world.credit.address]: { name: names.credit, role: "BuckCredit", abi: world.credit.abi },
    [world.kctrl.address]: { name: "BuckKControllerDirect", role: "BuckKControllerDirect",
                             abi: world.kctrl.abi },
    [world.buck.address]: { name: "Buck", role: "Buck", abi: world.buck.abi },
    ...extra,
  };
  const out = new Map();
  for (const [addr, c] of Object.entries(known)) {
    const fns = new Map();
    const evs = new Map();
    for (const item of c.abi) {
      if (item.type === "function") fns.set(toFunctionSelector(item), item);
      if (item.type === "event") evs.set(toEventSelector(item), item);
    }
    out.set(addr.toLowerCase(), { ...c, fns, evs });
  }
  return out;
}

function decodeCall(c, tx) {
  const item = c.fns.get(tx.input.slice(0, 10));
  if (!item) return { fn: tx.input.slice(0, 10), args: [] };
  const { args } = decodeFunctionData({ abi: [item], data: tx.input });
  return {
    fn: item.name,
    args: item.inputs.map((p, i) => field(c.role, item.name, p, args[i])),
  };
}

function decodeLog(known, log) {
  const c = known.get(log.address.toLowerCase());
  const item = c?.evs.get(log.topics[0]);
  if (!item) {
    return { contract: c?.name ?? getAddress(log.address), name: log.topics[0] ?? "", args: [] };
  }
  const { args } = decodeEventLog({ abi: [item], data: log.data, topics: log.topics });
  return {
    contract: c.name, name: item.name,
    args: item.inputs.map((p, i) =>
      field(c.role, item.name, p, Array.isArray(args) ? args[i] : args[p.name])),
  };
}

/**
 * Every transaction in blocks [fromBlock, toBlock] (default: to the head),
 * oldest first, as rows {block, timestamp, hash, from, to, contract, fn,
 * status, gasUsed, args, events}.  `to` is null and `fn` is "deploy" for a
 * contract creation (`contract` names what it created, when known); a plain
 * ether transfer has fn "transfer ETH".
 *
 * @param opts.extra more contracts to decode, {address: {name, role, abi}}
 */
export async function observe(world, fromBlock = 0n, opts = {}) {
  const client = world.session.client;
  const known = knownContracts(world, opts.extra);
  const head = opts.toBlock ?? (await client.getBlock()).number;
  const rows = [];
  for (let n = BigInt(fromBlock); n <= head; n++) {
    const block = await client.getBlock({ blockNumber: n, includeTransactions: true });
    for (const tx of block.transactions) {
      const rcpt = await client.getTransactionReceipt({ hash: tx.hash });
      const base = {
        block: block.number, timestamp: block.timestamp, hash: tx.hash,
        from: getAddress(tx.from), to: tx.to ? getAddress(tx.to) : null,
        status: rcpt.status === "success" ? "ok" : "revert", gasUsed: rcpt.gasUsed,
      };
      let call;
      if (!tx.to) {
        const made = rcpt.contractAddress && known.get(rcpt.contractAddress.toLowerCase());
        call = { contract: made?.name ?? (rcpt.contractAddress && getAddress(rcpt.contractAddress)),
                 fn: "deploy", args: [] };
      } else {
        const c = known.get(tx.to.toLowerCase());
        call = c
          ? { contract: c.name, ...decodeCall(c, tx) }
          : { contract: null, fn: tx.input === "0x" ? "transfer ETH" : tx.input.slice(0, 10),
              args: [{ name: "value", type: "uint256", kind: "public", value: tx.value }] };
      }
      rows.push({ ...base, ...call, events: rcpt.logs.map((l) => decodeLog(known, l)) });
    }
  }
  return rows;
}
