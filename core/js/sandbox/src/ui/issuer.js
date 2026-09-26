// The Issuer: the registry's desk.  It certifies a person -- a short core
// record, signed -- and hands over a credential card.  Issuance is off-chain:
// the chain learns nothing until a wallet registers with the card, and then
// only that some trusted issuer vouched for someone.

import { walletTitle } from "../app.js";
import { download, field, fill, h, preserving } from "./dom.js";

export const SAMPLE_PEOPLE = [
  { given_name: "Carol", family_name: "Nakamura", date_of_birth: "1994-10-27" },
  { given_name: "Bob", family_name: "Tremblay", date_of_birth: "1981-06-09" },
  { given_name: "Chloé", family_name: "Bélanger-李", date_of_birth: "1994-11-02" },
  { given_name: "Zoë", family_name: "Müller", date_of_birth: "1988-03-14" },
  { given_name: "花子", family_name: "田中", date_of_birth: "1979-12-01" },
  { given_name: "Amir", family_name: "Haddad", date_of_birth: "2001-07-19" },
];

export function mountIssuer(ctx) {
  const { app, act } = ctx;
  const panel = document.getElementById("panel-issuer");
  const given = h("input", { name: "given_name", autocomplete: "off", required: true });
  const family = h("input", { name: "family_name", autocomplete: "off", required: true });
  const dob = h("input", { name: "date_of_birth", type: "date", required: true, max: "2026-12-31" });
  let sample = 0;
  const form = h("form", {
    class: "card",
    onsubmit: async (e) => {
      e.preventDefault();
      const person = { given_name: given.value, family_name: family.value, date_of_birth: dob.value };
      const c = await act(`Certifying ${person.given_name} ${person.family_name}`,
        () => app.issue(person), (r) => `Issued ${r.id}: ${r.card.fields.id_number}.`);
      if (c) form.reset();
    },
  },
    h("h2", {}, "Certify a person"),
    h("p", { class: "hint" }, "The issuer checks documents and writes a short ",
      h("em", {}, "core record"), ": the name at first certification, the date of birth, a person ",
      "number it never reuses, and today's date.  It signs the record's fingerprint and hands the ",
      "person a credential card."),
    h("div", { class: "row" }, field("Given name", given), field("Family name", family)),
    field("Date of birth", dob),
    h("div", { class: "row" },
      h("button", { type: "submit", class: "primary" }, "Issue credential"),
      h("button", {
        type: "button", onclick: () => {
          const p = SAMPLE_PEOPLE[sample++ % SAMPLE_PEOPLE.length];
          given.value = p.given_name;
          family.value = p.family_name;
          dob.value = p.date_of_birth;
        },
      }, "Fill a sample person")),
    h("p", { class: "discloses" }, h("b", {}, "The chain learns: "), "nothing.  Issuance is off-chain."),
  );

  const paste = h("textarea", { "aria-label": "credential card JSON", placeholder: "{ \"issuer\": ... }" });
  const importer = h("details", { class: "card" },
    h("summary", {}, "Import a credential card"),
    h("p", { class: "hint" }, "Paste a card someone downloaded from an issuer.  The same card twice is ",
      "one credential."),
    paste,
    h("button", {
      type: "button", onclick: async () => {
        const c = await act("Importing a card", () => app.importCredential(paste.value),
          (r) => `Credential ${r.id} imported.`);
        if (c) paste.value = "";
      },
    }, "Import"));

  ctx.issuer = { list: h("div", { class: "cards" }) };
  fill(panel,
    h("p", { class: "intro" }, "Alberta Identity is the sandbox's issuer, trusted on chain by ",
      "governance.  A credential proves a person was certified; a wallet registered with it proves ",
      "that on chain without saying who."),
    h("div", { class: "cols" }, h("div", { class: "form" }, form, importer),
      h("div", {}, h("h2", {}, "Credentials"), h("p", { class: "hint" },
        "Each card belongs to its person.  Hand it to a wallet to register."), ctx.issuer.list)));
}

export function renderIssuer(ctx, view) {
  const { app, act } = ctx;
  const list = ctx.issuer.list;
  const unregistered = view.wallets.filter((w) => !w.registered);
  preserving(list, () => fill(list, view.credentials.length === 0
    ? h("p", { class: "empty" }, "No credentials yet.  Certify someone.")
    : view.credentials.map((c) => {
      const pick = h("select", { "data-key": `issuer:wallet:${c.id}`, "aria-label": "wallet to register" },
        unregistered.map((w) => h("option", { value: w.id }, walletTitle(w))));
      const held = c.wallets.map((id) => {
        const w = view.wallets.find((x) => x.id === id);
        return w ? walletTitle({ id: w.id, label: w.label }) : id;
      });
      return h("article", { class: "card" },
        h("div", { class: "card-head" }, h("h3", {}, c.name), h("span", { class: "chip" }, c.id)),
        h("dl", { class: "kv" },
          h("dt", {}, "Person number"), h("dd", {}, h("code", {}, c.personNumber)),
          h("dt", {}, "Born"), h("dd", {}, c.fields.date_of_birth),
          h("dt", {}, "Certified"), h("dd", {}, c.fields.issued_at.slice(0, 10)),
          h("dt", {}, "Registered wallets"), h("dd", {}, held.length ? held.join(", ") : "none")),
        h("div", { class: "row" },
          h("button", {
            type: "button", title: "Copy the card as JSON",
            onclick: () => navigator.clipboard?.writeText(app.credentialText(c.id)).then(
              () => ctx.say(`Card ${c.id} copied.`, "ok"), () => {}),
          }, "Copy card"),
          h("button", {
            type: "button",
            onclick: () => download(`credential-${c.personNumber}.json`, app.credentialText(c.id)),
          }, "Download")),
        h("div", { class: "step" },
          h("div", { class: "row" },
            h("button", {
              type: "button", class: "primary",
              onclick: () => act(`Creating and registering a wallet for ${c.name}`, async () => {
                const w = await app.createWallet("");
                return app.register(w.id, c.id);
              }, (w) => `${w.title} registered.`),
            }, `New wallet for ${c.fields.given_name}`),
            unregistered.length ? pick : null,
            unregistered.length ? h("button", {
              type: "button",
              onclick: () => act(`Registering ${pick.value}`, () => app.register(pick.value, c.id),
                (w) => `${w.title} registered.`),
            }, "Register it") : null),
          h("p", { class: "discloses" }, h("b", {}, "Registering puts on chain: "),
            "a fresh key, the identity encrypted to that key, a re-randomized signature and a ",
            "zero-knowledge proof.  Not the name, the number or the birth date.")),
      );
    })));
}
