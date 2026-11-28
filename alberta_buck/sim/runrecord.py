"""Run records: one directory per run, readable while it grows and after it ends.

    python -m alberta_buck.sim.runrecord import build/sim/economy-smoke-365.json [--runs build/runs]
    python -m alberta_buck.sim.runrecord list [--runs build/runs]

Every simulation, test world and sandbox session is meant to leave the same record, so one dashboard
(=python -m alberta_buck.sim.dashboard=) reads any of them, finished or still running. A run's directory
holds:

  manifest.json       what ran: schema, id, name, kind, engine, status, revision, scenario, tokens,
                      contracts, cast, units, files, capabilities, outputs; rewritten atomically
  frames.jsonl        one line per recorded day, grouped by ecosystem component (frame_record)
  events.jsonl        every household state-machine step (seq, member, kind, phase, status, reason...);
                      an event's =accounts= snapshot is written only when it differs from that household's
                      previous one (a reader carries the last one forward)
  agents.jsonl        one compact line per household per day (books, holdings, action, error)
  interventions.jsonl every shock or setting change applied, with its day and outcome

The files are append-only JSON Lines: a reader takes whole lines and ignores a partial last one, so a
growing run reads the same way as a finished one. Money is in raw units (1e-6 BUCK or USDC) as exact
integers; prices and ratios are floats; scaled chain values (1e36) are reduced to raw units. The
record is for observation. The exact accounting record of a run remains its vector (snapshot.write).

The JavaScript worlds (Tevm, in Node or in a browser tab) are to write the same records; the
frame's shape is pinned by alberta_buck/test/test_runrecord.py.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

SCHEMA                          = "buck-run/1"
RAW                             = 10**6
SCALE                           = 10**36
FILES                           = dict(frames="frames.jsonl", events="events.jsonl", agents="agents.jsonl",
                                       interventions="interventions.jsonl", wheel="wheel.jsonl")
UNITS                           = dict(money="raw integer, 1e-6 BUCK or USDC", price="float, USD or BUCK per unit",
                                       ratio="float", day="simulated day index from 0")
AGENT_FIELDS                    = ("role", "nw", "nw_buck", "hypo", "drawn", "limit", "basket", "basket_mark", "held_buck", "cash",
                                   "mortgage", "action", "error", "exited", "active", "relief_unavailable",
                                   # the lifecycle (household_policy.stage): present only for households with an age
                                   "age", "stage", "usd_fund", "usd_fund_paid")
MANIFEST_EVERY                  = 10        # days between manifest refreshes while running
MANIFEST_SECONDS                = 30        # ... or seconds, whichever comes first (a five-year day can take 30 s)


def default_root() -> Path:
    return Path(os.environ.get("SIM_RUNS") or "build/runs")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _ratio(wad):
    return None if wad is None else int(wad) / 1e18


def _price(raw):
    return None if raw is None else int(raw) / RAW


def _carry(row):
    """A Carrying account's net / nominal BUCK (1 is fresh; less as its carried fee grows)."""
    v = (row or {}).get("net_ratio_scaled")
    return None if v is None else int(v) / SCALE


def _scaled(value):
    """A 1e36-scaled chain value (often a decimal string) as raw units."""
    return None if value is None else int(value) // SCALE


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (bytes, bytearray)):
        return "0x" + bytes(o).hex()
    if isinstance(o, int) and not isinstance(o, bool) and abs(o) >= 2**53:
        return str(o)
    return o


def revision(root=None) -> dict | None:
    """The source revision a run used: commit and whether the tree had changes."""
    cwd = Path(root or os.environ.get("ALBERTA_BUCK_REPO") or Path(__file__).resolve().parents[2])
    try:
        commit = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=cwd, capture_output=True, text=True,
                                timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=cwd,
                                    capture_output=True, text=True, timeout=30).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None
    return dict(commit=commit, dirty=dirty) if commit else None


def _at(values, i):
    return values[i] if values and i < len(values) else None


