// The Observer: Mallory's view.  Every transaction since the world began,
// decoded exactly as anyone running a node could decode it -- addresses,
// amounts and credit terms in the open; identity material as what it is on
// chain, curve points, ciphertexts and proofs, each with a note on what
// opening it would take.  Names never appear, unless you ask to see what
// only you know.

import { amount, fill, h } from "./dom.js";

const PAGE = 60;

// What kind of transaction a row is, for the filter.
const KINDS = {
  identity: new Set(["register", "approve", "trustIssuer", "bindContract", "authorizeContractBinding",
                     "setBindingAdapter", "createPoolAndBind"]),
  credit: new Set(["createCredit", "setCreditIssuer", "mint", "burn"]),
  payments: new Set(["transfer", "transfer ETH"]),
  market: new Set(["execute", "initialize"]),
};
const kindOf = (r) => {
  if (r.fn === "deploy") return "deploys";
  // The plain ERC-20 approve (to Permit2) carries no identity; the
  // identity-bound approve carries a ciphertext and a proof.
  if (r.fn === "approve" && !r.args.some((f) => f.kind === "opaque")) return "market";
  if (r.contract === "Permit2" || r.contract === "USDC" || r.contract === "SimLP") return "market";
  for (const [k, fns] of Object.entries(KINDS)) if (fns.has(r.fn)) return k;
  return "other";
};

// Amounts with six decimals, by the field names that carry them.
const MONEY = new Set(["amount", "value", "faceValue", "depreciationFloor", "premium", "creditValue",
                       "newLimit", "additionalValue", "totalActivated", "wad"]);
const isMoney = (contract, name) => MONEY.has(name)
  && ["Buck", "USDC", "BuckCredit", "BuckCreditHarness"].includes(contract);

// Approvals of "everything": uint256 and Permit2's uint160 maxima.
const UNLIMITED = new Set([(1n << 256n) - 1n, (1n << 160n) - 1n]);

