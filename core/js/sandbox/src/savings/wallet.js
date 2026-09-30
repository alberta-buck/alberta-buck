// The saver: a key in this browser, acting in the server's world only
// through transactions it signs itself (a --public server executes nothing
// else).  Gas is free in the simulated chain (gas price 0), so the key
// needs no ETH; the world's TOKENs are faucets, so saving starts by minting
// the TOKEN a deposit needs at its market price.  An equity basket pays its
// exits in BUCK, which only a registered identity may receive: before its
// first exit the key asks the server to enroll it (sim_enroll -- a
// credential from the world's issuer and the identity-bound approve of the
// basket, as the world's own depositors hold).
//
// Every action is ONE JSON-RPC batch, answered under one hold of the
// world's chain: for each transaction, a preflight eth_call (the same call
// against the same state, so a refusal comes back with its reason), the
// signed transaction, and its receipt -- named by the transaction's own
// hash, which the signer knows before sending.

import { decodeErrorResult, decodeFunctionResult, encodeFunctionData, keccak256,
         parseEventLogs } from "viem";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

import { BASKET, DIRECTOR, ERC20, POOL, REASONS, RECEIPT } from "./abi.js";

const GAS = 30_000_000n;
/** The deposit's guard: refuse a pool more than this far from its recent
 *  average price (the depositor agents use 100 bp). */
export const MAX_DEVIATION_BP = 200;

/** A refusal for people: the basket's own reason when it gave one. */
export function reasonOf(err, abi = BASKET) {
  const data = err?.data;
  if (typeof data === "string" && data.length >= 10) {
    try {
      const d = decodeErrorResult({ abi, data });
      if (d.errorName === "Error") return String(d.args[0]);
      return REASONS[d.errorName] ?? d.errorName;
    } catch { /* not ours */ }
  }
  return (err?.message ?? String(err)).replace(/^execution reverted:?\s*/, "") || "refused";
}

export class Saver {
  /** link: a SimLink; info: sim_info; key: a hex private key (or none: a
   *  new one). */
  constructor({ link, info, key }) {
    this.link = link;
    this.info = info;
    this.key = key || generatePrivateKey();
    this.account = privateKeyToAccount(this.key);
    this.address = this.account.address;
    this.nonce = null;
    this.enrolled = false;
  }

  /** The basket pays its exits in BUCK (an equity basket). */
  get equity() { return this.info.basket_kind === "equity"; }

  /** Register this key as an identity and approve the basket to pay it
   *  BUCK (the server does both, once). */
  async enroll() {
    if (this.enrolled) return;
    await this.link.call("sim_enroll", [this.address]);
    this.enrolled = true;
  }

  async _nonce() {
    if (this.nonce === null) {
      this.nonce = Number(BigInt(await this.link.call("eth_getTransactionCount",
                                                      [this.address, "latest"])));
    }
    return this.nonce;
  }

  /** Read-only calls in one batch: [{address, abi, functionName, args}] ->
   *  [{result} | {error: reason}]. */
  async read(calls) {
    if (calls.length === 0) return [];
    const replies = await this.link.batch(calls.map((c) => ({
      method: "eth_call",
      params: [{ from: this.address, to: c.address,
                 data: encodeFunctionData({ abi: c.abi, functionName: c.functionName, args: c.args ?? [] }) },
               "latest"],
    })));
    return replies.map((r, i) => {
      if (r.error) return { error: reasonOf(r.error, calls[i].abi) };
      try {
        return { result: decodeFunctionResult({ abi: calls[i].abi, functionName: calls[i].functionName,
                                                data: r.result }) };
      } catch (e) {
        return { error: e.shortMessage ?? e.message };
      }
    });
  }

  /** Sign and send [{to, abi, functionName, args}] in one batch; each comes
   *  back {hash, receipt, ok, reason}.  A refused transaction still spends
   *  its nonce (it is mined, and fails). */
  async send(txs) {
    const chainId = Number(this.info.chain_id);
    for (let attempt = 0; attempt < 2; attempt++) {
      const n0 = await this._nonce();
      const signed = await Promise.all(txs.map(async (t, i) => {
        const data = encodeFunctionData({ abi: t.abi, functionName: t.functionName, args: t.args ?? [] });
        const raw = await this.account.signTransaction({
          type: "legacy", chainId, nonce: n0 + i, gasPrice: 0n, gas: GAS, to: t.to, data, value: 0n,
        });
        return { t, data, raw, hash: keccak256(raw) };
      }));
      const reqs = signed.flatMap((s) => [
        { method: "eth_call", params: [{ from: this.address, to: s.t.to, data: s.data }, "latest"] },
        { method: "eth_sendRawTransaction", params: [s.raw] },
        { method: "eth_getTransactionReceipt", params: [s.hash] },
      ]);
      const out = await this.link.batch(reqs);
      if (out[1]?.error && /nonce/.test(out[1].error.message ?? "")) {
        this.nonce = null;                 // out of step (a new world?): resync once
        continue;
      }
      this.nonce = n0 + txs.length;
      return signed.map((s, i) => {
        const [pre, sent, rcpt] = out.slice(3 * i, 3 * i + 3);
        const receipt = rcpt?.result ?? null;
        const ok = !!receipt && receipt.status === "0x1";
        return { hash: s.hash, receipt, ok,
                 reason: ok ? null : pre?.error ? reasonOf(pre.error, s.t.abi)
                   : sent?.error ? reasonOf(sent.error) : "refused" };
      });
    }
    throw new Error("the world's nonce for this key keeps moving");
  }

