"""The Notes lifecycle on a live EVM, end to end, from the committed
real-proof e2e fixtures -- the anvil twin of test/NotesE2E.t.sol.

Two pieces:

* :class:`E2EFixture` -- loads one ``alberta_buck/test/vectors/e2e/{a1,a2,
  b1}.json`` world (built by scripts/snark/gen_e2e_fixtures.sh: REAL Groth16
  proofs at every gate, REAL named identities -- the canonical Alice/Bob KYC
  data) into wallet objects, and builds the AB-RCPT/1 receipt for either
  party over it.  The fixtures are package data, so this layer works from a
  venv-installed wheel with no repo checkout.

* :class:`NotesStack` -- deploys the full real-verifier contract stack onto a
  running anvil (chain-id 1, auto-impersonation), binds the fixture identities
  into the registry's incremental Poseidon accumulator (real Merkle updates on
  the EVM), funds and identity-approves the parties (the real Chaum-Pedersen
  ``Buck.approve`` handshake -- no storage hacks), and drives the fixture's
  mint and coupled spend, recording per-step gas / wall time / tx anchors.

The committed fixtures carry the *proofs* pre-generated (proving wall times
ride along in ``fixture.timings``); everything executed here -- registry
Merkle updates, Groth16 verification, escrow and payout -- is live EVM
execution.  Used by alberta-buck-receipt.org ("The Receipt Flow, Executed")
and test_receipt_e2e.py.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from alberta_buck.wallet.bn254 import G1, mul, rand_scalar, words_to_point, point_to_words
from alberta_buck.wallet.elgamal import ElGamalCiphertext, elgamal_encrypt
from alberta_buck.wallet.chaum_pedersen import chaum_pedersen_prove
from alberta_buck.wallet.schnorr import SchnorrProof
from alberta_buck.wallet.issuer_reenc import IssuerReencProof
from alberta_buck.wallet.notes import NoteOpening
from alberta_buck.wallet.build_receipt import (
    build_note_b1, build_note_a1, build_note_a2,
)
from alberta_buck.wallet.ps import ps_keygen, ps_sign, ps_present
from alberta_buck.wallet.nizk import bind_contract_prove
from alberta_buck.wallet.contract_binding import contract_binding_prove
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.vectors import ALICE_FIELDS, BOB_FIELDS

# The fixture worlds ship as alberta_buck.test package data, so a
# venv-installed wheel can load them with no repo checkout; the live-EVM
# NotesStack additionally needs the repo (forge artifacts, contract sources)
# and resolves it lazily via alberta_buck.sim.chain.repo_root().
E2E_VECTORS = Path(__file__).resolve().parents[1] / "test" / "vectors" / "e2e"

EVENT_BY_FLAVOR = {"b1": "SpentCoupledB1", "a1": "SpentCoupledA1", "a2": "SpentCoupledA2"}

# A registered "contract" account needs code; the world accounts get the same
# one-revert stub NotesE2E.t.sol etches.
ACCOUNT_STUB = "0x60006000fd"

# Must match scripts/snark/gen_e2e_world.py SEEDS so account() replay
# recovers the fixture's (sk, r) for credential bind.
_E2E_SEEDS = {"a1": 0xE2EA1, "a2": 0xE2EA2, "b1": 0xE2EB1}


def _pt(d: dict):
    return words_to_point(int(d["x"]), int(d["y"]))


def _ct(d: dict) -> ElGamalCiphertext:
    return ElGamalCiphertext(R=_pt(d["R"]), C=_pt(d["C"]))


def _xy(P) -> tuple:
    return point_to_words(P)


def _ct_tuple(d: dict) -> tuple:
    return ((int(d["R"]["x"]), int(d["R"]["y"])),
            (int(d["C"]["x"]), int(d["C"]["y"])))


def _g1_tuple(d: dict) -> tuple:
    return (int(d["x"]), int(d["y"]))


# ---------------------------------------------------------------------------
# The fixture world (wallet side)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Party:
    """One fixture wallet: the KYC preimage + the registered account."""
    addr:     int
    identity: str          # canonical_identity_data (the M preimage)
    m:        int
    M:        Any
    pk:       Any
    sk:       int
    E:        ElGamalCiphertext


@dataclass
class E2EFixture:
    """One real-proof e2e world, parsed into wallet objects."""
    flavor:    str
    chainid:   int
    face:      int
    issuer:    Party
    depositor: Party
    payout:    int
    opening:   NoteOpening
    cms:       List[int]
    nullifier: int
    issuer_sig: Optional[SchnorrProof]        # B1/A1 batch Schnorr
    binding:    Optional[IssuerReencProof]    # A2 mint binding
    note:      Dict[str, Any]                 # raw notePayload (eNote/eRec/eIss/sigma)
    timings:   Dict[str, float]
    raw:       Dict[str, Any]                 # the full fixture JSON

    @classmethod
    def load(cls, flavor: str) -> "E2EFixture":
        d = json.loads((E2E_VECTORS / f"{flavor}.json").read_text())

        def party(k: str) -> Party:
            p = d["parties"][k]
            return Party(addr=int(p["addr"], 16), identity=p["identity"],
                         m=int(p["m"]), M=_pt(p["M"]), pk=_pt(p["pk"]),
                         sk=int(p["sk"]), E=_ct(p["E"]))

        o = d["opening"]
        sig = None
        if "issuerSchnorr" in d:
            s = d["issuerSchnorr"]
            sig = SchnorrProof(e=int(s["e"]), s=int(s["s"]), R=_pt(s["R"]))
        binding = None
        if "a2Binding" in d:
            b = d["a2Binding"]["proof"]
            binding = IssuerReencProof(
                e=int(b["e"]), s_r=int(b["s_r"]), s_b=int(b["s_b"]),
                s_s=int(b["s_s"]), s_g=int(b["s_g"]),
                A1=_pt(b["A1"]), A2=_pt(b["A2"]), A3=_pt(b["A3"]),
                A4=_pt(b["A4"]), A5=_pt(b["A5"]),
                Q=_pt(b["Q"]), U=_pt(b["U"]), T=_pt(b["T"]),
            )
        return cls(
            flavor=flavor, chainid=int(d["chainid"]), face=int(d["face"]),
            issuer=party("issuer"), depositor=party("depositor"),
            payout=int(d["payout"], 16),
            opening=NoteOpening(flavor=int(o["flavor"]), v=int(o["v"]),
                                rho=int(o["rho"]), id_hash=int(o["idHash"]),
                                predicate=0),
            cms=[int(c) for c in d["mint"]["public"]["cm"]],
            nullifier=int(o["nullifier"]),
            issuer_sig=sig, binding=binding,
            note=d["notePayload"],
            timings=d.get("timings", {}),
            raw=d,
        )

    def note_cts(self) -> Dict[str, Any]:
        """The Identity-M note payload as wallet objects (ciphertexts/points),
        plus -- for B1 -- the spend event's ``eDepForIss``."""
        out: Dict[str, Any] = {}
        for k, v in self.note.items():
            if k in ("eNote", "eRec", "eIss"):
                out[k] = _ct(v)
            elif k == "sigma_R":
                out[k] = _pt(v)
            else:
                out[k] = int(v)
        if self.flavor == "b1":
            out["eDepForIss"] = _ct(self.raw["sigma"]["eDepForIss"])
        return out

    def mailbox_binding(self):
        """The recipient's holder-produced evidence, as a wallet object.

        None for B1: a bearer note is addressed to nobody, so there is no
        mailbox to bind.
        """
        b = self.raw.get("mailboxBinding")
        if b is None:
            return None
        from alberta_buck.registry.tree import MembershipProof
        from alberta_buck.wallet.recvkey import ReceivingBinding
        return ReceivingBinding(
            pk_recv=_pt(self.raw["parties"]["depositor"]["pkRecv"]),
            salt=int(b["salt"]),
            path=MembershipProof(
                leaf=int(b["leaf"]),
                siblings=[int(x) for x in b["siblings"]],
                index_bits=[int(x) for x in b["indexBits"]],
                root=int(b["root"]), leaf_index=0,
            ),
        )

    # -- receipts ------------------------------------------------------------

    def build_receipt(self, role: str, contracts: Dict[str, str],
                      mint: Dict[str, Any], spend: Dict[str, Any],
                      notes: Optional[List[str]] = None, rng=None):
        """Build this world's AB-RCPT/1 :class:`ReceiptCore` from either side.

        ``mint`` carries the Minted anchor (``txhash``, ``block``); ``spend``
        the SpentCoupled* anchor (``txhash``, ``block``, ``logindex``,
        ``timestamp``).  ``role`` is ``"recipient"`` (the depositor's copy)
        or ``"issuer"`` (the payer-side copy).
        """
        iss, dep, np = self.issuer, self.depositor, self.note
        kw = dict(
            chainid=self.chainid, contracts=contracts,
            issuer_addr=iss.addr, issuer_identity=iss.identity,
            issuer_M=iss.M, issuer_pk=iss.pk,
            payee_addr=dep.addr, payee_identity=dep.identity,
            payee_M=dep.M, payee_pk=dep.pk, payee_E_addr=dep.E,
            opening=self.opening, cms=self.cms,
            nullifier=self.nullifier, face=self.face,
            value=self.face, block_time=spend["timestamp"],
            txhash=spend["txhash"], block=spend["block"],
            logindex=spend["logindex"],
            mint_txhash=mint["txhash"], mint_block=mint["block"],
            role=role, notes=notes, rng=rng,
        )
        if role == "recipient":
            kw["payee_sk"] = dep.sk

        if self.flavor != "b1":
            # The addressed legs: the mailbox key, and whichever evidence about
            # it this side can produce.  The recipient holds k; the issuer holds
            # the randomness it encrypted with.  Neither holds the other's, and
            # that is what makes the receipt evidence.
            dp = self.raw["parties"]["depositor"]
            kw["pk_recv"] = _pt(dp["pkRecv"])
            kw["mailbox_binding"] = self.mailbox_binding()
            if role == "recipient":
                kw["k_recv"] = int(dp["kRecv"])
            else:
                # The minter's retained randomness -- not the payload's wrapped
                # copy, which only the mailbox holder can open.  A1's identity
                # ciphertext is eRec (randomness r'); A2's is eIss (also r').
                # eNote carries the value under r_note in both flavours; the
                # identity ciphertext (A1's eRec, A2's eIss) carries r'.
                sec = self.raw["issuerSecrets"]
                kw["r_note"] = int(sec["rNote"])
                kw["r_id"] = int(sec["rPrime"])

        if self.flavor == "b1":
            return build_note_b1(
                issuer_sig=self.issuer_sig,
                sigma_R=_pt(np["sigma_R"]), sigma_s=int(np["sigma_s"]),
                eDepForIss=_ct(self.raw["sigma"]["eDepForIss"]),
                issuer_sk=iss.sk if role == "issuer" else None,
                **kw)
        if self.flavor == "a1":
            return build_note_a1(
                issuer_sig=self.issuer_sig,
                eNote=_ct(np["eNote"]), eRec=_ct(np["eRec"]),
                sigma_R=_pt(np["sigma_R"]), sigma_s=int(np["sigma_s"]),
                **kw)
        return build_note_a2(
            issuer_E_addr=iss.E,
            eNote=_ct(np["eNote"]), eIss=_ct(np["eIss"]),
            binding=self.binding,
            issuer_sk=iss.sk if role == "issuer" else None,
            **kw)


