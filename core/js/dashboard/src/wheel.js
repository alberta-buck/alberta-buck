import { ChartTab } from "./overview.js";
import { RAW, windowFrames } from "./series.js";

const buck = (raw) => raw == null ? null : Number(raw) / RAW;
const netBuck = (scaled) => buck(scaled == null ? null : Number(scaled) / 1e36);
const money = (value) => value == null ? "n/a" : value.toLocaleString("en-US", { maximumFractionDigits: 12 });
const wheelSeries = (key, label, convert = buck) => ({
  key, label, value: (f) => convert(f.wheel_revenue?.[key]),
});
const hasRevenue = (frames) => frames.some((f) => f.wheel_revenue);
const specs = [
  {
    id: "basket-wheel-revenue", title: "Direct basket revenue per day", unit: "buck", kind: "line",
    note: "Arb cash and LP return together give the cycle gain. LP return already includes cycle fees. Separate pool collections include a BUCK mark for TOKEN fees.",
    showWhen: hasRevenue,
    series: [
      wheelSeries("arb_cash", "Arb cash"),
      wheelSeries("arb_lp", "Arb LP return"),
      wheelSeries("pool_fees", "Pool fee collection"),
      wheelSeries("prior_arb_fees", "Fees before arb"),
    ],
  },
  {
    id: "keeper-reserve", title: "Keeper reserve transfers per day", unit: "buck", kind: "line",
    note: "Net BUCK delivered. Funding goes into the wheel reserve. Payouts come from that reserve and go to the tick caller.",
    showWhen: hasRevenue,
    series: [
      wheelSeries("reserve_net_scaled", "Reserve funded", netBuck),
      wheelSeries("keeper_net_scaled", "Paid to keepers", netBuck),
    ],
  },
];

function tiles(frames) {
  const days = frames.map((f) => f.wheel_revenue).filter(Boolean);
  if (!days.length) return [];
  return [
    ["basket_total", "To basket", buck],
    ["reserve_net_scaled", "Reserve funded", netBuck],
    ["keeper_net_scaled", "Paid to keepers", netBuck],
  ].map(([key, label, convert]) => ({
    label, value: days.some((d) => d[key] == null) ? null : convert(days.reduce((s, d) => s + BigInt(d[key]), 0n)),
    unit: "buck", detail: "Measured run total",
  }));
}

function el(tag, value) {
  const node = document.createElement(tag);
  node.textContent = value ?? "";
  return node;
}

function cells(op, tokens) {
  const route = op.route == null ? "" : `Route ${op.route}`;
  if (op.kind === "arb") {
    return ["Arbitrage", op.to, money(buck(op.total)),
      `${route}, direction ${op.direction}. Cash ${money(buck(op.cash))}, LP ${money(buck(op.lp))}, incl. fees ${money(buck(op.lp_fees))}. Input ${money(buck(op.input_nominal))} nominal BUCK.`];
  }
  if (op.kind === "pool_fees") {
    const amount = Number(op.token_amount) / (10 ** op.token_decimals);
    const token = tokens[op.pool] ?? `pool ${op.pool} TOKEN`;
    return ["Pool fee collection", op.to, money(buck(op.total)),
      `${money(buck(op.buck))} BUCK plus ${amount.toLocaleString("en-US", { maximumSignificantDigits: 10 })} ${token} marked at ${money(buck(op.token_mark))} BUCK.`];
  }
  if (op.kind === "prior_arb_fees") {
    return ["Fees before arb", op.to, money(buck(op.total)), `${route}. Previously accrued pool fees collected before the cycle.`];
  }
  const details = op.kind === "keeper_payout" ? `Work completed ${op.work}.`
    : `Refused by reserve cap ${money(buck(op.refused))} nominal BUCK.`;
  return [op.kind === "keeper_payout" ? "Keeper payout" : "Reserve funding", op.to,
    money(netBuck(op.net_scaled)), `${money(buck(op.nominal))} nominal BUCK from ${op.source}. ${details}`];
}

export class WheelTab extends ChartTab {
  constructor(root, bus) {
    super(root, bus, specs, tiles);
    const card = el("section");
    card.className = "card";
    const label = el("label", "Operation day ");
    this.days = el("select");
    this.days.addEventListener("change", () => this.showDay());
    label.append(this.days);
    this.status = el("p");
    this.status.className = "note";
    this.table = el("div");
    this.table.className = "table-wrap";
    card.append(el("h3", "Confirmed wheel operations"), label, this.status, this.table);
    root.append(card);
    this.request = null;
    this.available = "";
  }

  update(args) {
    super.update(args);
    this.source = args.source;
    this.tokens = args.manifest?.tokens ?? [];
    const days = windowFrames(args.frames, args.range).filter((f) => f.wheel_revenue).map((f) => f.day);
    const key = days.join(",");
    if (this.available !== key) {
      this.available = key;
      const prior = this.days.value;
      const follow = !prior || prior === this.days.options[0]?.value;
      this.days.replaceChildren(...days.toReversed().map((day) => {
        const opt = el("option", `Day ${day}`);
        opt.value = day;
        return opt;
      }));
      if (!follow && days.includes(Number(prior))) this.days.value = prior;
    }
    this.days.disabled = !days.length;
    if (days.length) this.showDay();
    else {
      this.request = null;
      this.table.replaceChildren();
      this.status.textContent = hasRevenue(args.frames) ? "No wheel observations in this range."
        : "Wheel revenue was not recorded in this run.";
    }
  }

  async showDay() {
    const day = this.days.value;
    if (this.request?.day === day) return;
    const request = this.request = { day };
    this.status.textContent = "Loading operations…";
    this.table.replaceChildren();
    try {
      const rows = await this.source.wheel(day);
      if (this.request !== request) return;
      this.status.textContent = rows.length ? "" : "No direct wheel revenue or reserve transfers recorded that day.";
      if (!rows.length) return;
      const table = el("table");
      const head = el("tr");
      head.append(...["Operation", "Recipient", "BUCK value", "Details", "Transaction"].map((s) => el("th", s)));
      const body = el("tbody");
      for (const op of rows) {
        const tr = el("tr");
        tr.append(...cells(op, this.tokens).map((value) => el("td", value)));
        const tx = el("td", `Block ${op.block} · ${op.tx.slice(0, 10)}…`);
        tx.title = `${op.tx} · log ${op.log_index}`;
        tr.append(tx);
        body.append(tr);
      }
      const thead = el("thead");
      thead.append(head);
      table.append(thead, body);
      this.table.append(table);
    } catch (e) {
      if (this.request !== request) return;
      this.request = null;
      this.status.textContent = e.message;
    }
  }
}
