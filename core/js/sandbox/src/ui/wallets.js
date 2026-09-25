// The Wallets: each an account with its own keys, kept in this tab.  A wallet
// registers with a credential, introduces itself to the wallets it pays,
// sends BUCKs, and trades BUCKs for USDC in the pool -- each step saying what it
// discloses, and to whom.

import { addr, field, fill, h, money, parseAmount, preserving } from "./dom.js";

export function mountWallets(ctx) {
  const { app, act } = ctx;
  const panel = document.getElementById("panel-wallets");
  const label = h("input", { name: "label", autocomplete: "off", placeholder: "e.g. Carol's wallet" });
  const create = h("form", {
    class: "card",
    onsubmit: async (e) => {
      e.preventDefault();
      const w = await act("Creating a wallet", () => app.createWallet(label.value),
        (r) => `${r.label} created.`);
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
      "certified its holder — without saying who.  Paying another private wallet takes an ",
      "introduction first: each re-encrypts its identity for the other, so each can later say who ",
      "it dealt with, and no one else can."),
    h("div", { class: "cols" }, h("div", { class: "form" }, create),
      h("div", {}, h("h2", {}, "Wallets"), ctx.wallets.list)));
}

const discloses = (...text) => h("p", { class: "discloses" }, h("b", {}, "Discloses: "), ...text);

function walletCard(ctx, view, w) {
  const { app, act } = ctx;
  const others = view.wallets.filter((o) => o.id !== w.id);
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
      "Demurrage owing"), h("dd", {}, money(w.feeOwing, "BUCK", 6)),
    h("dt", {}, "USDC"), h("dd", {}, money(w.usdc, "USDC")),
    h("dt", {}, "ETH (gas)"), h("dd", {}, `${(Number(w.eth) / 1e18).toFixed(3)}`),
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
          onclick: () => act(`Registering ${w.label}`, () => app.register(w.id, cred.value),
            () => `${w.label} registered.`),
        }, "Register"))
        : h("p", { class: "hint" }, "Issue a credential first (Issuer)."),
      discloses("a fresh identity key, the identity encrypted to it, and proofs.  ",
        "Not who the holder is.")));
  } else {
    // Pay.
    const amt = h("input", { "data-key": key("pay"), inputmode: "decimal", placeholder: "BUCKs",
                             "aria-label": "amount to pay" });
    const to = h("select", { "data-key": key("to"), "aria-label": "pay to" },
      others.map((o) => h("option", { value: o.id }, `${o.id} ${o.label}`)));
    steps.push(h("div", { class: "step" },
      h("h3", {}, "Pay"),
      others.length ? h("div", { class: "row" }, amt, to, h("button", {
        type: "button", class: "primary",
        onclick: () => act(`Paying from ${w.label}`,
          () => app.send(w.id, to.value, parseAmount(amt.value, "amount")), () => {
            amt.value = "";
            return "Paid.";
          }),
      }, "Send")) : h("p", { class: "hint" }, "Create another wallet to pay."),
      discloses("the amount and both addresses, to everyone.  The payee must be registered and ",
        "introduced.")));

    // Introduce.
    const strangers = others.filter((o) => o.registered && !w.introduced.includes(o.id));
    const intro = h("select", { "data-key": key("intro"), "aria-label": "introduce to" },
      strangers.map((o) => h("option", { value: o.id }, `${o.id} ${o.label}`)));
    steps.push(h("div", { class: "step" },
      h("h3", {}, "Introduce"),
      strangers.length ? h("div", { class: "row" }, intro, h("button", {
        type: "button",
        onclick: () => act(`Introducing ${w.label}`, () => app.introduce(w.id, intro.value),
          () => "Introduced, both ways."),
      }, "Introduce")) : h("p", { class: "hint" },
        w.introduced.length ? `Introduced to ${w.introduced.join(", ")}.` : "No registered wallet to meet."),
      discloses("each wallet's identity to the other, encrypted so only they can read it.  The ",
        "chain sees ciphertexts and proofs.")));

    // The market.
    if (!w.trading) {
      steps.push(h("div", { class: "step" },
        h("h3", {}, "Market"),
        h("button", {
          type: "button",
          onclick: () => act(`Opening trading for ${w.label}`, () => app.openTrading(w.id),
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
          onclick: () => act(`Buying BUCKs for ${w.label}`,
            () => app.buy(w.id, { usdc: parseAmount(usdc.value, "USDC") }), (r) => {
              usdc.value = "";
              return `Bought ${(Number(r.received) / 1e6).toFixed(2)} BUCKs.`;
            }),
        }, "Buy BUCKs")),
        h("div", { class: "row" }, buck, h("button", {
          type: "button",
          onclick: () => act(`Selling BUCKs for ${w.label}`,
            () => app.sell(w.id, parseAmount(buck.value, "BUCKs")), (r) => {
              buck.value = "";
              return `Sold for ${(Number(r.received) / 1e6).toFixed(2)} USDC.`;
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
      onclick: () => act(`Depositing to ${w.label}`,
        () => app.deposit(w.id, parseAmount(dep.value, "USDC")), () => {
          dep.value = "";
          return "Deposited.";
        }),
    }, "Deposit")),
    h("p", { class: "hint" }, "Simulated dollars, as a bank transfer would bring them.")));

  return h("article", { class: "card", "data-wallet": w.id },
    h("div", { class: "card-head" }, h("h3", {}, w.label),
      h("span", {}, status, " ", h("span", { class: "chip" }, w.id))),
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
