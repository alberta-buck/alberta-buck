"""The privacy paper's world, loaded into wallet objects, and driven on a live chain.

* :class:`PrivacyWorld` -- loads ``alberta_buck/test/vectors/privacy/world.json``
  (built by scripts/snark/gen_privacy_world.py): the cast's core records and
  wallet secrets, their accounts, the identity tree, and three Notes with
  REAL Groth16 proofs -- Aspen Mutual's B1 and A1 batches and Bob's A2 cheque.

* :class:`PrivacyChain` -- the full Notes stack on anvil (a :class:`NotesStack`
  whose trusted issuer is Alberta's :class:`~alberta_buck.wallet.issuer.Issuer`),
  with the operations the story needs: EOA registration from a credential,
  public binding, identity-bound approve, transfer, and each note's mint and
  spend.  Every step is a real transaction, and :attr:`PrivacyChain.observer`
  decodes any of them as an outside observer sees it.

The Groth16 proofs are pre-generated because proving the deposit gates takes
15-20 s and ~2 GB each; every sigma proof, envelope and registration is made
live.  :meth:`PrivacyWorld.replay` hands an operation the draws recorded when
the fixture was built, so a wallet operation re-run in the paper rebuilds the
exact object the pre-generated proofs were made over.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from alberta_buck.wallet.bn254 import G1, mul, rand_scalar, words_to_point, point_to_words
from alberta_buck.wallet.chaum_pedersen import CPProof, chaum_pedersen_prove
from alberta_buck.wallet.elgamal import ElGamalCiphertext, elgamal_encrypt
from alberta_buck.wallet.issuer import IssuedCredential, present_for_registration
from alberta_buck.wallet.nizk import RegistrationProof, registration_prove
from alberta_buck.wallet.notes import NoteOpening
from alberta_buck.wallet.ps import PSPresentation
from alberta_buck.sim.notes_stack import ACCOUNT_STUB, NotesStack, Step, _ct_tuple, _g1_tuple, _xy
from alberta_buck.sim.observer import Observer

WORLD                           = Path(__file__).resolve().parents[1] / "test" / "vectors" / "privacy" / "world.json"
FLAVORS                         = {"b1": 3, "a1": 1, "a2": 2}


def _pt(d):
    return words_to_point(int(d["x"]), int(d["y"]))


def _ct(d) -> ElGamalCiphertext:
    return ElGamalCiphertext(R=_pt(d["R"]), C=_pt(d["C"]))


def replay(values):
    """An rng that hands back recorded draws, in order."""
    it = iter(int(v) for v in values)
    return lambda: next(it)


@dataclass(frozen=True)
class Person:
    """One member of the cast: a core record and, for a citizen, the wallet's secrets."""
    name:                       str
    identity:                   str                   # the canonical core record: M's preimage
    fields:                     Dict[str, Any]
    m:                          int
    M:                          Any
    seed:                       Optional[int] = None  # the wallet seed: receiving key and salts derive from it
    k:                          Optional[int] = None  # receiving secret
    pk_recv:                    Any = None            # receiving (mailbox) public key
    salts:                      Dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Acct:
    """One registered account: an Ethereum address and its identity key pair."""
    label:                      str
    owner:                      str
    addr:                       int
    address:                    str                # checksummed
    sk:                         int                # identity secret key (BN254)
    pk:                         Any
    r:                          int                # the registration ciphertext's randomness
    E:                          ElGamalCiphertext  # Enc(M, pk; r): the account's registered identity envelope
    m:                          int
    M:                          Any
    is_public:                  bool
    kind:                       str                # "eoa" | "contract"
    eth_key:                    Optional[str] = None