def holders(net) -> dict | None:
    """Raw BUCK held by the Carrying accounts a frame's =net.carrying= records: the BUCK/USDC pool,
    the TOKEN/BUCK pools and the basket. Everyone else is supply less these."""
    carrying = (net or {}).get("carrying")
    if not carrying:
        return None
    whole = lambda row: _scaled((row or {}).get("nominal_scaled")) or 0
    return dict(dollar_pool=whole(carrying.get("buck_usdc")),
                token_pools=sum(whole(r) for r in carrying.get("buck_tokens") or []),
                basket=whole(carrying.get("basket")))


def frame_record(flat: dict, tokens) -> dict:
    """One recorded day, grouped by component, from a snapshot frame (Snapshotter.capture).

    Absent sources give None, never zero: a world without households has no =households= group, a
    legacy world no =net= reads. =health.faults= counts the day's dispatcher faults (kind=act events)."""
    net                         = flat.get("net") or {}
    hh                          = flat.get("households") or {}
    members                     = [a for a in flat.get("octl") or [] if "member" in a]
    events                      = flat.get("household_events") or []
    jubilee                     = net.get("jubilee") or {}
    supply                      = flat.get("supply")
    held                        = holders(net)
    if held is not None and supply is not None:
        held["others"]          = supply - sum(held.values())
    pop                         = flat.get("pop") or {}
    roles                       = {role: dict(arrived=v.get("arrived"), active=v.get("active"), exited=v.get("exited"))
                                   for role, v in pop.items() if isinstance(v, dict) and "arrived" in v}
    spots                       = zip(tokens, flat.get("refUsd") or [], flat.get("spotUsdc") or [], flat.get("spotBuck") or [])
    carried                     = (net.get("carrying") or {}).get("buck_tokens") or []
    weights                     = [dict(symbol=s, held_bp=_at(flat.get("eq_w"), i), target_bp=_at(flat.get("eq_lean"), i),
                                        declared_bp=_at(flat.get("eq_decl"), i))
                                   for i, s in enumerate(tokens)] if flat.get("eq_w") else None
    ownership = {"ownership": flat["ownership"]} if "ownership" in flat else {}
    treasury = {}
    if all(flat.get(k) is not None for k in ("eq_total_shares", "eq_treasury_shares", "eq_equity")):
        total, shares = int(flat["eq_total_shares"]), int(flat["eq_treasury_shares"])
        treasury["treasury"] = dict(shares=str(shares), total_shares=str(total),
                                    mark=shares * int(flat["eq_equity"]) // total if total else 0)
    position = jubilee.get("position") or {}
    startup = flat.get("startup")
    return _jsonable({
        "day": flat["day"],
        "block": net.get("block"),
        **({"wheel_revenue": flat["wheel_revenue"]} if "wheel_revenue" in flat else {}),
        **({"startup": {**startup, "share_price": _ratio(startup["share_price"])}} if startup is not None else {}),
        "control": dict(k=_ratio(flat.get("buckK")), bvib=_ratio(flat.get("basketVal")),
                        bvib_nominal=_ratio(flat.get("basketValNominal")),
                        bvib_usdc=_ratio(flat.get("eq_book_usdc")) or None,
                        pid=dict(p=flat.get("pid_p"), i=flat.get("pid_i"), d=flat.get("pid_d"))),
        "market": dict(buck_usd=_price(flat.get("buck_usd")),
                       tokens=[dict(symbol=s, ref_usd=_price(r), usdc=_price(u), buck=_price(b))
                               for s, r, u, b in spots],
                       pools=[dict(symbol=s, buck=_scaled(row.get("nominal_scaled")), net=_carry(row))
                              for s, row in zip(tokens, carried)] if carried else None,
                       dollar_pool_net=_carry((net.get("carrying") or {}).get("buck_usdc")),
                       book=dict(buck_bid=_price(flat.get("eq_book_buck_bid")) or None,
                                 buck_ask=_price(flat.get("eq_book_buck_ask")) or None,
                                 bids=[dict(symbol=s, buck=_price(row[0]), usdc=_price(row[1]) or None,
                                            via_usdc=bool(row[2]), via_buck=bool(row[3]))
                                       for s, row in zip(tokens, flat.get("eq_book_bids") or [])])
                       if flat.get("eq_book_bids") else None,
                       arb=dict(cycles=flat.get("cycleTrades"), direct=flat.get("directTrades"), ub=flat.get("ubTrades"))),
        "supply": dict(total=supply, active_principal=net.get("active_principal"),
                       deferred_principal=net.get("deferred_principal"), basket_lien=flat.get("eq_lien"),
                       household_principal=hh.get("drawn") if hh else None, holders=held),
        "basket": dict(**ownership, **treasury, equity=flat.get("eq_equity"), lien=flat.get("eq_lien"), nav=flat.get("basketNav"),
                       share_price=_ratio(flat.get("eq_share_price")), hold_index=flat.get("eq_hold"),
                       held_buck=flat.get("eq_held_buck"),
                       idle_buck=flat.get("eq_liquidity"), headroom=flat.get("eq_headroom"), mark=flat.get("eq_mark"),
                       unpaid_relief=flat.get("eq_unpaid_relief"),
                       token_exposure=flat.get("eq_token_exposure"), net_issued=flat.get("eq_net_issued"),
                       hard_limit=flat.get("eq_hard_limit"), ceiling=flat.get("eq_ceiling"),
                       carried_fee=flat.get("eq_carried_fee"), weights=weights,
                       wheel=dict(buy=flat.get("wh_eq_buy"), sell=flat.get("wh_eq_sell"), respond=flat.get("wh_eq_respond"),
                                  buys=flat.get("wh_eq_buy_n"), sells=flat.get("wh_eq_sell_n"),
                                  refresh=flat.get("wh_eq_refresh"), refreshes=flat.get("wh_eq_refresh_n"),
                                  refreshes_by_pool=[flat.get(f"wh_eq_refresh_{i}_n", 0) for i in range(len(tokens))]
                                  if "wh_eq_refresh_n" in flat else None,
                                  responds=flat.get("wh_eq_respond_n"))),
        "jubilee": dict(nominal=jubilee.get("nominal"), cash=jubilee.get("cash"),
                        position_mark=position.get("mark"),
                        **({"position_basis": position["basis"]} if "basis" in position else {}),
                        relief_available=_scaled(net.get("available_relief_scaled"))) if jubilee else None,
        "population": dict(roles=roles, gap=pop.get("gap"), k_factor=pop.get("k_factor"),
                           stages=pop.get("stages")) if pop else None,
        "households": dict(active=hh.get("active"), exited=hh.get("exited"), nw=hh.get("nw"),
                           nw_buck=hh.get("nw_buck"), hypo=hh.get("hypo"),
                           drawn=hh.get("drawn"), basket=hh.get("basket"), basket_mark=hh.get("basket_mark"),
                           cash=hh.get("cash"), mortgage=hh.get("mortgage"),
                           **{k: hh[k] for k in ("basket_at_risk", "basket_mtm") if k in hh}) if hh else None,
        "health": dict(errors=sum(bool(a.get("error")) for a in members) if members else None,
                       relief_unavailable=sum(bool(a.get("relief_unavailable")) for a in members) if members else None,
                       faults=sum(e.get("kind") == "act" for e in events), events=len(events)),
    })


def agent_records(flat: dict) -> list[dict]:
    """One compact line per household marked on this day."""
    day                         = flat["day"]
    return [_jsonable({"day": day, "member": a["member"], **{k: a.get(k) for k in AGENT_FIELDS if k in a}})
            for a in flat.get("octl") or [] if "member" in a]


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:80] or "run"


