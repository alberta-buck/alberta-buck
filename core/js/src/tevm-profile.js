// Where a Tevm world's time goes: every JSON-RPC request the session's client makes, timed by
// method and, for calls and sends, by the contract function its 4-byte selector names. Wraps
// client.request in place (viem's actions call it at call time), so nothing else changes.
//
//   const prof = profileClient(session.client, abis);   // abis: [[name, abi], ...]
//   ... run ...
//   prof.day(d)          // close a day's bucket (wall time and counts since the last)
//   prof.report()        // { methods, functions, days }
//
// Timings nest: a send's eth_sendTransaction includes the mining Tevm does under automine, and
// viem's waitForTransactionReceipt adds its own requests. Self time is not separated here; use
// node --cpu-prof for that.

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

export function profileClient(client, abis = []) {
  const names = selectorNames(abis);
  const methods = new Map(), functions = new Map(), days = [];
  let dayStart = performance.now(), dayCounts = new Map();
  const add = (map, key, ms) => {
    const r = map.get(key) ?? { count: 0, ms: 0, max: 0 };
    r.count += 1; r.ms += ms; r.max = Math.max(r.max, ms);
    map.set(key, r);
  };
  const original = client.request.bind(client);
  client.request = async (args, options) => {
    const t0 = performance.now();
    try {
      return await original(args, options);
    } finally {
      const ms = performance.now() - t0;
      const method = args?.method ?? "?";
      add(methods, method, ms);
      dayCounts.set(method, (dayCounts.get(method) ?? 0) + 1);
      const p = args?.params?.[0];
      const data = p?.data ?? p?.input;
      if ((method === "eth_call" || method === "eth_sendTransaction" || method === "eth_estimateGas")
          && typeof data === "string" && data.length >= 10) {
        const sel = data.slice(0, 10);
        add(functions, `${method === "eth_call" ? "call" : method === "eth_estimateGas" ? "estimate" : "send"} ${names.get(sel) ?? sel}`, ms);
      }
    }
  };
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
