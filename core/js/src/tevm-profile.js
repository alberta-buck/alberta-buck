// Where a Tevm world's time goes: every action the session's client is asked for (readContract,
// writeContract, waitForTransactionReceipt, getBlock, request, ...), timed by action and, for
// contract actions and raw calls, by the contract function. Wraps the client's own properties in
// place, so nothing else changes.
//
//   const prof = profileClient(session.client, abis);   // abis: [[name, abi], ...]
//   ... run ...
//   prof.day(d)          // close a day's bucket (wall time and counts since the last)
//   prof.report()        // { methods, functions, days }
//
// viem binds each action to the client in a closure when the client is built, so an action's own
// internal requests are not seen here -- only what the caller asked for, which is the cost the
// caller can change. Under automine a writeContract includes the block Tevm mines for it. For self
// time inside Tevm, use node --cpu-prof.

import { toFunctionSelector } from "viem";

export function selectorNames(abis) {
  const names = new Map();
  for (const [contract, abi] of abis) {
    for (const item of abi ?? []) {
      if (item.type !== "function") continue;
      try {
        const sel = toFunctionSelector(item);
        if (!names.has(sel)) names.set(sel, `${contract}.${item.name}`);
      } catch { /* unnameable */ }
    }
  }
  return names;
}

const SKIP = new Set(["extend", "transport", "chain", "account", "key", "name", "type", "uid",
                      "pollingInterval", "cacheTime", "batch", "ccipRead"]);

export function profileClient(client, abis = []) {
  const names = selectorNames(abis);
  const methods = new Map(), functions = new Map(), days = [];
  let dayStart = performance.now(), dayCounts = new Map();
  const add = (map, key, ms) => {
    const r = map.get(key) ?? { count: 0, ms: 0, max: 0 };
    r.count += 1; r.ms += ms; r.max = Math.max(r.max, ms);
    map.set(key, r);
  };
  const fnName = (method, p) => {
    if (p?.functionName) return p.functionName;
    const q = method === "request" ? p?.params?.[0] : p;
    const data = q?.data ?? q?.input;
    return typeof data === "string" && data.length >= 10 ? (names.get(data.slice(0, 10)) ?? data.slice(0, 10)) : null;
  };
  for (const key of Object.keys(client)) {
    const original = client[key];
    if (SKIP.has(key) || typeof original !== "function") continue;
    client[key] = async (...args) => {
      const t0 = performance.now();
      try {
        return await original(...args);
      } finally {
        const ms = performance.now() - t0;
        const method = key === "request" ? `request ${args[0]?.method ?? "?"}` : key;
        add(methods, method, ms);
        dayCounts.set(method, (dayCounts.get(method) ?? 0) + 1);
        const fn = fnName(key, args[0]);
        if (fn) add(functions, `${method} ${fn}`, ms);
      }
    };
  }
  const table = (map) => [...map.entries()].map(([key, r]) => ({ key, count: r.count, ms: Math.round(r.ms),
    avg: +(r.ms / r.count).toFixed(1), max: Math.round(r.max) })).sort((a, b) => b.ms - a.ms);
  return {
    day(d, extra = {}) {
      const now = performance.now();
      days.push({ day: d, ms: Math.round(now - dayStart), requests: Object.fromEntries(dayCounts), ...extra });
      dayStart = now;
      dayCounts = new Map();
    },
    report() {
      return { methods: table(methods), functions: table(functions), days };
    },
  };
}
