// ChainSession, JS side: send/call/deploy with declared expectations and
// the shared JSONL journal (alberta-buck-platform.org, Layer 1).
//
// The peer of buck_core.session.Web3Session with identical semantics:
//   * expect: "ok" (default) or "revert".  An unexpected revert THROWS
//     (with the Solidity reason, extracted by replaying the call at the
//     receipt's block); an unexpected success logs a warning.  Either way
//     the outcome is journaled and contradictions accumulate in
//     session.mismatches.
//   * reads (call) are not journaled.
//
// Works over any viem-compatible client -- anvil/testnets by HTTP
// transport, Tevm's in-process MemoryClient in Node or the browser (see
// backends.js).  Contract handles are plain {abi, address} objects.

export const CALL_GAS = 12_000_000n;
// Under tevm's 30M default block gas limit: a tx whose gas limit exceeds
// the block limit is rejected outright ("Tx gaslimit ... exceeds block gas
// limit"), and tevm exposes no way to raise it.  55M happened to work only
// because older viem did not forward the parameter; a fresh install of the
// published package fails on the first deploy.  The largest contract here
// (BuckBasketProRata, ~21.8 KB) deploys for well under 6M, so this is a
// ceiling rather than a constraint.
export const DEPLOY_GAS = 29_000_000n;

export class Session {
  /**
   * @param client viem client with public + wallet actions
   * @param opts.account default viem Account for sends/deploys
   * @param opts.journal a JournalWriter (or null: journaling off)
   * @param opts.txOverrides merged into every tx, e.g. {gasPrice: 0n} on
   *        the zero-fee anvil profile the Python sim uses
   */
  constructor(client, { account, journal = null, txOverrides = {} } = {}) {
    this.client = client;
    this.account = account;
    this.journal = journal;
    this.txOverrides = txOverrides;
    this.mismatches = [];
    this.lastRevertReason = "";
  }

  /** A contract handle: just {abi, address}. */
  contractAt(abi, address) {
    return { abi, address };
  }

  async call(contract, functionName, args = []) {
    return this.client.readContract({
      address: contract.address, abi: contract.abi, functionName, args,
    });
  }

  async send(contract, functionName, args = [],
             { account, gas = CALL_GAS, value = 0n,
               expect = "ok", tag = "" } = {}) {
    const acct = account ?? this.account;
    const hash = await this.client.writeContract({
      address: contract.address, abi: contract.abi, functionName, args,
      account: acct, gas, value, chain: null, ...this.txOverrides,
    });
    const rcpt = await this.client.waitForTransactionReceipt({ hash });
    const ok = rcpt.status === "success";
    const err = ok ? "" : await this.#revertReason(
      contract, functionName, args, acct, gas, value, rcpt.blockNumber);
    this.lastRevertReason = err;
    const entry = this.#journalOp(
      "send", functionName, acct.address, expect, ok, rcpt, err, tag);
    // expect "either": a legitimately-uncertain send (e.g. a funding-gate
    // mint that reverting IS the signal) -- journaled, never a mismatch;
    // the caller reads rcpt.status.
    if (ok && expect === "revert") {
      console.warn(`expected REVERT but ${functionName} succeeded ` +
                   `(tag=${tag} tx=${rcpt.transactionHash})`);
      this.mismatches.push(entry);
    }
    if (!ok && expect === "ok") {
      this.mismatches.push(entry);
      throw new Error(`tx reverted: ${functionName} :: ${err}`);
    }
    return rcpt;
  }

  /** Deploy from an {abi, bytecode} artifact; returns a contract handle. */
  async deploy(artifact, args = [],
               { account, gas = DEPLOY_GAS, name = "", tag = "" } = {}) {
    const acct = account ?? this.account;
    const hash = await this.client.deployContract({
      abi: artifact.abi, bytecode: artifact.bytecode, args,
      account: acct, gas, chain: null, ...this.txOverrides,
    });
    const rcpt = await this.client.waitForTransactionReceipt({ hash });
    const ok = rcpt.status === "success";
    this.#journalOp("deploy", "constructor", acct.address, "ok", ok, rcpt,
                    ok ? "" : "deploy reverted", tag || `deploy:${name}`);
    if (!ok) throw new Error(`deploy ${name} reverted`);
    return this.contractAt(artifact.abi, rcpt.contractAddress);
  }

  async #revertReason(contract, functionName, args, acct, gas, value, block) {
    try {
      await this.client.simulateContract({
        address: contract.address, abi: contract.abi, functionName, args,
        account: acct, gas, value, blockNumber: block,
      });
    } catch (e) {
      // Prefer the decoded Solidity reason: a require string ("SPL") or a
      // custom error name (ERC20InsufficientBalance) over viem's generic
      // "The contract function ... reverted." shortMessage.
      const revert = e.walk?.((c) => c?.name === "ContractFunctionRevertedError");
      const reason = revert?.reason ?? revert?.data?.errorName;
      return String(reason ?? e.shortMessage ?? e.message ?? e).slice(0, 400);
    }
    return "";
  }

  #journalOp(op, fn, sender, expect, ok, rcpt, err, tag) {
    const entry = {
      tag, op, fn, sender,
      expect, outcome: ok ? "ok" : "revert",
      matched: (ok ? "ok" : "revert") === expect,
      gas: Number(rcpt.gasUsed ?? 0n),
      tx: rcpt.transactionHash ?? "",
      block: Number(rcpt.blockNumber ?? 0n),
      err,
    };
    return this.journal ? this.journal.record(entry) : entry;
  }
}
