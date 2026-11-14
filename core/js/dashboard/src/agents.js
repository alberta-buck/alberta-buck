// The Agents tab: every household's latest state in one table (sortable, filtered by role or
// name), and one household's drill-down: its books over time and every step of its state machine,
// newest first. In a live session, a household can be opened in the settings form from here.

import { TimeChart } from "./chart.js";
import { format, windowFrames } from "./series.js";

const RAW = 1e6;
const h = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "on") for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) node.append(typeof c === "string" ? document.createTextNode(c) : c);
  return node;
};
const money = (raw) => (raw === null || raw === undefined ? null : Number(raw) / RAW);

// The table's columns: what each household is, owns and owes, against its counterfactual.
export const COLUMNS = [
  { key: "member", label: "Household", value: (r) => r.member, text: true },
  { key: "role", label: "Role", value: (r) => r.role ?? "", text: true },
  { key: "stage", label: "Stage", value: (r) => r.stage ?? "", text: true },
  { key: "age", label: "Age", value: (r) => (r.age === undefined || r.age === null ? null : Math.floor(r.age)), unit: "count" },
  { key: "day", label: "Day", value: (r) => r.day, unit: "count" },
  { key: "vs", label: "Net worth vs counterfactual", value: (r) => (r.nw === undefined || r.hypo === undefined ? null : money(r.nw - r.hypo)), unit: "usd" },
  { key: "drawn", label: "Drawn", value: (r) => money(r.drawn), unit: "buck" },
  { key: "basket", label: "Basket", value: (r) => money(r.basket), unit: "buck" },
  { key: "held", label: "Held BUCK", value: (r) => money(r.held_buck), unit: "buck" },
  { key: "cash", label: "Cash", value: (r) => money(r.cash), unit: "usd" },
  { key: "action", label: "Last action", value: (r) => r.action ?? "", text: true },
  { key: "state", label: "State", value: (r) => (r.exited ? "exited" : r.error ? `refused: ${r.error}` : r.relief_unavailable ? "relief unmarked" : "active"), text: true },
];

// One household's charts, over its own lines.
export const MEMBER = [
  {
    id: "m-nw", title: "Net worth and its counterfactual", unit: "usd", kind: "line",
    note: "Its net worth in dollars, and what it would be had it never used BUCK (the same income, mortgage and choices in dollars).",
    series: [
      { key: "nw", label: "Net worth", value: (r) => money(r.nw) },
      { key: "hypo", label: "Counterfactual", value: (r) => money(r.hypo) },
    ],
  },
  {
    id: "m-credit", title: "Credit and holdings", unit: "buck", kind: "line",
    note: "BUCK drawn against its insured assets, its limit at today's K, and what it holds in the basket and as BUCK.",
    series: [
      { key: "drawn", label: "Drawn", value: (r) => money(r.drawn) },
      { key: "limit", label: "Limit", value: (r) => money(r.limit) },
      { key: "basket", label: "Basket", value: (r) => money(r.basket) },
      { key: "held", label: "Held BUCK", value: (r) => money(r.held_buck) },
    ],
  },
  {
    id: "m-savings", title: "Savings: the basket and the USD fund", unit: "usd", kind: "line",
    note: "Its basket savings at their mark, and its external USD fund (off chain, about 8% a year) with what that fund has paid out as retirement income. The fund lines are empty for a household outside the lifecycle.",
    series: [
      { key: "basket_mark", label: "Basket (mark)", value: (r) => money(r.basket_mark) },
      { key: "usd_fund", label: "USD fund", value: (r) => money(r.usd_fund) },
      { key: "usd_fund_paid", label: "Fund paid out", value: (r) => money(r.usd_fund_paid) },
    ],
  },
  {
    id: "m-cash", title: "Cash and mortgage", unit: "usd", kind: "line",
    note: "Its dollars, and what remains of its dollar mortgage.",
    series: [
      { key: "cash", label: "Cash", value: (r) => money(r.cash) },
      { key: "mortgage", label: "Mortgage", value: (r) => money(r.mortgage) },
    ],
  },
];

// An event's own details, without the bookkeeping every event carries.
const PLAIN = new Set(["schema", "seq", "member", "decision", "day", "block", "phase", "status", "accounts", "agent", "kind"]);
export function eventDetail(e) {
  const rest = Object.fromEntries(Object.entries(e).filter(([k]) => !PLAIN.has(k)));
  return JSON.stringify(rest);
}