function show(v, depth = 0) {
  if (typeof v === "bigint") return UNLIMITED.has(v) ? "unlimited" : v.toLocaleString("en-US");
  if (typeof v === "string") return v;
  if (typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return `[${v.map((x) => show(x, depth + 1)).join(", ")}]`;
  if (v && typeof v === "object") {
    if (depth > 1) return "{…}";
    return `{${Object.entries(v).map(([k, x]) => `${k}: ${show(x, depth + 1)}`).join(", ")}}`;
  }
  return String(v);
}

// The first number inside a struct, as a short hex: what an observer
// actually sees of a curve point or a proof.
function glimpse(v) {
  const walk = (x) => {
    if (typeof x === "bigint") return x;
    if (Array.isArray(x)) for (const y of x) { const r = walk(y); if (r !== undefined) return r; }
    if (x && typeof x === "object") for (const y of Object.values(x)) { const r = walk(y); if (r !== undefined) return r; }
    return undefined;
  };
  const n = walk(v);
  if (n === undefined) return show(v).slice(0, 18);
  const hex = n.toString(16).padStart(64, "0");
  return `0x${hex.slice(0, 8)}…${hex.slice(-6)}`;
}

function fieldRow(ctx, contract, f) {
  const name = h("span", { class: "f-name" }, f.name || "(unnamed)");
  if (f.kind === "opaque") {
    return h("li", { class: "opaque" }, name, " ",
      h("span", { class: "chip opaque-chip", title: f.type }, "opaque"), " ",
      h("code", {}, glimpse(f.value)),
      h("div", { class: "f-note" }, f.note));
  }
  let val;
  if (typeof f.value === "string" && /^0x[0-9a-fA-F]{40}$/.test(f.value)) {
    val = ctx.address(f.value);
  } else if (typeof f.value === "bigint" && isMoney(contract, f.name) && !UNLIMITED.has(f.value)) {
    val = h("span", { class: "num", title: `${f.value} base units` }, amount(f.value, 6));
  } else {
    const s = show(f.value);
    val = h("code", { title: s.length > 80 ? s : undefined }, s.length > 80 ? `${s.slice(0, 77)}…` : s);
  }
  return h("li", {}, name, " ", val);
}

export function mountObserver(ctx) {
  const panel = document.getElementById("panel-observer");
  const kind = h("select", { "aria-label": "show" },
    [["all", "Everything"], ["identity", "Identity"], ["payments", "Payments"], ["credit", "Credit"],
     ["market", "Market"], ["deploys", "Deployments"]].map(([v, t]) => h("option", { value: v }, t)));
  const knows = h("input", { type: "checkbox", id: "observer-knows" });
  const list = h("ol", { class: "txs", reversed: true });
  const more = h("button", { type: "button", hidden: true }, "Show older");
  ctx.observer = { kind, knows, list, more, shown: PAGE };
  const rerender = () => ctx.lastView && renderObserver(ctx, ctx.lastView);
  kind.addEventListener("change", () => { ctx.observer.shown = PAGE; rerender(); });
  knows.addEventListener("change", rerender);
  more.addEventListener("click", () => { ctx.observer.shown += PAGE; rerender(); });
  fill(panel,
    h("p", { class: "intro" }, "This is the chain as anyone sees it — Mallory included.  Every ",
      "transaction is public: who sent it, to which contract, with what arguments, and what it ",
      "emitted.  Addresses, amounts and credit terms are open.  Identity is not: it appears only as ",
      "keys, ciphertexts and proofs, and no name, birth date or person number is anywhere below."),
    h("div", { class: "row toolbar" },
      h("label", { class: "field" }, h("span", { class: "field-label" }, "Show"), kind),
      h("label", { class: "check" }, knows,
        " Show what only you know (your own labels for addresses — Mallory has none)")),
    list, more);
}

export function renderObserver(ctx, view) {
  const o = ctx.observer;
  // Keyed in lower case: contracts deployed on Tevm report lower-case
  // addresses, decoded arguments checksummed ones.
  const labels = Object.fromEntries(Object.entries(o.knows.checked ? ctx.app.labels() : {})
    .map(([k, v]) => [k.toLowerCase(), v]));
  ctx.address = (a) => {
    const known = labels[a.toLowerCase()];
    return h("span", { class: "who" }, h("code", { title: a }, `${a.slice(0, 6)}…${a.slice(-4)}`),
      known ? h("span", { class: "known" }, ` ${known}`) : null);
  };
  const rows = view.observed.filter((r) => o.kind.value === "all" || kindOf(r) === o.kind.value);
  const newest = rows.slice(-o.shown).reverse();
  o.more.hidden = rows.length <= o.shown;
  fill(o.list, newest.length === 0 ? h("li", { class: "empty" }, "Nothing of that kind yet.")
    : newest.map((r) => h("li", { class: `tx ${r.status}` },
      h("div", { class: "tx-head" },
        h("span", { class: "chip" }, `#${r.block}`),
        h("span", { class: "tx-time" }, new Date(Number(r.timestamp) * 1000).toISOString().slice(0, 16)
          .replace("T", " ")),
        ctx.address(r.from), " → ",
        h("b", {}, r.contract ?? (r.to ? "" : "(new contract)")), r.to && !r.contract ? ctx.address(r.to) : null,
        h("span", { class: "fn" }, r.fn === "deploy" ? " created" : `.${r.fn}`),
        r.status === "ok" ? null : h("span", { class: "chip bad" }, "refused"),
        h("span", { class: "gas" }, `${Number(r.gasUsed).toLocaleString()} gas`)),
      r.args.length ? h("ul", { class: "fields" }, r.args.map((f) => fieldRow(ctx, r.contract, f))) : null,
      r.events.length ? h("div", { class: "events" }, r.events.map((e) => h("div", { class: "event" },
        h("span", { class: "ev-name" }, `${e.contract}.${e.name}`),
        e.args.length ? h("ul", { class: "fields" }, e.args.map((f) => fieldRow(ctx, e.contract, f))) : null)))
        : null)));
}