  /** Deposit `amount` of TOKEN i: mint what the wallet lacks of it (the
   *  faucet; `held` is what it holds), approve the basket and deposit.
   *  Resolves to the new receipt, or throws with the reason. */
  async save(i, amount, held = 0n) {
    const tok = this.info.tokens[i];
    const short = amount > held ? amount - held : 0n;
    const txs = [
      ...(short > 0n ? [{ what: "the faucet", to: tok.address, abi: ERC20, functionName: "mint",
                          args: [this.address, short] }] : []),
      { what: "the approval", to: tok.address, abi: ERC20, functionName: "approve", args: [this.info.basket, amount] },
      { what: "the deposit", to: this.info.basket, abi: BASKET, functionName: "depositToken",
        args: [tok.address, amount, BigInt(MAX_DEVIATION_BP)] },
    ];
    const out = await this.send(txs);
    out.forEach((r, k) => {
      if (!r.ok) throw Object.assign(new Error(`${txs[k].what} was refused: ${r.reason}`),
        { reason: `${txs[k].what} was refused: ${r.reason}` });
    });
    const deposit = out[out.length - 1];
    const [ev] = parseEventLogs({ abi: BASKET, eventName: "Deposited", logs: deposit.receipt.logs });
    if (!ev) throw new Error("the deposit made no receipt");
    return { id: ev.args.receiptId, buckMinted: ev.args.buckMinted, tokenAmount: ev.args.tokenAmount };
  }

  /** Redeem a whole receipt.  Resolves to what was paid: [{i, amount}]
   *  from the transfers to this key -- i a TOKEN's index, or -1 for BUCK. */
  async redeem(id) {
    if (this.equity) await this.enroll();
    const [r] = await this.send([{ to: this.info.basket, abi: BASKET, functionName: "redeem",
                                   args: [BigInt(id), 10_000n] }]);
    if (!r.ok) throw Object.assign(new Error(`the redemption was refused: ${r.reason}`),
      { reason: `the redemption was refused: ${r.reason}` });
    const me = this.address.toLowerCase();
    const index = new Map(this.info.tokens.map((t, i) => [t.address.toLowerCase(), i]));
    if (this.info.buck) index.set(this.info.buck.toLowerCase(), -1);
    const paid = parseEventLogs({ abi: ERC20, eventName: "Transfer", logs: r.receipt.logs })
      .filter((e) => e.args.to.toLowerCase() === me && index.has(e.address.toLowerCase()))
      .map((e) => ({ i: index.get(e.address.toLowerCase()), amount: e.args.value }));
    return { paid };
  }

  /** What this key holds -- each TOKEN's balance and its BUCK, each
   *  receipt's deposit and owner (and, in an equity basket, its shares and
   *  cost basis) -- with the director's hint (a pro-rata basket's director
   *  gives one) and each TOKEN/BUCK pool's tick now and at its average (the
   *  deposit guard's two readings): one batch. */
  async holdings(ids) {
    const toks = this.info.tokens;
    if (this.twapWindow === undefined) {
      const [w] = await this.read([{ address: this.info.basket, abi: BASKET, functionName: "twapWindow" }]);
      this.twapWindow = w.result ?? 0;
    }
    const calls = [
      ...toks.map((t) => ({ address: t.address, abi: ERC20, functionName: "balanceOf", args: [this.address] })),
      ...toks.flatMap((t) => [
        { address: t.pool_buck, abi: POOL, functionName: "slot0" },
        { address: this.info.basket, abi: BASKET, functionName: "consultTickExternal",
          args: [t.pool_buck, this.twapWindow] },
      ]),
      ...ids.flatMap((id) => [
        { address: this.info.basket, abi: BASKET, functionName: "deposits", args: [BigInt(id)] },
        { address: this.info.receipt, abi: RECEIPT, functionName: "ownerOf", args: [BigInt(id)] },
        ...(this.equity ? [{ address: this.info.basket, abi: BASKET, functionName: "holdings",
                             args: [BigInt(id)] }] : []),
      ]),
      { address: this.info.buck, abi: ERC20, functionName: "balanceOf", args: [this.address] },
    ];
    const hinted = !!this.info.director && !this.equity;
    if (hinted) calls.push({ address: this.info.director, abi: DIRECTOR, functionName: "depositHint" });
    const out = await this.read(calls);
    const balances = out.slice(0, toks.length).map((r) => r.result ?? 0n);
    const ticks = toks.map((_, i) => ({
      tick: out[toks.length + 2 * i].result?.[1] ?? null,
      twap: this.twapWindow ? out[toks.length + 2 * i + 1].result ?? null : null,
    }));
    const base = 3 * toks.length;
    const per = this.equity ? 3 : 2;
    const receipts = ids.map((id, k) => {
      const dep = out[base + per * k];
      const owner = out[base + per * k + 1];
      const hold = this.equity ? out[base + per * k + 2].result : null;
      const d = dep.result;
      return {
        id,
        live: !!d && d[0] > 0n && owner.result?.toLowerCase() === this.address.toLowerCase()
          && (!this.equity || (hold?.[0] ?? 0n) > 0n),
        buckPrincipal: d ? d[0] : 0n, tokenPrincipal: d ? d[1] : 0n,
        token: d ? d[2] : null, depositTime: d ? Number(d[3]) : 0,
        shares: hold ? hold[0] : 0n, basis: hold ? hold[1] : 0n,
      };
    });
    const buck = out[base + per * ids.length].result ?? 0n;
    const hint = hinted ? out[out.length - 1].result : undefined;
    return { balances, buck, receipts, hint, ticks };
  }
}
