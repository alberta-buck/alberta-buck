// The Wallets: each an account with its own keys, kept in this tab.  A wallet
// registers with a credential, introduces itself to the wallets it deals with,
// pays in BUCKs, USDC or ETH, and trades BUCKs for USDC in the pool -- each
// step saying what it discloses, and to whom.
//
// A wallet is "W1", or "W1: <its label>", and once registered it carries its
// holder's registered name too.  Another wallet learns that name only when
// this one introduces itself to it: until then its dropdowns show just the id,
// the label and the address.

import { ASSETS, walletTitle } from "../app.js";
import { addr, amount, field, fill, h, money, parseAmount, preserving, shortAddr } from "./dom.js";

// Another wallet as `viewer` knows it: its registered name only once it has
// introduced itself to the viewer.
const knownTo = (viewer) => (o) => walletTitle({
  id: o.id, label: o.label, name: o.introduced.includes(viewer.id) ? o.name : null });
const withAddr = (viewer) => (o) => `${knownTo(viewer)(o)} (${shortAddr(o.address)})`;

export function mountWallets(ctx) {
  const { app, act } = ctx;
  const panel = document.getElementById("panel-wallets");
  const label = h("input", { name: "label", autocomplete: "off", placeholder: "optional, e.g. Savings" });
  const create = h("form", {
    class: "card",
    onsubmit: async (e) => {
      e.preventDefault();
      const w = await act("Creating a wallet", () => app.createWallet(label.value),
        (r) => `${r.title} created.`);
      if (w) label.value = "";
    },
  },
    h("h2", {}, "Create a wallet"),
    h("p", { class: "hint" }, "A fresh account, with 10 ETH for gas and 10,000 USDC in savings.  ",
      "The Issuer's cards can also make one in a click."),
    h("div", { class: "row" }, field("Label", label),
      h("button", { type: "submit", class: "primary" }, "Create")),
  );
  ctx.wallets = { list: h("div", { class: "cards" }) };
  fill(panel,
    h("p", { class: "intro" }, "A registered wallet has proved, on chain, that a trusted issuer ",
      "certified its holder — without saying who.  A wallet introduces itself to another by ",
      "re-encrypting its identity for that wallet alone, which then knows its registered name; no ",
      "one else does.  Paying BUCKs between two private wallets takes an introduction each way."),
    h("div", { class: "cols" }, h("div", { class: "form" }, create),
      h("div", {}, h("h2", {}, "Wallets"), ctx.wallets.list)));
}

const discloses = (...text) => h("p", { class: "discloses" }, h("b", {}, "Discloses: "), ...text);