def verified_complete(root, run_id, scenario, price_digest, engine):
    """Reuse a finished workbench run only on the same resolved input tape."""
    path = root / run_id / "manifest.json"
    if not path.is_file():
        return False
    manifest = json.loads(path.read_text())
    if manifest["status"] != "complete":
        return False
    if (manifest["engine"] != engine or manifest["frames"] != scenario.days
            or manifest["scenario"]["seed"] != scenario.seed
            or (manifest.get("source") or {}).get("price_digest") != price_digest):
        raise ValueError(f"completed run {run_id} has different inputs")
    return True


class RunRecorder:
    """Writes one run's directory as the run goes.

    open(...) before the first day; frame(flat, applied=...) after each captured day; close(status)
    at the end. A failed run is closed with status "failed" and its error. A process killed mid-run
    (SIGKILL: the kernel's out-of-memory killer) cannot close its record: it stays "running", and
    runs() reads it as "killed" when its =host= is this machine and its =pid= is gone; from another
    machine, the page shows it stale once =updated= is old."""

    def __init__(self, root=None, name="run", *, run_id=None, kind="simulation", engine="pyrevm", live=None):
        stamp                   = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.root               = Path(root or default_root())
        self.chosen             = run_id is not None
        self.id                 = run_id or f"{_slug(name)}-{stamp}"
        self.dir                = self.root / self.id
        self.name, self.kind, self.engine = name, kind, engine
        self.live               = live          # a live session's control address, if it takes commands
        self.manifest: dict     = {}
        self._applied           = 0
        self._handles: dict     = {}
        self._accounts: dict    = {}        # each household's last written =accounts= snapshot
        self.days               = 0
        self._written           = 0.        # monotonic time of the last manifest write

    # --- writing ------------------------------------------------------------------------------- #
    def _append(self, key, rows):
        if not rows:
            return
        h = self._handles.get(key)
        if h is None:
            h = self._handles[key] = open(self.dir / FILES[key], "a", encoding="utf-8")
        h.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
        h.flush()

    def _write_manifest(self):
        self._written = time.monotonic()
        self.manifest["updated"] = _now()
        self.manifest["frames"] = self.days
        tmp = self.dir / "manifest.json.tmp"
        tmp.write_text(json.dumps(_jsonable(self.manifest), indent=1) + "\n")
        tmp.replace(self.dir / "manifest.json")

    def open(self, *, scenario=None, meta=None, tokens=(), live=None, source=None, settings=None):
        """Create the directory and the first manifest. =meta= is the snapshot's meta (contracts,
        experiment); =live= the control address of a session that accepts commands, if any (else the
        one given at construction). A generated
        id that is taken (cells of one grid starting in the same second) gets a counter; a chosen
        id that is taken is an error."""
        self.root.mkdir(parents=True, exist_ok=True)
        base, n = self.id, 1
        while True:
            try:
                self.dir.mkdir(exist_ok=False)
                break
            except FileExistsError:
                if self.chosen:
                    # A chosen id is a cell's name: an unfinished earlier attempt is kept beside it
                    # and the cell records afresh; a complete one is never replaced.
                    try:
                        earlier = json.loads((self.dir / "manifest.json").read_text()).get("status")
                    except (OSError, ValueError):
                        earlier = None
                    if earlier == "complete":
                        raise
                    k = 1
                    while (self.root / f"{base}-earlier-{k}").exists():
                        k += 1
                    self.dir.rename(self.root / f"{base}-earlier-{k}")
                    continue
                n += 1
                self.id = f"{base}-{n}"
                self.dir = self.root / self.id
        meta = meta or {}
        live = live or self.live
        run = meta.get("run") or {}
        contract = meta.get("buck_contract") or {}
        self.manifest = dict(
            schema=SCHEMA, id=self.id, name=self.name, kind=self.kind, engine=self.engine, status="running",
            started=_now(), finished=None, revision=revision(), host=socket.gethostname(), pid=os.getpid(),
            scenario=dict(name=(meta.get("experiment") or {}).get("name") or getattr(scenario, "name", run.get("scenario")),
                          seed=getattr(scenario, "seed", run.get("seed")),
                          days=getattr(scenario, "days", run.get("days")),
                          day_step=getattr(scenario, "day_step", run.get("day_step")),
                          ticks_per_day=getattr(scenario, "ticks_per_day", None),
                          basket=run.get("basket")),
            tokens=list(tokens),
            cast={k: n for k, n in (getattr(scenario, "agents", None) or run.get("agents") or {}).items() if n},
            contracts={name: r.get("address") for name, r in (contract.get("roles") or {}).items()}
            or ({"buck": contract["address"]} if contract.get("address") else {}),
            accounting=contract.get("accounting"), experiment=meta.get("experiment"), settings=settings,
            units=UNITS, files=FILES, source=source, outputs={}, error=None,
            capabilities=dict(live=bool(live), control=live, agents=True, events=True))
        self._write_manifest()
        return self

    def frame(self, flat: dict, applied=()):
        """Record one captured day: its frame, its events and marks, and interventions applied since."""
        day = flat["day"]
        self._append("wheel", [_jsonable({"day": day, **op}) for op in flat.get("wheel_operations") or []])
        self._append("frames", [frame_record(flat, self.manifest.get("tokens") or [])])
        self._append("events", [self._event(day, e) for e in flat.get("household_events") or []])
        self._append("agents", agent_records(flat))
        new = list(applied)[self._applied:]
        self._applied += len(new)
        self._append("interventions", [_jsonable({"day": iv.get("applied_day", day), "source": "scenario", **iv})
                                       for iv in new])
        self.days += 1
        if self.days % MANIFEST_EVERY == 0 or time.monotonic() - self._written >= MANIFEST_SECONDS:
            self._write_manifest()

    def _event(self, day, e: dict) -> dict:
        """One event line. Its =accounts= snapshot (63% of an event log's bytes, most of them repeating
        the household's previous event) is dropped when it equals the last one written for the same
        household, so a reader carries the last one forward."""
        row = _jsonable({"day": day, **e})
        accounts = row.get("accounts")
        if accounts is not None:
            key = (row.get("agent"), row.get("member"))
            if self._accounts.get(key) == accounts:
                del row["accounts"]
            else:
                self._accounts[key] = accounts
        return row

    def intervene(self, day, kind, args=None, *, source="operator", ok=True, note="", replay=True):
        """An action made outside the scenario's engine (a person steering a live session: pause,
        step, pace, reset, stop). replay=False marks one that changes no economics and is not exported."""
        self._append("interventions", [_jsonable(dict(day=day, action=kind, args=args or {}, source=source,
                                                      ok=ok, note=note, replay=replay))])

    def close(self, status="complete", *, outputs=None, error=None):
        for h in self._handles.values():
            h.close()
        self._handles.clear()
        self.manifest.update(status=status, finished=_now(), error=error and str(error)[:500])
        self.manifest["outputs"].update({k: str(v) for k, v in (outputs or {}).items() if v})
        self._write_manifest()