# ---------------------------------------------------------------------------
# The live EVM stack (anvil side)
# ---------------------------------------------------------------------------

@dataclass
class Step:
    """One on-chain step's anchors and costs."""
    name:      str
    txhash:    str
    block:     int
    timestamp: int
    gas:       int
    seconds:   float
    logindex:  Optional[int] = None


class NotesStack:
    """The full real-verifier Notes stack on a live anvil, fixture-driven.

    Mirrors test/NotesE2E.t.sol's setUp -- except that funding and the
    receipt-fragment plumbing use the LEGITIMATE paths (BuckCredit grant +
    ``Buck.mint``, and the identity-bound ``Buck.approve`` carrying a real
    Chaum-Pedersen re-encryption proof) rather than storage writes.
    """

    def __init__(self, anvil, fixture: E2EFixture, rng=None,
                 block_time: Optional[int] = None):
        from alberta_buck.sim.chain import Chain
        self.anvil = anvil
        self.fx = fixture
        self.w3 = anvil.w3
        self.chain = Chain(self.w3, self.w3.eth.accounts[0])
        self.gov = self.chain.deployer
        self.rng = rng or (lambda: int.from_bytes(__import__("os").urandom(32), "big"))
        # With `block_time` set, every step's block gets a deterministic
        # timestamp advancing block_time seconds per tx (requires an Anvil
        # started with a pinned genesis `timestamp` <= the chain clock).
        self._block_time = block_time
        self._clock = (self.w3.eth.get_block("latest")["timestamp"]
                       if block_time else None)
        self.steps: List[Step] = []
        assert self.w3.eth.chain_id == fixture.chainid, \
            "anvil must run the fixture's chain id (Anvil(chain_id=1))"
        self._deploy()

    # -- plumbing -------------------------------------------------------------

    def _addr(self, a: int) -> str:
        from web3 import Web3
        return Web3.to_checksum_address(f"0x{a:040x}")

    def _impersonate(self, addr: str) -> None:
        self.anvil._rpc("anvil_setBalance", [addr, hex(10**18)])

    def _send_from(self, fn, sender: str, name: str, event: Optional[str] = None,
                   contract=None) -> Step:
        """Send fn from `sender` (auto-impersonated), recording a Step."""
        if self._block_time:
            self._clock += self._block_time
            self.anvil.set_next_block_timestamp(self._clock)
        t0 = time.monotonic()
        rcpt = self.chain.send(fn, sender=sender)
        dt = time.monotonic() - t0
        blk = self.w3.eth.get_block(rcpt["blockNumber"])
        logindex = None
        if event is not None and contract is not None:
            from web3.logs import DISCARD
            entries = getattr(contract.events, event)().process_receipt(rcpt, errors=DISCARD)
            if entries:
                logindex = entries[0]["logIndex"]
        step = Step(name=name, txhash=rcpt["transactionHash"].to_0x_hex(),
                    block=rcpt["blockNumber"], timestamp=blk["timestamp"],
                    gas=rcpt["gasUsed"], seconds=round(dt, 3), logindex=logindex)
        self.steps.append(step)
        return step

    # -- deployment -----------------------------------------------------------

    def _deploy_poseidon(self) -> str:
        """Deploy the Poseidon-T3 helper from PoseidonT3Bytecode's creation
        code (the same bytes PoseidonT3Bytecode.deploy() `create`s)."""
        from alberta_buck.sim.chain import repo_root
        src = (repo_root() / "src" / "PoseidonT3Bytecode.sol").read_text()
        code = re.search(r'hex"([0-9a-fA-F]+)"', src).group(1)
        h = self.w3.eth.send_transaction({
            "from": self.gov, "data": "0x" + code, "gas": 10_000_000, "gasPrice": 0,
        })
        rcpt = self.w3.eth.wait_for_transaction_receipt(h)
        assert rcpt["status"] == 1, "poseidon deploy failed"
        return rcpt["contractAddress"]

    def _deploy(self) -> None:
        ch, gov = self.chain, self.gov

        # Identity registry + the real incremental Poseidon accumulator.
        self.reg = ch.deploy("IdentityRegistry", gov)
        ch.send(self.reg.functions.setIdentityPoseidon(self._deploy_poseidon()))

        # Buck stack (harnessed credit so the issuer can be funded).
        self.credit = ch.deploy("BuckCreditHarness", sol_file="BuckCreditHarness")
        self.kctrl  = ch.deploy("BuckKControllerStatic", 10**18, gov)
        self.buck   = ch.deploy("Buck", self.credit.address, self.kctrl.address,
                                self.reg.address, self._addr(0xB00C))
        ch.send(self.reg.functions.setBuck(self.buck.address))
        ch.send(self.credit.functions.setBuck(self.buck.address))

        # The REAL Groth16 verifier stack.
        mint_adapter = ch.deploy("MintVerifierAdapter", gov)
        a2_adapter   = ch.deploy("MintVerifierA2Adapter", gov)
        ch.send(mint_adapter.functions.registerVerifier(
            1, ch.deploy("MintBatchN1Groth16Verifier").address))
        ch.send(a2_adapter.functions.registerVerifier(
            1, ch.deploy("MintBatchA2N1Groth16Verifier").address))
        spend_adapter = ch.deploy("SpendVerifierAdapter",
                                  ch.deploy("SpendGroth16Verifier").address)
        # B1's membership goes through the REPAIRED circuit: its blind is
        # proven rather than witnessed, and its generator has no known
        # logarithm.  The G1-tie adapter it replaces let a depositor shift the
        # blind onto another registered Identity and spend while unregistered.
        mem_adapter  = ch.deploy("IdentityMembershipB1VerifierAdapter")

        self.notes = ch.deploy("Notes", self.buck.address, mint_adapter.address,
                               spend_adapter.address, gov)
        ch.send(self.notes.functions.setIdentityRegistry(self.reg.address))
        ch.send(self.notes.functions.setA2MintVerifier(a2_adapter.address))
        ch.send(self.notes.functions.setIdentityMembershipVerifier(mem_adapter.address))

        # The addressed flavours spend through the FOLDED gate: one proof
        # carrying every relation.  The slot is not optional -- an addressed
        # spend with it unset reverts, because there is no weaker path to fall
        # back to.
        fold_adapter = ch.deploy("DepositFoldVerifierAdapter", self.reg.address)
        ch.send(self.notes.functions.setDepositFoldVerifier(fold_adapter.address))

        # The Notes pool is a Public-Identity Carrying contract with a REAL
        # key pair, so a private party's identity-bound approve toward it is
        # a genuine re-encryption (the operator could decrypt it).
        self.pool_sk = rand_scalar(self.rng)
        self.pool_pk = mul(G1, self.pool_sk)
        self._bind_cred = self.reg.get_function_by_signature(
            "bindContract(address,address,(uint256,uint256),"
            "((uint256,uint256),(uint256,uint256)),"
            "((uint256,uint256),(uint256,uint256)),"
            "(uint256,uint256,uint256,uint256,uint256,(uint256,uint256),(uint256,uint256),"
            "(uint256,uint256),(uint256,uint256)),"
            "(uint256,uint256,(uint256,uint256)),"
            "bool,bool)")
        self._iss_kp = ps_keygen(rng=self.rng)
        self._iss_addr = self._addr(0xAA)
        self.pool_m = rand_scalar(self.rng)
        self.pool_r = rand_scalar(self.rng)
        self.pool_E = elgamal_encrypt(mul(G1, self.pool_m), self.pool_pk, self.pool_r)
        g2 = lambda P: ((int(P[0].coeffs[0]), int(P[0].coeffs[1])),
                        (int(P[1].coeffs[0]), int(P[1].coeffs[1])))
        ch.send(self.reg.functions.trustIssuer(
            self._iss_addr, (g2(self._iss_kp.pk_X), g2(self._iss_kp.pk_Y),
                             _xy(self._iss_kp.pk_Y1))))
        ch.send(self.notes.functions.authorizeIdentityBinding(
            self.reg.address, self.gov, _xy(self.pool_pk),
            (_xy(self.pool_E.R), _xy(self.pool_E.C)),
            True, True))
        ch.send(self._credential_bind_fn(
            self.notes.address, self.pool_pk, self.pool_sk, True, True,
            m=self.pool_m, r=self.pool_r, E=self.pool_E))

        self.contracts = {
            "registry": self.reg.address.lower(),
            "buck":     self.buck.address.lower(),
            "notes":    self.notes.address.lower(),
        }

    def _proof_arg(self, pf):
        return (pf.e, pf.s_m, pf.s_b, pf.s_r, pf.s_sk, _xy(pf.C1), _xy(pf.T_C),
                _xy(pf.T_R), _xy(pf.T_key))

    def _binding_arg(self, pf):
        return (pf.e, pf.s, _xy(pf.T))

    def _credential_bind_fn(self, target, pk, sk, is_public, is_carrying,
                            m=None, r=None, E=None):
        """PS credential + NIZK, Fiat-Shamir registrant = uint160(target)."""
        if m is None:
            m = rand_scalar(self.rng)
        if r is None:
            r = rand_scalar(self.rng)
        if E is None:
            E = elgamal_encrypt(mul(G1, m), pk, r)
        pres, _a, b = ps_present(ps_sign(self._iss_kp, m, rng=self.rng),
                                 self._iss_kp.pk_Y1, rng=self.rng)
        pf = bind_contract_prove(
            pres, b, m, r, pk, E, int(target, 16), sk,
            chainid=self.fx.chainid, rng=self.rng,
            registry=int(self.reg.address, 16))
        binder = self.gov
        bind_auth = contract_binding_prove(
            sk, pk, int(target, 16), int(binder, 16), int(self.reg.address, 16),
            is_public, is_carrying, chainid=self.fx.chainid, rng=self.rng)
        return self._bind_cred(
            target, self._iss_addr, _xy(pk), (_xy(E.R), _xy(E.C)),
            (_xy(pres.A), _xy(pres.B)),
            self._proof_arg(pf), self._binding_arg(bind_auth), is_public, is_carrying)

    def _replay_fixture_accounts(self):
        """Recover (sk, r) for issuer then depositor (gen_e2e_world.account)."""
        import random
        rng_state = random.Random(_E2E_SEEDS[self.fx.flavor])
        rng = lambda: rng_state.getrandbits(256)
        out = []
        for fields in (BOB_FIELDS, ALICE_FIELDS):
            m = identity_scalar(canonical_identity_data(fields))
            sk = rand_scalar(rng)
            pk = mul(G1, sk)
            r = rand_scalar(rng)
            E = elgamal_encrypt(mul(G1, m), pk, r)
            out.append(dict(m=m, sk=sk, pk=pk, E=E, r=r))
        return out

    # -- lifecycle steps -------------------------------------------------------

    def bind_identities(self) -> List[Step]:
        """Credential-bind the two fixture identities.  Leaves are not
        inserted on-chain (unconstrained); governance posts the fixture root."""
        recovered = self._replay_fixture_accounts()
        out = []
        for b, rec in zip(self.fx.raw["binds"], recovered):
            addr = self._addr(int(b["addr"], 16))
            self.anvil._rpc("anvil_setCode", [addr, ACCOUNT_STUB])
            self._impersonate(addr)
            who = "issuer" if int(b["addr"], 16) == self.fx.issuer.addr else "depositor"
            out.append(self._send_from(
                self.reg.functions.authorizeContractBinding(
                    self.gov, _xy(rec["pk"]),
                    (_xy(rec["E"].R), _xy(rec["E"].C)), bool(b["isPublic"]), False),
                addr, f"authorize {who} binding"))
            out.append(self._send_from(
                self._credential_bind_fn(
                    addr, rec["pk"], rec["sk"], bool(b["isPublic"]), False,
                    m=rec["m"], r=rec["r"], E=rec["E"]),
                self.gov, f"bind {who}"))
        root = int(self.fx.raw["identityRoot"])
        out.append(self._send_from(
            self.reg.functions.setIdentityRoot(root), self.gov, "setIdentityRoot"))
        assert self.reg.functions.identityRoot().call() == root
        return out

    def fund_issuer(self) -> List[Step]:
        """Grant the issuer BuckCredit and mint BUCK against it."""
        iss = self._addr(self.fx.issuer.addr)
        face = self.fx.face
        gov_addr = self.gov.address if hasattr(self.gov, "address") else self.gov
        s0 = self._send_from(
            self.credit.functions.setCreditIssuer(gov_addr, True),
            iss, "acceptIssuer")
        fn = self.credit.functions.createCredit(iss, 0, 10 * face, 10 * face, 0, 0, 0, 0)
        token_id = fn.call()
        s1 = self._send_from(fn, self.gov, "createCredit")
        s2 = self._send_from(self.credit.functions.forceActivate(token_id, 10 * face),
                             iss, "activateCredit")
        s3 = self._send_from(self.buck.functions.mint(2 * face), iss, "buck.mint")
        return [s0, s1, s2, s3]

    def approve_pool(self, party: Party, amount: int, name: str) -> Step:
        """The identity-bound ``Buck.approve``: re-encrypt the party's
        registered Identity under the pool's key and prove it (the real
        Chaum-Pedersen handshake = ``IdentityRegistry.verifyApprove``)."""
        addr = self._addr(party.addr)
        self._impersonate(addr)
        r_prime = rand_scalar(self.rng)
        E_for_pool = elgamal_encrypt(party.M, self.pool_pk, r_prime)
        cp = chaum_pedersen_prove(
            party.E, E_for_pool, party.pk, self.pool_pk,
            party.sk, r_prime,
            party.addr, int(self.notes.address, 16), self.fx.chainid,
            rng=self.rng,
            registry=int(self.reg.address, 16),
        )
        approve4 = self.buck.get_function_by_signature(
            "approve(address,uint256,((uint256,uint256),(uint256,uint256)),"
            "(uint256,uint256,uint256,(uint256,uint256),(uint256,uint256),(uint256,uint256)))")
        e_t = (_xy(E_for_pool.R), _xy(E_for_pool.C))
        cp_t = (cp.e, cp.s1, cp.s2, _xy(cp.T1), _xy(cp.T2), _xy(cp.T3))
        return self._send_from(approve4(self.notes.address, amount, e_t, cp_t),
                               addr, name)

    def mint(self) -> Step:
        """Submit the fixture's REAL batch-mint Groth16 proof as the issuer."""
        d = self.fx.raw
        iss = self._addr(self.fx.issuer.addr)
        m = d["mint"]["public"]
        args = (bytes.fromhex(d["mint"]["proofBytes"][2:]),
                int(m["oldRoot"]), int(m["newRoot"]),
                int(m["nextLeafIndex"]), int(m["totalFace"]),
                [int(c) for c in m["cm"]])
        if self.fx.flavor == "a2":
            mint_fn = self.notes.get_function_by_signature(
                "mint(bytes,uint256,uint256,uint32,uint256,uint256[],uint256[],"
                "(((uint256,uint256),(uint256,uint256)),"
                "(uint256,uint256,uint256,uint256,uint256,"
                "(uint256,uint256),(uint256,uint256),(uint256,uint256),(uint256,uint256),"
                "(uint256,uint256),(uint256,uint256),(uint256,uint256),(uint256,uint256)))[])")
            b = d["a2Binding"]
            p = b["proof"]
            binding = (_ct_tuple(b["eIss"]),
                       (int(p["e"]), int(p["s_r"]), int(p["s_b"]), int(p["s_s"]),
                        int(p["s_g"]),
                        _g1_tuple(p["A1"]), _g1_tuple(p["A2"]), _g1_tuple(p["A3"]),
                        _g1_tuple(p["A4"]), _g1_tuple(p["A5"]),
                        _g1_tuple(p["Q"]), _g1_tuple(p["U"]), _g1_tuple(p["T"])))
            fn = mint_fn(*args, [2], [binding])
        else:
            mint_fn = self.notes.get_function_by_signature(
                "mint(bytes,uint256,uint256,uint32,uint256,uint256[],uint256[],"
                "(uint256,uint256,(uint256,uint256)))")
            s = d["issuerSchnorr"]
            sig = (int(s["e"]), int(s["s"]), _g1_tuple(s["R"]))
            fn = mint_fn(*args, [1], sig)
        return self._send_from(fn, iss, "Notes.mint", event="Minted",
                               contract=self.notes)

    def spend(self) -> Step:
        """Submit the fixture's REAL coupled spend as the depositor.

        The shape differs by flavour, and the difference is the architecture.
        The addressed flavours submit ONE folded proof: their two facts rest
        on two different secrets -- the Identity and the receiving key -- so
        no sigma can tie them, and three checks sharing a public point would
        let a payload thief supply one of each.  B1's rest on one secret, so
        its sigma is a genuine tie and it submits sigma plus membership.
        """
        d = self.fx.raw
        dep = self._addr(self.fx.depositor.addr)
        self._impersonate(dep)
        sp = d["spend"]["public"]
        proof = bytes.fromhex(d["spend"]["proofBytes"][2:])
        root, nf = int(sp["noteRoot"]), int(sp["nullifier"])
        face, rec = int(sp["face"]), self._addr(int(sp["recipient"], 16))
        if self.fx.flavor == "b1":
            db = d["sigma"]["db"]
            b1p = (int(db["e"]), int(db["s_m"]), int(db["s_s"]), int(db["s_r"]),
                   int(db["s_b"]),
                   _g1_tuple(db["A2"]), _g1_tuple(db["A4"]), _g1_tuple(db["B1"]),
                   _g1_tuple(db["B2"]), _g1_tuple(db["A_p"]), _g1_tuple(db["P_dep"]))
            mem = bytes.fromhex(d["membership"]["proofBytes"][2:])
            fn = self.notes.functions.spendCoupledB1(
                proof, root, nf, face, rec, int(d["opening"]["cm"]),
                self._addr(self.fx.issuer.addr),
                _ct_tuple(d["sigma"]["eDepForIss"]), b1p, mem)
        else:
            # The folded gate: the re-encryption and one proof, nothing else.
            fold = bytes.fromhex(d["depositFold"]["proofBytes"][2:])
            f = (self.notes.functions.spendCoupledA1 if self.fx.flavor == "a1"
                 else self.notes.functions.spendCoupledA2)
            fn = f(proof, root, nf, face, rec,
                   _ct_tuple(d["sigma"]["eEnc"]), fold)
        return self._send_from(fn, dep, f"Notes.spendCoupled{self.fx.flavor.upper()}",
                               event=EVENT_BY_FLAVOR[self.fx.flavor],
                               contract=self.notes)

    # -- one-shot -------------------------------------------------------------

    def run_lifecycle(self) -> Dict[str, Step]:
        """bind -> fund -> approve(s) -> mint -> spend; returns the named steps."""
        self.bind_identities()
        self.fund_issuer()
        # The mint escrow pull (issuer -> pool) and, for a private depositor,
        # the payout leg (pool -> depositor) need the identity-bound approve.
        self.approve_pool(self.fx.issuer, 2 * self.fx.face, "approve issuer->pool")
        self.approve_pool(self.fx.depositor, 0, "approve depositor->pool")
        mint = self.mint()
        spend = self.spend()
        return {"mint": mint, "spend": spend}

    def anchors(self, mint: Step, spend: Step) -> Dict[str, Dict[str, Any]]:
        """The receipt anchor dicts for :meth:`E2EFixture.build_receipt`."""
        return {
            "mint":  {"txhash": mint.txhash, "block": mint.block},
            "spend": {"txhash": spend.txhash, "block": spend.block,
                      "logindex": spend.logindex, "timestamp": spend.timestamp},
        }


__all__ = ["E2EFixture", "Party", "NotesStack", "Step"]
