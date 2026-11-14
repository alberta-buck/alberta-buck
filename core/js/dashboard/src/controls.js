// The live session's controls: steering (pause, step, pace, stop, reset, export) in one row, and
// the world-changing actions (price and BUCK shocks, the population, a new household, one
// household's settings) in collapsible forms. Every result the session reports is shown beside
// them; nothing here uses a browser dialog.

import { experiments, filled, parseSets, startSession } from "./live.js";

const h = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "on") for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) node.append(typeof c === "string" ? document.createTextNode(c) : c);
  return node;
};
const number = (attrs = {}) => h("input", { type: "number", step: "any", ...attrs });
const select = (options, attrs = {}) => h("select", attrs, ...options.map(([v, label]) => h("option", { value: v }, label)));
const field = (label, input, hint = "") => h("label", { class: "field" }, h("span", {}, label), input,
  hint ? h("small", {}, hint) : "");

// Two clicks for what cannot be undone, without a browser dialog.
function confirmButton(label, run) {
  const b = h("button", { type: "button", class: "ghost" }, label);
  let armed = null;
  b.addEventListener("click", () => {
    if (!armed) {
      b.textContent = `Confirm ${label.toLowerCase()}`;
      armed = setTimeout(() => { b.textContent = label; armed = null; }, 4000);
      return;
    }
    clearTimeout(armed); armed = null; b.textContent = label;
    run();
  });
  return b;
}

export class ControlPanel {
  constructor(root) {
    this.root = root;
    this.control = null;
    this.manifest = null;
    root.hidden = true;
  }

  detach() {
    this.control = null;
    this.root.hidden = true;
    this.root.replaceChildren();
  }

  attach(control, manifest) {
    this.control = control;
    this.manifest = manifest;
    this.root.hidden = false;
    this.state = h("span", { class: "live-state" }, "Connecting");
    this.result = h("p", { class: "live-result", role: "status" });
    const send = (op, label) => this.run(label, () => control.send(op));
    this.pauseButton = h("button", { type: "button", on: { click: () => send({ op: this.paused ? "resume" : "pause" }, this.paused ? "Resume" : "Pause") } }, "Pause");
    const pace = select([["0", "Flat out"], ["1", "1 day/s"], ["2", "2 days/s"], ["5", "5 days/s"], ["10", "10 days/s"]],
      { "aria-label": "Pace" });
    pace.addEventListener("change", () => send({ op: "pace", days_per_second: Number(pace.value) }, "Pace"));
    this.pace = pace;
    const steer = h("div", { class: "live-row" },
      h("strong", {}, "Live session"), this.state, this.pauseButton,
      ...[1, 7, 30].map((n) => h("button", { type: "button", on: { click: () => send({ op: "step", days: n }, `Step ${n}`) } }, `+${n} day${n > 1 ? "s" : ""}`)),
      pace,
      h("a", { class: "button ghost", href: control.exportUrl(), download: "" }, "Export experiment"),
      confirmButton("Reset", () => send({ op: "reset" }, "Reset")),
      confirmButton("Stop", () => send({ op: "stop" }, "Stop")));
    const actions = h("div", { class: "live-actions" },
      this.priceShock(), this.buckShock(), this.population(), this.addHousehold(), this.changeHousehold());
    this.root.replaceChildren(steer, actions, this.result);
  }

  async run(label, fn) {
    try {
      await fn();
      this.result.textContent = `${label}: sent; applied at the next point between days.`;
    } catch (e) {
      this.result.textContent = `${label}: ${e.message}`;
    }
  }

  refresh(status) {
    if (!this.control || !status) return;
    this.paused = status.paused;
    this.pauseButton.textContent = status.paused ? "Resume" : "Pause";
    const pace = status.pace ? `${status.pace} days/s` : "flat out";
    this.state.textContent = status.done ? `Ended${status.error ? `: ${status.error}` : ""}`
      : `Day ${status.day ?? "building"} · ${status.paused ? "paused" : "running"} · ${pace}`
        + (status.queued ? ` · ${status.queued} queued` : "");
    if (document.activeElement !== this.pace) this.pace.value = String(status.pace ?? 0);
    const last = status.last;
    if (last && last.op !== "pace" && this.lastSeen !== status.ops) {
      this.lastSeen = status.ops;
      this.result.textContent = `Day ${last.day}: ${last.op} ${last.ok ? "applied" : "refused"}${last.note ? `: ${last.note}` : ""}`;
    }
    for (const el of this.root.querySelectorAll("button, select, input")) el.disabled = !!status.done;
  }