# --- reading ---------------------------------------------------------------------------------- #
def read_lines(path: Path, offset: int = 0) -> tuple[list[dict], int]:
    """Whole JSON lines from byte =offset=; returns them and the offset after the last whole line."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except FileNotFoundError:
        return [], offset
    end = data.rfind(b"\n") + 1
    rows = [json.loads(line) for line in data[:end].splitlines() if line.strip()]
    return rows, offset + end


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, TypeError, ValueError):
        return True             # someone else's process, or no pid: not provably gone
    return True


def liveness(manifest: dict) -> dict:
    """A "running" manifest whose process ran on this machine and is gone was killed: it could not
    close its record (SIGKILL, typically the kernel's out-of-memory killer). Read it as "killed"; the
    file is left as written. Records from other machines are left to the page's staleness test."""
    if (manifest.get("status") == "running" and manifest.get("pid") and manifest.get("host") == socket.gethostname()
            and not _alive(manifest["pid"])):
        return {**manifest, "status": "killed",
                "error": manifest.get("error") or f"process {manifest['pid']} ended without closing its record "
                                                  f"at day {manifest.get('frames', 0)} (SIGKILL: out of memory?)"}
    return manifest


def runs(root=None) -> list[dict]:
    """Every run under =root=, newest first: its manifest, or a note for an unreadable one."""
    out = []
    for m in Path(root or default_root()).glob("*/manifest.json"):
        try:
            out.append(liveness(json.loads(m.read_text())))
        except (OSError, ValueError) as e:
            out.append(dict(id=m.parent.name, status="unreadable", error=str(e)[:200]))
    return sorted(out, key=lambda r: r.get("started") or "", reverse=True)


# --- importing a finished vector ---------------------------------------------------------------- #
def import_vector(path, root=None, run_id=None) -> Path:
    """Write a run record from a completed (or checkpointed) snapshot vector, so earlier runs open in
    the dashboard. The vector stays the exact record; this is a derived view of it."""
    path = Path(path)
    vector = json.loads(path.read_text())
    meta = vector.get("meta") or {}
    run = meta.get("run") or {}
    rec = RunRecorder(root, path.stem, run_id=run_id or _slug(path.stem), engine="pyrevm")
    old = rec.dir / "manifest.json"
    if old.exists():
        # Re-importing the same vector replaces its derived view, as does importing the vector a run
        # wrote beside its own record (a newer reader of the same frames); anything else is left alone.
        earlier = json.loads(old.read_text())
        if str(path) not in ((earlier.get("source") or {}).get("vector"), (earlier.get("outputs") or {}).get("vector")):
            raise FileExistsError(f"{rec.dir} holds another run; pass run_id")
        if earlier.get("status") == "running":
            raise FileExistsError(f"{rec.dir} is still running")
        for f in rec.dir.iterdir():
            f.unlink()
        rec.dir.rmdir()
    rec.open(meta=meta, tokens=vector.get("tokens") or [], source=dict(vector=str(path)))
    rec.manifest["scenario"]["name"] = (meta.get("experiment") or {}).get("name") or run.get("scenario") or path.stem
    # The vector does not record its revision or run times; the file's time stands in for both.
    written = datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc).isoformat(timespec="seconds")
    rec.manifest.update(revision=None, started=written, imported=_now())
    applied = meta.get("interventions_applied") or []
    for flat in vector.get("frames") or []:
        rec.frame(flat, applied=[iv for iv in applied if iv.get("applied_day", 0) <= flat["day"]])
    rec.close("complete" if run.get("completed", True) else "incomplete", outputs=dict(vector=path))
    rec.manifest["finished"] = written
    rec._write_manifest()
    return rec.dir


