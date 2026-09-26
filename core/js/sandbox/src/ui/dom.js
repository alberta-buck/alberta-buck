// Small DOM helpers for the sandbox's tools: elements built with the DOM API
// (never innerHTML, so a name typed into a form is only ever text), amounts
// in and out, and re-rendering a panel without losing what someone is typing.

import { parseUnits } from "viem";

/** h("button", {class: "x", onclick}, "text", child, ...) -> Element. */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs ?? {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  for (const c of children.flat()) {
    if (c === undefined || c === null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** Replace a container's children. */
export function fill(container, ...children) {
  container.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

const group = (s) => s.replace(/\B(?=(\d{3})+(?!\d))/g, ",");

/** A base-unit amount as text, grouped: `places` shown (six, the sandbox's
 *  standard) of its `decimals` (6: BUCK, USDC; 18: ETH). */
export function amount(v, places = 6, decimals = 6) {
  const neg = v < 0n;
  const a = neg ? -v : v;
  const one = 10n ** BigInt(decimals);
  const whole = group((a / one).toString());
  const frac = (a % one).toString().padStart(decimals, "0").slice(0, places);
  return `${neg ? "-" : ""}${whole}${places ? "." + frac : ""}`;
}

// A BUCK is counted like a dollar: one BUCK, two BUCKs.  (USDC is a ticker.)
const unitOf = (v, unit) => (unit === "BUCK" && v !== 1_000_000n && v !== -1_000_000n ? "BUCKs" : unit);

/** An amount element, to six places: the number right-justified, then its
 *  currency in a fixed-width column, so a column of them lines up on the
 *  decimal point.  ETH (18 decimals) shows all of them on hover. */
export function money(v, unit, decimals = 6) {
  const u = unitOf(v, unit);
  return h("span", { class: "money", title: `${amount(v, decimals, decimals)} ${u}` },
    h("span", { class: "num" }, amount(v, 6, decimals)), h("span", { class: "unit" }, u));
}

// An error carrying a `reason` for people, as SandboxError does.
const refusal = (reason) => Object.assign(new Error(reason), { reason });

/** A typed amount -> base units of `decimals` (6: BUCK, USDC; 18: ETH), or a
 *  refusal with a reason.  `zero` admits nothing at all (a floor may be 0). */
export function parseAmount(text, what = "amount", { decimals = 6, zero = false } = {}) {
  const t = String(text ?? "").trim().replace(/,/g, "");
  if (!new RegExp(`^\\d+(\\.\\d{1,${decimals}})?$`).test(t)) {
    throw refusal(`${what}: a number, at most ${decimals === 6 ? "six" : decimals} decimals`);
  }
  const v = parseUnits(t, decimals);
  if (zero ? v < 0n : v <= 0n) throw refusal(`${what} must be positive`);
  return v;
}

/** An address as a dropdown shows it: 0xab2c9d. */
export const shortAddr = (a) => a.slice(0, 8).toLowerCase();

/** An address, shortened, full on hover, copied on click. */
export function addr(a, label) {
  const short = `${a.slice(0, 6)}…${a.slice(-4)}`;
  return h("button", {
    class: "addr", type: "button", title: `${a} (click to copy)`,
    "aria-label": `copy address ${a}`,
    onclick: () => navigator.clipboard?.writeText(a).catch(() => {}),
  }, label ? `${label} ` : "", h("code", {}, short));
}

/** A labelled form field. */
export function field(label, input, hint) {
  return h("label", { class: "field" }, h("span", { class: "field-label" }, label), input,
    hint ? h("span", { class: "hint" }, hint) : null);
}

/** Re-render `container` with `fn`, keeping every [data-key] input's value
 *  and the focused one (a refresh must not eat what someone is typing). */
export function preserving(container, fn) {
  const kept = new Map();
  for (const el of container.querySelectorAll("[data-key]")) kept.set(el.dataset.key, el.value);
  const focused = document.activeElement?.dataset?.key;
  fn();
  for (const el of container.querySelectorAll("[data-key]")) {
    if (!kept.has(el.dataset.key)) continue;
    const v = kept.get(el.dataset.key);
    if (el.tagName === "SELECT" && ![...el.options].some((o) => o.value === v)) continue;
    el.value = v;
  }
  if (focused) container.querySelector(`[data-key="${CSS.escape(focused)}"]`)?.focus();
}

/** Save text as a file download. */
export function download(name, text, type = "application/json") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = h("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** localStorage, never throwing (private windows, blocked site data). */
export const prefs = {
  get(k, dflt) {
    try { return globalThis.localStorage?.getItem(`sandbox:${k}`) ?? dflt; } catch { return dflt; }
  },
  set(k, v) {
    try { globalThis.localStorage?.setItem(`sandbox:${k}`, v); } catch { /* not kept */ }
  },
};