  form(title, note, fields, submit, label = "Apply") {
    const go = h("button", { type: "submit" }, label);
    const f = h("form", { class: "live-form", on: { submit: (e) => { e.preventDefault(); submit(); } } },
      note ? h("p", { class: "note" }, note) : "", h("div", { class: "fields" }, ...fields), go);
    return h("details", {}, h("summary", {}, title), f);
  }

  priceShock() {
    const tokens = this.manifest?.tokens ?? [];
    const token = select(tokens.map((t) => [t, t]));
    const mult = number({ min: "0", value: "0.8" });
    const until = number({ min: "0", step: "1", placeholder: "permanent" });
    return this.form("Commodity price shock", "Multiplies one commodity's reference price from today (the whale re-pins its USDC pool to it).",
      [field("Commodity", token), field("Multiplier", mult), field("Until day", until, "blank: permanent")],
      () => this.run("Price shock", () => this.control.intervene({ action: "price_shock", token: token.value, mult: Number(mult.value),
        ...(until.value ? { until_day: Number(until.value) } : {}) })));
  }

  buckShock() {
    const cast = this.manifest?.cast ?? {};
    const side = select([["sell", "Sell BUCK"], ["buy", "Buy BUCK"]]);
    const usd = number({ min: "0", value: "1000000" });
    const days = number({ min: "1", step: "1", value: "5" });
    const note = cast.ShockAgent ? "A one-sided BUCK trade spread over the days given, through the shock agent."
      : "This experiment casts no ShockAgent, so the session will refuse it (start one with scenario.agents.ShockAgent=1).";
    return this.form("BUCK supply or demand shock", note,
      [field("Side", side), field("Dollars", usd), field("Over days", days)],
      () => this.run("BUCK shock", () => this.control.send({ op: "shock", side: side.value, usd: Number(usd.value), days: Number(days.value) })));
  }

  population() {
    const f = {
      debtor_rate: number({ min: "0", placeholder: "unchanged" }), saver_rate: number({ min: "0", placeholder: "unchanged" }),
      investor_rate: number({ min: "0", placeholder: "unchanged" }), rate_scale: number({ min: "0", placeholder: "1" }),
      apr_shift: number({ placeholder: "0" }), income_scale: number({ min: "0", placeholder: "1" }),
      days: number({ min: "1", step: "1", placeholder: "for good" }), exit_now: number({ min: "0", max: "1", placeholder: "0" }),
    };
    return this.form("Population and economy", "Arrival rates (a year, at the base pace), mortgage rates, incomes and investor withdrawals, from today. Blank fields are unchanged.",
      [field("Debtor arrivals", f.debtor_rate), field("Saver arrivals", f.saver_rate), field("Investor arrivals", f.investor_rate),
        field("Scale all rates", f.rate_scale), field("Mortgage rate shift", f.apr_shift, "e.g. 0.02"),
        field("Income scale", f.income_scale, "e.g. 0.75"), field("Income for days", f.days),
        field("Investors leaving now", f.exit_now, "share, 0 to 1")],
      () => {
        const iv = filled(f);
        if (iv.days !== undefined && iv.income_scale === undefined) delete iv.days;
        return this.run("Population", () => this.control.intervene({ action: "set_population", ...iv }));
      });
  }

  addHousehold() {
    const role = select([["debtor", "Debtor"], ["saver", "Saver"], ["investor", "Investor"]]);
    const f = {
      home_k: number({ min: "1", placeholder: "drawn" }), ltv: number({ min: "0", max: "1", placeholder: "drawn" }),
      income_frac: number({ min: "0", placeholder: "drawn" }), cash_k: number({ min: "0", placeholder: "drawn" }),
      invest_cash_k: number({ min: "0", placeholder: "drawn" }), utilization: number({ min: "0", max: "1", placeholder: "drawn" }),
      carry_utilization: number({ min: "0", max: "1", placeholder: "population's" }),
      bvib_tolerance: number({ min: "0", max: "0.2", placeholder: "drawn" }),
      surplus_policy: select([["", "population's"], ["adaptive", "adaptive"], ["basket", "basket"], ["retire", "retire"], ["discount", "discount"]]),
    };
    return this.form("Add a household", "Arrives today. Blank settings are drawn as for any arrival; a value pins one (home and cash in $k, ratios 0 to 1).",
      [field("Role", role), field("Home ($k)", f.home_k), field("Loan to value", f.ltv), field("Income / home value", f.income_frac),
        field("Cash ($k)", f.cash_k), field("Investor's cash ($k)", f.invest_cash_k), field("Utilization", f.utilization),
        field("Carry utilization", f.carry_utilization), field("BVIB tolerance", f.bvib_tolerance), field("Surplus policy", f.surplus_policy)],
      () => this.run("Add household", () => this.control.intervene({ action: "add_household", role: role.value, settings: filled(f) })), "Add");
  }