# --- a long run, year by year --------------------------------------------------------------------- #
def years(directory, every=365, tail=730) -> dict:
    """One record's state at the end of each year (and its last day), with the means of its last
    =tail= days: what a five-year suite's table reads (doc/review/bucknet-economy-implementation.org)."""
    directory = Path(directory)
    manifest = liveness(json.loads((directory / "manifest.json").read_text()))
    frames, _ = read_lines(directory / "frames.jsonl")
    if not frames:
        return dict(id=directory.name, status=manifest.get("status"), rows=[], tail=None)

    def row(f):
        c, m, s, b = f.get("control") or {}, f.get("market") or {}, f.get("supply") or {}, f.get("basket") or {}
        h, pop = f.get("households") or {}, f.get("population") or {}
        pools = [p["net"] for p in m.get("pools") or [] if p.get("net") is not None]
        book = m.get("book") or {}
        bid, ask = book.get("buck_bid"), book.get("buck_ask")
        stages = pop.get("stages") or {}
        return dict(day=f["day"], k=c.get("k"), bvib=c.get("bvib"), bvib_nominal=c.get("bvib_nominal"),
                    pool_net=sum(pools) / len(pools) if pools else None, dollar_pool_net=m.get("dollar_pool_net"),
                    spread=(ask / bid - 1) if bid and ask else None, buck_usd=m.get("buck_usd"),
                    supply=(s.get("total") or 0) / RAW, lien=(s.get("basket_lien") or 0) / RAW,
                    households_principal=(s.get("household_principal") or 0) / RAW,
                    unpaid_relief=(b.get("unpaid_relief") or 0) / RAW, active=h.get("active"), exited=h.get("exited"),
                    young=stages.get("young"), middle=stages.get("middle"), retired=stages.get("retired"),
                    nw_vs_usd=((h.get("nw") or 0) - (h.get("hypo") or 0)) / RAW,
                    errors=(f.get("health") or {}).get("errors"), faults=(f.get("health") or {}).get("faults"))

    last = frames[-1]
    rows = [row(f) for f in frames if f["day"] % every == every - 1]
    if not rows or rows[-1]["day"] != last["day"]:
        rows.append(row(last))
    window = [f for f in frames if f["day"] > last["day"] - tail]
    bvib = [f["control"]["bvib"] for f in window if (f.get("control") or {}).get("bvib") is not None]
    ks = [f["control"]["k"] for f in window if (f.get("control") or {}).get("k") is not None]
    return dict(id=directory.name, status=manifest.get("status"), commit=(manifest.get("revision") or {}).get("commit"),
                rows=rows, tail=dict(days=len(window), bvib_dev=sum(abs(v - 1) for v in bvib) / len(bvib) if bvib else None,
                                     bvib_above=sum(v > 1 for v in bvib) / len(bvib) if bvib else None,
                                     k_mean=sum(ks) / len(ks) if ks else None,
                                     k_low=min(ks) if ks else None, k_high=max(ks) if ks else None))