export class AgentsTab {
  constructor(root, bus, panel) {
    this.root = root;
    this.bus = bus;
    this.panel = panel;
    this.rows = [];
    this.sort = { key: "vs", dir: 1 };
    this.selected = null;
    this.role = h("select", { "aria-label": "Role" }, ...[["", "All roles"], ["debtor", "Debtors"], ["saver", "Savers"], ["investor", "Investors"]]
      .map(([v, l]) => h("option", { value: v }, l)));
    this.search = h("input", { type: "search", placeholder: "Household name", "aria-label": "Find a household" });
    for (const el of [this.role, this.search]) el.addEventListener("input", () => this.renderTable());
    this.count = h("span", { class: "note" });
    this.table = h("div", { class: "table-wrap agents-table" });
    this.drill = h("section", { class: "drill", "aria-label": "Household detail", hidden: true });
    root.replaceChildren(h("div", { class: "filters" }, this.role, this.search,
      h("button", { type: "button", class: "ghost", on: { click: () => this.refresh() } }, "Refresh"), this.count), this.table, this.drill);
  }

  async show({ source, range }) {
    this.source = source;
    this.range = range;
    await this.refresh();
  }

  async refresh() {
    if (!this.source) return;
    try {
      this.rows = await this.source.agents();
      this.renderTable();
      if (this.selected) await this.open(this.selected, false);
    } catch (e) {
      this.table.replaceChildren(h("p", { class: "note" }, `Households not available: ${e.message}`));
    }
  }

  renderTable() {
    const role = this.role.value, q = this.search.value.trim().toLowerCase();
    const col = COLUMNS.find((c) => c.key === this.sort.key) ?? COLUMNS[0];
    const rows = this.rows.filter((r) => (!role || r.role === role) && (!q || r.member.toLowerCase().includes(q)))
      .sort((a, b) => {
        const x = col.value(a), y = col.value(b);
        if (x === y) return a.member < b.member ? -1 : 1;
        if (x === null || x === undefined) return 1;
        if (y === null || y === undefined) return -1;
        return (x < y ? -1 : 1) * this.sort.dir;
      });
    this.count.textContent = `${rows.length} of ${this.rows.length} households`;
    const head = h("tr", {}, ...COLUMNS.map((c) => h("th", { scope: "col", "aria-sort": c.key === this.sort.key ? (this.sort.dir > 0 ? "ascending" : "descending") : null },
      h("button", { type: "button", class: "sort", on: { click: () => {
        this.sort = { key: c.key, dir: this.sort.key === c.key ? -this.sort.dir : 1 };
        this.renderTable();
      } } }, c.label))));
    const body = h("tbody", {}, ...rows.map((r) => h("tr", {
      class: r.member === this.selected ? "selected" : null, tabindex: "0",
      on: { click: () => this.open(r.member), keydown: (e) => { if (e.key === "Enter") this.open(r.member); } },
    }, ...COLUMNS.map((c) => h("td", {}, c.text ? String(c.value(r)) : format(c.value(r), c.unit))))));
    this.table.replaceChildren(h("table", {}, h("thead", {}, head), body));
  }

  async open(member, scroll = true) {
    this.selected = member;
    this.renderTable();
    const [lines, events] = await Promise.all([this.source.agents(member), this.source.events(member)]);
    const role = lines.at(-1)?.role ?? "";
    const head = h("div", { class: "card-head" }, h("h3", {}, `${member} (${role})`));
    if (this.panel?.control) {
      head.append(h("button", { type: "button", class: "ghost", on: { click: () => this.panel.openFor(member) } }, "Change its settings"));
    }
    const grid = h("div", { class: "charts" });
    this.drill.replaceChildren(head, grid, this.timeline(events));
    this.drill.hidden = false;
    const shown = windowFrames(lines, this.range ?? "all");
    for (const spec of MEMBER) {
      const card = h("section", { class: "card", "aria-label": spec.title });
      grid.append(card);
      new TimeChart(card, spec, this.bus).update(shown, []);
    }
    if (scroll) this.drill.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  timeline(events) {
    const LIMIT = 300;
    const newest = events.slice().reverse().slice(0, LIMIT);
    const body = h("tbody", {}, ...newest.map((e) => h("tr", { class: e.kind === "act" ? "fault" : null },
      h("td", {}, String(e.day)), h("td", {}, String(e.seq ?? "")), h("td", {}, e.kind ?? ""), h("td", {}, e.phase ?? ""),
      h("td", {}, e.status ?? ""), h("td", { class: "detail", title: eventDetail(e) }, eventDetail(e).slice(0, 180)))));
    const note = events.length > LIMIT ? `Latest ${LIMIT} of ${events.length} steps, newest first.` : `${events.length} steps, newest first.`;
    return h("section", { class: "card events" }, h("h3", {}, "State machine"), h("p", { class: "note" }, note),
      h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Day", "Step", "Kind", "Phase", "Status", "Detail"].map((t) => h("th", { scope: "col" }, t)))), body)));
  }
}