function walletCard(ctx, view, w) {
  const { app, act } = ctx;
  const others = view.wallets.filter((o) => o.id !== w.id);
  const known = knownTo(w);
  const option = withAddr(w);
  const me = walletTitle({ id: w.id, label: w.label });
  const key = (k) => `wallet:${w.id}:${k}`;
  const held = w.signedBalance > 0n ? w.signedBalance : 0n;
  const drawn = w.signedBalance < 0n ? -w.signedBalance : 0n;
  const status = w.registered
    ? h("span", { class: "chip good" }, "registered")
    : h("span", { class: "chip warn" }, "not registered");

  const balances = h("dl", { class: "kv" },
    h("dt", {}, "BUCKs held"), h("dd", {}, money(held, "BUCK")),
    drawn ? h("dt", {}, "Credit drawn") : null, drawn ? h("dd", {}, money(drawn, "BUCK")) : null,
    h("dt", { title: "Held BUCKs plus unused credit" }, "Spendable"), h("dd", {}, money(w.balance, "BUCK")),
    h("dt", {}, "Credit limit"), h("dd", {}, money(w.creditLimit, "BUCK")),
    h("dt", { title: "Demurrage accrues on held BUCKs; it is settled at the next transfer" },
      "Demurrage owing"), h("dd", {}, money(w.feeOwing, "BUCK")),
    h("dt", {}, "USDC"), h("dd", {}, money(w.usdc, "USDC")),
    h("dt", { title: "For gas" }, "ETH"), h("dd", {}, money(w.eth, "ETH", 18)),
  );

  const steps = [];
  if (!w.registered) {
    const cred = h("select", { "data-key": key("cred"), "aria-label": "credential" },
      view.credentials.map((c) => h("option", { value: c.id }, `${c.id} ${c.name}`)));
    steps.push(h("div", { class: "step" },
      h("h3", {}, "Register"),
      view.credentials.length
        ? h("div", { class: "row" }, cred, h("button", {
          type: "button", class: "primary",
          onclick: () => act(`Registering ${me}`, () => app.register(w.id, cred.value),
            (r) => `${r.title} registered.`),
        }, "Register"))
        : h("p", { class: "hint" }, "Issue a credential first (Issuer)."),
      discloses("a fresh identity key, the identity encrypted to it, and proofs.  ",
        "Not who the holder is.")));
  }

  // Pay: BUCKs once registered; USDC and ETH from any wallet, to any.
  const assets = ASSETS.filter((a) => w.registered || !a.identity);
  const amt = h("input", { "data-key": key("pay"), inputmode: "decimal", placeholder: "0.00",
                           "aria-label": "amount to pay" });
  // Keyed by registration, so a newly registered wallet starts on BUCKs.
  const asset = h("select", { "data-key": key(w.registered ? "asset" : "asset:unregistered"),
                              "aria-label": "pay in" },
    assets.map((a) => h("option", { value: a.key }, a.label)));
  const to = h("select", { "data-key": key("to"), "aria-label": "pay to" },
    others.map((o) => h("option", { value: o.id }, option(o))));
  steps.push(h("div", { class: "step" },
    h("h3", {}, "Pay"),
    others.length ? h("div", { class: "row" }, amt, asset, to, h("button", {
      type: "button", class: "primary",
      onclick: () => {
        const a = ASSETS.find((x) => x.key === asset.value);
        return act(`Paying from ${me}`, async () => {
          const v = parseAmount(amt.value, "amount", { decimals: a.decimals });
          await app.send(w.id, to.value, v, a.key);
          return v;
        }, (v) => {
          amt.value = "";
          return `Paid ${amount(v, 6, a.decimals)} ${a.key === "BUCK" && v === 1_000_000n ? "BUCK" : a.label} ` +
            `to ${to.value}.`;
        });
      },
    }, "Send")) : h("p", { class: "hint" }, "Create another wallet to pay."),
    discloses("the amount and both addresses, to everyone.  BUCKs also carry a receipt each side ",
      "can decrypt, so paying BUCKs between two private wallets takes an introduction each way; ",
      "USDC and ETH carry no identity.")));

  if (w.registered) {
    // Introduce: this wallet to one it has not yet introduced itself to.
    const strangers = others.filter((o) => o.registered && !w.introduced.includes(o.id));
    const intro = h("select", { "data-key": key("intro"), "aria-label": "introduce to" },
      strangers.map((o) => h("option", { value: o.id }, option(o))));
    const byId = (id) => known(others.find((o) => o.id === id));
    const standing = [
      w.introduced.length ? `Introduced to ${w.introduced.map(byId).join(", ")}.` : "",
      w.introducedBy.length ? `Introduced by ${w.introducedBy.map(byId).join(", ")}.` : "",
    ].filter(Boolean).join("  ");
    steps.push(h("div", { class: "step" },
      h("h3", {}, "Introduce"),
      strangers.length ? h("div", { class: "row" }, intro, h("button", {
        type: "button",
        onclick: () => act(`Introducing ${me} to ${intro.value}`, () => app.introduce(w.id, intro.value),
          () => `Introduced ${w.id} to ${intro.value}.`),
      }, "Introduce")) : null,
      standing || !strangers.length
        ? h("p", { class: "hint" }, standing || "No registered wallet to meet.") : null,
      discloses("this wallet's identity to that wallet alone, encrypted so only it can read it: it ",
        "learns the registered name.  The chain sees a ciphertext and a proof.")));

    // The market.
    if (!w.trading) {
      steps.push(h("div", { class: "step" },
        h("h3", {}, "Market"),
        h("button", {
          type: "button",
          onclick: () => act(`Opening trading for ${me}`, () => app.openTrading(w.id),
            () => "Trading open."),
        }, "Open trading"),
        discloses("this wallet's identity to the BUCK/USDC pool's operator (who must be able to say ",
          "who traded), and Permit2 approvals so the router can move its BUCKs and USDC to the pool.")));
    } else {
      const usdc = h("input", { "data-key": key("buy"), inputmode: "decimal", placeholder: "USDC",
                                "aria-label": "USDC to spend" });
      const buck = h("input", { "data-key": key("sell"), inputmode: "decimal", placeholder: "BUCKs",
                                "aria-label": "BUCKs to sell" });
      steps.push(h("div", { class: "step" },
        h("h3", {}, "Market"),
        h("div", { class: "row" }, usdc, h("button", {
          type: "button",
          onclick: () => act(`Buying BUCKs for ${me}`,
            () => app.buy(w.id, { usdc: parseAmount(usdc.value, "USDC") }), (r) => {
              usdc.value = "";
              return `Bought ${amount(r.received)} BUCKs for ${amount(r.paid)} USDC.`;
            }),
        }, "Buy BUCKs")),
        h("div", { class: "row" }, buck, h("button", {
          type: "button",
          onclick: () => act(`Selling BUCKs for ${me}`,
            () => app.sell(w.id, parseAmount(buck.value, "BUCKs")), (r) => {
              buck.value = "";
              return `Sold ${amount(r.paid)} BUCKs for ${amount(r.received)} USDC.`;
            }),
        }, "Sell BUCKs")),
        discloses("the amounts, the price and this address, to everyone.")));
    }
  }

  // Dollars in.
  const dep = h("input", { "data-key": key("deposit"), inputmode: "decimal", placeholder: "USDC",
                           "aria-label": "USDC to deposit" });
  steps.push(h("details", { class: "step" },
    h("summary", {}, "Deposit USDC"),
    h("div", { class: "row" }, dep, h("button", {
      type: "button",
      onclick: () => act(`Depositing to ${me}`,
        () => app.deposit(w.id, parseAmount(dep.value, "USDC")), () => {
          dep.value = "";
          return "Deposited.";
        }),
    }, "Deposit")),
    h("p", { class: "hint" }, "Simulated dollars, as a bank transfer would bring them.")));

  return h("article", { class: "card", "data-wallet": w.id },
    h("div", { class: "card-head" },
      h("h3", {}, me, w.name ? [": ", h("span", { class: "registered-name" }, w.name)] : null),
      status),
    h("div", { class: "sub" }, addr(w.address),
      w.credential ? ` · credential ${w.credential}` : "",
      w.trading ? " · trading" : ""),
    balances, ...steps);
}

export function renderWallets(ctx, view) {
  const list = ctx.wallets.list;
  preserving(list, () => fill(list, view.wallets.length === 0
    ? h("p", { class: "empty" }, "No wallets yet.")
    : view.wallets.map((w) => walletCard(ctx, view, w))));
}