@dataclass
class Note:
    """One note of the world, with its batch and its pre-generated proofs."""
    flavor:                     str
    raw:                        Dict[str, Any]

    @property
    def batch(self) -> List[NoteOpening]:
        return [NoteOpening(int(o["flavor"]), int(o["v"]), int(o["rho"]), int(o["idHash"]), 0)
                for o in self.raw["batch"]]

    @property
    def opening(self) -> NoteOpening:
        return self.batch[self.raw["index"]]

    @property
    def cms(self) -> List[int]:
        return [int(c) for c in self.raw["cms"]]

    @property
    def cm(self) -> int:
        return self.cms[self.raw["index"]]

    @property
    def issuer(self) -> str:
        return self.raw["issuer"]

    @property
    def payout(self) -> str:
        return self.raw["payout"]

    @property
    def nullifier(self) -> int:
        return int(self.raw["nullifier"])

    @property
    def face(self) -> int:
        return self.opening.v

    @property
    def timings(self) -> Dict[str, float]:
        return self.raw.get("timings", {})

    @property
    def delivery(self) -> Optional[Dict[str, Any]]:
        return self.raw.get("delivery")

    @property
    def e_enc(self) -> Optional[ElGamalCiphertext]:
        """An addressed spend's fresh envelope: the named identity, re-sealed to the mailbox."""
        return _ct(self.raw["eEnc"]) if "eEnc" in self.raw else None


class PrivacyWorld:
    """The privacy fixture world as wallet objects."""

    def __init__(self, raw: Dict[str, Any]):
        self.raw = raw
        self.chainid = int(raw["chainid"])
        self.kyc = raw["kyc"]
        self.unit = int(raw["unit"])
        self.people: Dict[str, Person] = {}
        for name, p in raw["people"].items():
            self.people[name] = Person(
                name=name, identity=p["identity"], fields=json.loads(p["identity"]),
                m=int(p["m"]), M=_pt(p["M"]),
                seed=int(p["seed"]) if "seed" in p else None,
                k=int(p["kRecv"]) if "kRecv" in p else None,
                pk_recv=_pt(p["pkRecv"]) if "pkRecv" in p else None,
                salts={k: int(v) for k, v in p.get("salts", {}).items()},
            )
        self.accounts: Dict[str, Acct] = {}
        for label, a in raw["accounts"].items():
            from web3 import Web3
            owner = self.people[a["owner"]]
            self.accounts[label] = Acct(
                label=label, owner=a["owner"], addr=int(a["addr"], 16),
                address=Web3.to_checksum_address(a["addr"]),
                sk=int(a["sk"]), pk=_pt(a["pk"]), r=int(a["r"]), E=_ct(a["E"]),
                m=owner.m, M=owner.M, is_public=bool(a["isPublic"]), kind=a["kind"],
                eth_key=a.get("ethKey"),
            )
        self.notes = {f: Note(f, n) for f, n in raw["notes"].items()}
        self.identity_root = int(raw["identityTree"]["root"])
        self.identity_leaves = raw["identityTree"]["leaves"]

    @classmethod
    def load(cls, path: Path = WORLD) -> "PrivacyWorld":
        return cls(json.loads(Path(path).read_text()))

    def replay(self, flavor: str, what: str):
        """The recorded draws of one wallet operation, as an rng."""
        return replay(self.raw["notes"][flavor]["draws"][what])

    def new_account(self, owner: str, label: str, rng) -> Acct:
        """A fresh account for an existing identity: a new Ethereum key and a new identity key
        pair, encrypting the same M.  Nothing about it matches the owner's other accounts."""
        from eth_account import Account
        from web3 import Web3
        person                  = self.people[owner]
        eth                     = Account.from_key(rand_scalar(rng).to_bytes(32, "big"))
        sk, r                   = rand_scalar(rng), rand_scalar(rng)
        pk                      = mul(G1, sk)
        acct                    = Acct(label=label, owner=owner, addr=int(eth.address, 16),
                                       address=Web3.to_checksum_address(eth.address), sk=sk, pk=pk, r=r,
                                       E=elgamal_encrypt(person.M, pk, r), m=person.m, M=person.M,
                                       is_public=False, kind="eoa", eth_key=eth.key.hex())
        self.accounts[label] = acct
        return acct

    def fixture(self, flavor: str):
        """One note as an :class:`E2EFixture`, so the AB-RCPT/2 receipt builders apply as-is."""
        from alberta_buck.sim.notes_stack import E2EFixture, Party
        from alberta_buck.wallet.issuer_reenc import IssuerReencProof
        from alberta_buck.wallet.schnorr import SchnorrProof
        note                    = self.notes[flavor]
        d                       = note.raw

        def party(label):
            a = self.accounts[label]
            return Party(addr=a.addr, identity=self.people[a.owner].identity, m=a.m, M=a.M, pk=a.pk, sk=a.sk, E=a.E)

        carol                   = self.people["carol"]
        raw                     = {"parties": {"depositor": {"pkRecv": self.raw["people"]["carol"]["pkRecv"],
                                                             "kRecv": str(carol.k)}},
                                   "mailboxBinding": self.raw["mailboxBinding"]}
        sig = binding = None
        if "issuerSchnorr" in d:
            s                   = d["issuerSchnorr"]
            sig                 = SchnorrProof(e=int(s["e"]), s=int(s["s"]), R=_pt(s["R"]))
        if "a2Binding" in d:
            b                   = d["a2Binding"]["proof"]
            binding             = IssuerReencProof(
                            e=int(b["e"]), s_r=int(b["s_r"]), s_b=int(b["s_b"]), s_s=int(b["s_s"]),
                            s_g=int(b["s_g"]), A1=_pt(b["A1"]), A2=_pt(b["A2"]), A3=_pt(b["A3"]),
                            A4=_pt(b["A4"]), A5=_pt(b["A5"]), Q=_pt(b["Q"]), U=_pt(b["U"]), T=_pt(b["T"]))
        if "issuerSecrets" in d:
            raw["issuerSecrets"] = d["issuerSecrets"]
        if flavor == "b1":
            raw["sigma"] = {"eDepForIss": d["depositor"]["eDepForIss"]}
        return E2EFixture(
            flavor=flavor, chainid=self.chainid, face=note.face,
            issuer=party(note.issuer), depositor=party(note.payout),
            payout=self.accounts[note.payout].addr, opening=note.opening, cms=note.cms,
            nullifier=note.nullifier, issuer_sig=sig, binding=binding,
            note=d["bearer"] if flavor == "b1" else d["delivery"],
            timings=note.timings, raw=raw)

    def mailbox_binding(self):
        """Carol's certified mailbox association: what a payer checks before addressing her."""
        from alberta_buck.registry.tree import MembershipProof
        from alberta_buck.wallet.recvkey import ReceivingBinding
        b = self.raw["mailboxBinding"]
        return ReceivingBinding(
            pk_recv=self.people["carol"].pk_recv, salt=int(b["salt"]),
            path=MembershipProof(leaf=int(b["leaf"]), siblings=[int(x) for x in b["siblings"]],
                                 index_bits=[int(x) for x in b["indexBits"]],
                                 root=int(b["root"]), leaf_index=0),
        )