def _years_table(y) -> str:
    out = [f"{y['id']}  ({y['status']}, {y.get('commit') or 'no commit'})"]
    out.append(f"  {'day':>4} {'K':>6} {'BVIB':>7} {'quoted':>7} {'pools':>6} {'$pool':>6} {'spread':>6} {'BUCK$':>6} "
               f"{'supplyM':>8} {'lienM':>7} {'hhM':>6} {'act':>4} {'exit':>4} {'y/m/r':>11} {'vsUSD M':>8} {'err':>3}")
    f = lambda v, fmt: format(v, fmt) if v is not None else "-"
    for r in y["rows"]:
        stages = "/".join(str(r[k]) if r[k] is not None else "-" for k in ("young", "middle", "retired"))
        out.append(f"  {r['day']:>4} {f(r['k'], '6.3f')} {f(r['bvib'], '7.4f')} {f(r['bvib_nominal'], '7.4f')} "
                   f"{f(r['pool_net'], '6.4f')} {f(r['dollar_pool_net'], '6.4f')} "
                   f"{f(r['spread'] and 100 * r['spread'], '5.2f')}% {f(r['buck_usd'], '6.3f')} "
                   f"{r['supply'] / 1e6:8.2f} {r['lien'] / 1e6:7.2f} {r['households_principal'] / 1e6:6.2f} "
                   f"{f(r['active'], '>4')} {f(r['exited'], '>4')} {stages:>11} {r['nw_vs_usd'] / 1e6:8.2f} "
                   f"{f(r['errors'], '>3')}")
    t = y["tail"]
    if t and t["bvib_dev"] is not None:
        out.append(f"  last {t['days']} days: mean |BVIB - 1| {100 * t['bvib_dev']:.2f}%, BVIB above 1 on "
                   f"{100 * t['bvib_above']:.0f}% of days, K {t['k_low']:.3f} to {t['k_high']:.3f} (mean {t['k_mean']:.3f})")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m alberta_buck.sim.runrecord", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    im = sub.add_parser("import", help="write run records from snapshot vectors")
    im.add_argument("vectors", nargs="+")
    im.add_argument("--runs", default=None, help="runs directory (default $SIM_RUNS or build/runs)")
    ls = sub.add_parser("list", help="list recorded runs")
    ls.add_argument("--runs", default=None)
    yr = sub.add_parser("years", help="each record's state at every year's end, and its last two years' means")
    yr.add_argument("records", nargs="+", help="run directories (build/runs/<id>)")
    yr.add_argument("--json", action="store_true", help="print JSON instead of tables")
    a = ap.parse_args(argv)
    if a.cmd == "import":
        for v in a.vectors:
            print(import_vector(v, a.runs))
        return 0
    if a.cmd == "years":
        got = [years(r) for r in a.records]
        print(json.dumps(got, indent=1) if a.json else "\n".join(_years_table(y) for y in got))
        return 0
    for r in runs(a.runs):
        print(f"{r.get('id'):50s} {r.get('status', ''):10s} {r.get('frames', 0):5} days  {r.get('started', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