  changeHousehold() {
    const member = select([], { "aria-label": "Household" });
    const keys = ["utilization", "carry_utilization", "theta", "risk", "issue_premium", "retire_disc", "bvib_tolerance",
      "decision_days", "money_months", "max_impact_bp"];
    const f = Object.fromEntries(keys.map((k) => [k, number({ min: "0" })]));
    f.surplus_policy = select([["", "unchanged"], ["adaptive", "adaptive"], ["basket", "basket"], ["retire", "retire"], ["discount", "discount"]]);
    let members = [];
    const show = () => {
      const m = members.find((x) => x.member === member.value);
      for (const k of keys) { f[k].value = ""; f[k].placeholder = m?.settings?.[k] !== undefined ? String(+Number(m.settings[k]).toPrecision(4)) : ""; }
      f.surplus_policy.value = "";
      f.surplus_policy.options[0].textContent = `unchanged (${m?.settings?.surplus_policy ?? "?"})`;
    };
    member.addEventListener("change", show);
    const loadMembers = async () => {
      try {
        members = (await this.control.members()).filter((m) => !m.exited);
        member.replaceChildren(...members.map((m) => h("option", { value: m.member }, `${m.member} (${m.role})`)));
        show();
      } catch (e) { this.result.textContent = `Households: ${e.message}`; }
    };
    const load = h("button", { type: "button", class: "ghost", on: { click: loadMembers } }, "Load households");
    const details = this.form("Change a household's settings", "Decision settings only: its home, mortgage, income and role are fixed at arrival. Placeholders show current values; blank fields are unchanged.",
      [field("Household", member), load, ...keys.map((k) => field(k.replaceAll("_", " "), f[k])), field("Surplus policy", f.surplus_policy)],
      () => this.run("Household settings", () => this.control.intervene({ action: "household_settings", member: member.value, settings: filled(f) })));
    details.addEventListener("toggle", () => { if (details.open && !members.length) loadMembers(); });
    this.changeDetails = details;
    this.pickMember = async (name) => {
      details.open = true;
      if (!members.some((m) => m.member === name)) await loadMembers();
      member.value = name;
      show();
    };
    return details;
  }

  // From the Agents tab: this household, in the settings form.
  async openFor(name) {
    if (!this.control || !this.pickMember) return;
    await this.pickMember(name);
    this.changeDetails.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

// The "New session" form: an experiment, its length, seed and price path, extra settings.
export function newSessionForm(root, onStarted) {
  const exp = select([]);
  const days = number({ min: "1", step: "1", value: "365" });
  const seed = number({ step: "1", placeholder: "experiment's" });
  const prices = select([["", "experiment's"], ["history", "history"], ["revert", "reverting"]]);
  const paused = h("input", { type: "checkbox", checked: true });
  const sets = h("textarea", { rows: "3", placeholder: "agents.HouseholdPopulationAgent.investor_rate=60\nscenario.agents.ShockAgent=1" });
  const status = h("p", { class: "note", role: "status" });
  const go = h("button", { type: "submit" }, "Start");
  const form = h("form", { class: "live-form", on: { submit: async (e) => {
    e.preventDefault();
    go.disabled = true;
    try {
      const body = { experiment: exp.value, days: Number(days.value), paused: paused.checked, sets: parseSets(sets.value) };
      if (seed.value) body.seed = Number(seed.value);
      if (prices.value) body.prices = prices.value;
      const started = await startSession(body);
      status.textContent = `Building ${started.run}: the world takes a few seconds to deploy.`;
      root.querySelector("details").open = false;
      onStarted(started);
    } catch (err) {
      status.textContent = `Not started: ${err.message}`;
    } finally {
      go.disabled = false;
    }
  } } },
  h("div", { class: "fields" }, field("Experiment", exp), field("Days", days), field("Seed", seed), field("Prices", prices),
    h("label", { class: "field check" }, paused, h("span", {}, "Start paused at day 0"))),
  field("Extra settings", sets, "one key=value per line, as --set"), go, status);
  experiments().then((names) => {
    exp.replaceChildren(...names.map((n) => h("option", { value: n }, n)));
    exp.value = names.includes("economy") ? "economy" : names[0];
  }).catch((e) => { status.textContent = `Live sessions are off here: ${e.message}`; go.disabled = true; });
  root.replaceChildren(h("details", { class: "new-session" }, h("summary", {}, "New live session"), form));
}