@dataclass(frozen=True)
class RegistrationPackage:
    """What a wallet sends to IdentityRegistry.register: nothing in it names the holder."""
    account:                    Acct
    presentation:               PSPresentation     # the masked credential
    proof:                      RegistrationProof
    pk:                         Any                # the account's identity public key
    E:                          ElGamalCiphertext  # Enc(M, pk; r): the identity envelope the registry stores


@dataclass(frozen=True)
class IdentityEnvelope:
    """An identity-bound approve's payload: the sender's M, encrypted for one counterparty."""
    sender:                     Acct
    target:                     str                # the counterparty's address
    E:                          ElGamalCiphertext  # Enc(M_sender, pk_target; r')
    proof:                      CPProof            # "same identity as my registration, and I hold its key"


class _ChainOf:
    """The one fixture attribute NotesStack's deployment reads."""
    def __init__(self, chainid: int):
        self.chainid = chainid


class PrivacyChain(NotesStack):
    """The Notes stack on anvil, trusting Alberta's issuer, driven by the story."""

    def __init__(self, anvil, world: PrivacyWorld, issuer, rng=None, block_time: Optional[int] = None):
        self.world = world
        super().__init__(anvil, _ChainOf(world.chainid), rng=rng, block_time=block_time, issuer=issuer)
        self.issuer = issuer
        # Aspen Mutual mints batches of four.
        self.chain.send(self.mint_adapter.functions.registerVerifier(
            4, self.chain.deploy("MintBatchN4Groth16Verifier").address))
        self.observer = Observer(self.w3, {"IdentityRegistry": self.reg, "Buck": self.buck,
                                           "Notes": self.notes})
        for a in world.accounts.values():
            name = world.people[a.owner].fields["given_name"]
            if a.owner == "aspen":
                self.observer.label(a.address, "Aspen Mutual Credit Union (public)", public=True)
            else:
                self.observer.label(a.address, f"{name}'s {'savings' if 'Savings' in a.label else 'everyday'} account")
        self.observer.label(self.notes.address, "Notes pool (public)", public=True)
        self.observer.label(self._iss_addr, "Alberta's issuer (public)", public=True)

    def observe(self, step: Step):
        return self.observer.observe(step.txhash)

    # -- identity -------------------------------------------------------------------------------

    def registration_package(self, acct: Acct, cred: IssuedCredential, rng=None) -> RegistrationPackage:
        """The wallet's side of registration: mask the credential, prove the envelope holds the
        certified identity, and bind the proof to this account, chain and registry."""
        rng                     = rng or self.rng
        pres, _a, b             = present_for_registration(cred, rng=rng)
        proof                   = registration_prove(pres, b, cred.m, acct.r, acct.pk, acct.E, acct.addr, acct.sk,
                                                     chainid=self.world.chainid, rng=rng,
                                                     registry=int(self.reg.address, 16))
        return RegistrationPackage(acct, pres, proof, acct.pk, acct.E)

    def register(self, pkg: RegistrationPackage) -> Step:
        a = pkg.account
        self._impersonate(a.address)
        pf = pkg.proof
        return self._send_from(
            self.reg.get_function_by_signature(
                "register(address,(uint256,uint256),((uint256,uint256),(uint256,uint256)),"
                "((uint256,uint256),(uint256,uint256)),"
                "(uint256,uint256,uint256,uint256,uint256,(uint256,uint256),(uint256,uint256),"
                "(uint256,uint256),(uint256,uint256)))")(
                self._iss_addr, _xy(pkg.pk), (_xy(pkg.E.R), _xy(pkg.E.C)),
                (_xy(pkg.presentation.A), _xy(pkg.presentation.B)), self._proof_arg(pf)),
            a.address, f"register {a.label}")

    def bind_public(self, acct: Acct) -> List[Step]:
        """A public identity (Aspen Mutual's note-issuing contract): authorise, then bind."""
        self.anvil._rpc("anvil_setCode", [acct.address, ACCOUNT_STUB])
        self._impersonate(acct.address)
        s0 = self._send_from(
            self.reg.functions.authorizeContractBinding(
                self.gov, _xy(acct.pk), (_xy(acct.E.R), _xy(acct.E.C)), True, False),
            acct.address, f"authorize {acct.label} binding")
        s1 = self._send_from(
            self._credential_bind_fn(acct.address, acct.pk, acct.sk, True, False,
                                     m=acct.m, r=acct.r, E=acct.E),
            self.gov, f"bind {acct.label} (public)")
        return [s0, s1]

    def post_identity_root(self, root: int) -> Step:
        """The aggregator posts the identity root every membership proof is made against."""
        gov = self.gov.address if hasattr(self.gov, "address") else self.gov
        self._send_from(self.reg.functions.setRootAuthority(gov), self.gov, "setRootAuthority")
        self._send_from(self.reg.functions.setAggregator(gov), self.gov, "setAggregator")
        step = self._send_from(self.reg.functions.postIdentityRoot(root, b"\x00" * 32),
                               self.gov, "postIdentityRoot")
        assert self.reg.functions.identityRoot().call() == root
        return step

    # -- money ----------------------------------------------------------------------------------

    def fund(self, acct: Acct, amount: int) -> Step:
        """Grant insured credit and mint BUCK against it (how every account gets its money)."""
        gov = self.gov.address if hasattr(self.gov, "address") else self.gov
        self._impersonate(acct.address)
        self._send_from(self.credit.functions.setCreditIssuer(gov, True), acct.address, f"{acct.label} accepts insurer")
        fn                      = self.credit.functions.createCredit(acct.address, 0, 10 * amount, 10 * amount, 0, 0, 0, 0)
        token                   = fn.call()
        self._send_from(fn, self.gov, f"credit for {acct.label}")
        self._send_from(self.credit.functions.forceActivate(token, 10 * amount), acct.address, f"{acct.label} activates credit")
        return self._send_from(self.buck.functions.mint(amount), acct.address, f"{acct.label} mints BUCK")

    def envelope(self, sender: Acct, target: str, target_pk, rng=None) -> IdentityEnvelope:
        """Encrypt the sender's identity for one counterparty and prove it is the registered one."""
        rng                     = rng or self.rng
        r_prime                 = rand_scalar(rng)
        E_for                   = elgamal_encrypt(sender.M, target_pk, r_prime)
        cp                      = chaum_pedersen_prove(sender.E, E_for, sender.pk, target_pk, sender.sk, r_prime,
                                                       sender.addr, int(target, 16), self.world.chainid, rng=rng,
                                                       registry=int(self.reg.address, 16))
        return IdentityEnvelope(sender, target, E_for, cp)

    def approve(self, env: IdentityEnvelope, amount: int = 0) -> Step:
        """Buck's identity-bound approve: lays down the receipt fragment for this direction."""
        a = env.sender
        self._impersonate(a.address)
        fn = self.buck.get_function_by_signature(
            "approve(address,uint256,((uint256,uint256),(uint256,uint256)),"
            "(uint256,uint256,uint256,(uint256,uint256),(uint256,uint256),(uint256,uint256)))")
        cp = env.proof
        return self._send_from(
            fn(env.target, amount, (_xy(env.E.R), _xy(env.E.C)),
               (cp.e, cp.s1, cp.s2, _xy(cp.T1), _xy(cp.T2), _xy(cp.T3))),
            a.address, f"{a.label} approves {env.target[:10]}")

    def allow(self, acct: Acct, spender: str, amount: int) -> Step:
        """A plain allowance (a public account needs no identity envelope)."""
        self._impersonate(acct.address)
        fn = self.buck.get_function_by_signature("approve(address,uint256)")
        return self._send_from(fn(spender, amount), acct.address, f"{acct.label} allows {spender[:10]}")

    def transfer(self, sender: Acct, to: str, amount: int) -> Step:
        self._impersonate(sender.address)
        return self._send_from(self.buck.functions.transfer(to, amount), sender.address,
                               f"{sender.label} transfers", event="Transfer",
                               contract=self.buck)

    def balance(self, address: str) -> int:
        return self.buck.functions.balanceOf(address).call()

    # -- receipts -------------------------------------------------------------------------------

    def eoa_receipt(self, env: IdentityEnvelope, payee: Acct, step: Step, value: int, rng=None):
        """The payee's AB-RCPT/2 receipt for a direct payment: names the payer through the
        payer's approve envelope, which only the payee can open."""
        from alberta_buck.wallet.build_receipt import build_eoa_priv
        payer                   = env.sender
        people                  = self.world.people
        return build_eoa_priv(
            self.world.chainid, self.contracts,
            payer.addr, people[payer.owner].identity, payer.M, payer.pk, payer.E,
            env.E, env.proof,
            payee.addr, people[payee.owner].identity, payee.M, payee.pk, payee.sk, payee.E,
            value=value, block_time=step.timestamp, txhash=step.txhash, block=step.block,
            logindex=step.logindex, rng=rng or self.rng)

    def note_receipt(self, flavor: str, role: str, mint: Step, spend: Step, rng=None):
        """Either party's AB-RCPT/2 receipt for a note: ``recipient`` or ``issuer``."""
        return self.world.fixture(flavor).build_receipt(role, self.contracts, rng=rng or self.rng, **self.anchors(mint, spend))

    # -- notes ----------------------------------------------------------------------------------

    def mint_note(self, note: Note) -> Step:
        """Submit a batch mint with its pre-generated Groth16 proof, as its issuer."""
        d                       = note.raw
        issuer                  = self.world.accounts[note.issuer]
        self._impersonate(issuer.address)
        m                       = d["mint"]["public"]
        args                    = (bytes.fromhex(d["mint"]["proofBytes"][2:]), int(m["oldRoot"]), int(m["newRoot"]),
                                   int(m["nextLeafIndex"]), int(m["totalFace"]), [int(c) for c in m["cm"]])
        if note.flavor == "a2":
            fn = self.notes.get_function_by_signature(
                "mint(bytes,uint256,uint256,uint32,uint256,uint256[],uint256[],"
                "(((uint256,uint256),(uint256,uint256)),"
                "(uint256,uint256,uint256,uint256,uint256,"
                "(uint256,uint256),(uint256,uint256),(uint256,uint256),(uint256,uint256),"
                "(uint256,uint256),(uint256,uint256),(uint256,uint256),(uint256,uint256)))[])")
            b, p                = d["a2Binding"], d["a2Binding"]["proof"]
            binding             = (_ct_tuple(b["eIss"]),
                                   (int(p["e"]), int(p["s_r"]), int(p["s_b"]), int(p["s_s"]), int(p["s_g"]),
                                    _g1_tuple(p["A1"]), _g1_tuple(p["A2"]), _g1_tuple(p["A3"]),
                                    _g1_tuple(p["A4"]), _g1_tuple(p["A5"]), _g1_tuple(p["Q"]),
                                    _g1_tuple(p["U"]), _g1_tuple(p["T"])))
            call = fn(*args, [2], [binding])
        else:
            fn = self.notes.get_function_by_signature(
                "mint(bytes,uint256,uint256,uint32,uint256,uint256[],uint256[],"
                "(uint256,uint256,(uint256,uint256)))")
            s                   = d["issuerSchnorr"]
            call                = fn(*args, [1] * len(note.cms), (int(s["e"]), int(s["s"]), _g1_tuple(s["R"])))
        return self._send_from(call, issuer.address, f"Notes.mint {note.flavor}", event="Minted", contract=self.notes)

    def spend_note(self, note: Note) -> Step:
        """Submit the note's spend with its pre-generated proofs, as the depositing account."""
        d                       = note.raw
        dep                     = self.world.accounts[note.payout]
        self._impersonate(dep.address)
        sp                      = d["spend"]["public"]
        proof                   = bytes.fromhex(d["spend"]["proofBytes"][2:])
        root, nf                = int(sp["noteRoot"]), int(sp["nullifier"])
        face, rec               = int(sp["face"]), self._addr(int(sp["recipient"], 16))
        gate                    = bytes.fromhex(d["gate"]["proofBytes"][2:])
        if note.flavor == "b1":
            db                  = d["depositor"]["db"]
            b1p                 = (int(db["e"]), int(db["s_m"]), int(db["s_s"]), int(db["s_r"]), int(db["s_b"]),
                                   _g1_tuple(db["A2"]), _g1_tuple(db["A4"]), _g1_tuple(db["B1"]),
                                   _g1_tuple(db["B2"]), _g1_tuple(db["A_p"]), _g1_tuple(db["P_dep"]))
            fn = self.notes.functions.spendCoupledB1(
                proof, root, self.world.identity_root, nf, face, rec, note.cm,
                self.world.accounts[note.issuer].address,
                _ct_tuple(d["depositor"]["eDepForIss"]), b1p, gate)
        else:
            f                   = (self.notes.functions.spendCoupledA1 if note.flavor == "a1"
                                   else self.notes.functions.spendCoupledA2)
            fn                  = f(proof, root, self.world.identity_root, nf, face, rec, _ct_tuple(d["eEnc"]), gate)
        event = {"b1": "SpentCoupledB1", "a1": "SpentCoupledA1", "a2": "SpentCoupledA2"}[note.flavor]
        return self._send_from(fn, dep.address, f"Notes.spend {note.flavor}", event=event, contract=self.notes)


__all__ = ["Acct", "IdentityEnvelope", "Note", "Person", "PrivacyChain", "PrivacyWorld",
           "RegistrationPackage", "WORLD", "replay"]
