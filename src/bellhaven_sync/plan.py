"""Turn (website locations, CRM snapshot) into reviewable proposals.

Each proposal is one logical change (re-parent, rename, address fix, CHOW, create, retire a duplicate,
flag) with the evidence behind it. Proposals are generated only where the CRM differs from the
desired state, so applied changes go quiet by themselves. The proposal key hashes the subject, the
current values and the desired values: a decision is remembered for exactly that situation, and a
genuinely new situation (the website changed again, someone edited the CRM) is reviewed again.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date

from .match import Candidate, broken_pointers, candidates_for, is_superseded, same_address_accounts
from .normalize import canon, is_po_box, norm_name, norm_street, norm_text, same, zip5
from .website import Location

_NAME_NOISE = {"the", "of", "at", "and"}
IDENTITY_FIELDS = ("name", "billing_street", "billing_city", "billing_state", "billing_zip")
GUARD_FIELDS = IDENTITY_FIELDS + ("parent_id", "lifetime_revenue", "outstanding_ar", "status",
                                  "duplicate_of_account", "chow_current_account")

KIND_LABEL = {
    "CHOW": "Change of ownership (billing SOP)",
    "REPARENT": "Move to the correct parent",
    "CONSOLIDATE": "Move to the correct parent and retire copies",
    "RENAME": "Rename to the website name",
    "ADDRESS": "Fix address fields",
    "REACTIVATE": "Reactivate (listed on the website)",
    "CREATE": "New location - create account",
    "DUPLICATE": "Retire duplicate copy",
    "NOT_ON_SITE": "Under Bellhaven but not on the website",
    "CONFLICT": "Conflict - needs a human",
}
KIND_ORDER = list(KIND_LABEL)


@dataclass
class Op:
    action: str                      # "patch" | "create"
    account_id: str | None           # patch target; None for create
    fields: dict                     # desired values; "$new" = id created by an earlier op in this proposal
    old: dict = field(default_factory=dict)        # current values of `fields` (what the reviewer saw)
    guard: dict = field(default_factory=dict)      # other fields of the target the decision relied on
    requires: list = field(default_factory=list)   # [{account_id, fields}] other records that must still look like this
    append_note: str = ""            # appended to the record's note (never overwrites a rep's note)
    ref: str | None = None           # create: reference name ("$new")
    known_occupants: list = field(default_factory=list)  # create: accounts at the facility when planned (anything else = stale)
    probe: dict = field(default_factory=dict)            # create: identity used to check nobody created it meanwhile (incl. phone)


@dataclass
class Proposal:
    kind: str
    title: str
    summary: str
    ops: list[Op]
    evidence: dict
    confidence: str                  # high | medium
    tags: list[str] = field(default_factory=list)
    location_slug: str | None = None
    key: str = ""
    labels: dict = field(default_factory=dict)

    def compute_key(self) -> str:
        # CONSOLIDATE is decided by its survivor move: rejecting it must stick even if one copy is later
        # resolved by hand (which would change the bundle). Everything else: all ops.
        key_ops = self.ops[:1] if self.kind == "CONSOLIDATE" else self.ops
        # a parent move is the same decision whether it is shown alone (REPARENT) or with copies (CONSOLIDATE)
        key_kind = "MOVE" if self.kind in ("REPARENT", "CONSOLIDATE") else self.kind
        canon_ops = [{"a": o.action, "id": o.account_id,
                      "to": {k: canon(k, v) for k, v in sorted(o.fields.items()) if k != "note"},
                      "from": {k: canon(k, v) for k, v in sorted(o.old.items()) if k != "note"}} for o in key_ops]
        blob = json.dumps({"kind": key_kind, "slug": self.location_slug, "ops": canon_ops}, sort_keys=True, default=str)
        self.key = hashlib.sha256(blob.encode()).hexdigest()[:16]
        return self.key

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind_label"] = KIND_LABEL[self.kind]
        return d


def names_equivalent(a: str, b: str) -> bool:
    ta = [w for w in norm_name(a).split() if w not in _NAME_NOISE]
    tb = [w for w in norm_name(b).split() if w not in _NAME_NOISE]
    return ta == tb


def _money(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def has_billing_lock(acc: dict, cfg: dict) -> bool:
    """SOP: revenue history AND outstanding AR > 0 -> the account may not be re-parented."""
    return _money(acc.get(cfg["sop"]["revenue_field"])) > 0 and _money(acc.get(cfg["sop"]["ar_field"])) > 0


def sop_verdict(acc: dict, cfg: dict) -> str:
    rev, ar = _money(acc.get(cfg["sop"]["revenue_field"])), _money(acc.get(cfg["sop"]["ar_field"]))
    if rev > 0 and ar > 0:
        return (f"lifetime_revenue={rev:,.0f} > 0 AND outstanding_ar={ar:,.0f} > 0 -> billing keeps the old account: "
                f"do NOT re-parent it; the current record lives under the new parent and the old one points to it "
                f"via chow_current_account")
    return f"lifetime_revenue={rev:,.0f}, outstanding_ar={ar:,.0f} -> not both > 0 -> re-parent the existing account directly"


def _guard(acc: dict, exclude=()) -> dict:
    return {k: acc.get(k, "") for k in GUARD_FIELDS if k not in exclude}


def _live_requirement(acc: dict, parent_id: str | None = None) -> dict:
    """The record we point at must still be the same, live, Active facility record (and, when given, under that parent)."""
    fields = {"duplicate_of_account": "", "chow_current_account": "", "status": "Active",
              **{k: acc.get(k, "") for k in ("billing_street", "billing_city", "billing_state", "billing_zip")}}
    if parent_id:
        fields["parent_id"] = parent_id
    return {"account_id": acc["account_id"], "fields": fields}


def _brief(acc: dict) -> dict:
    keep = ("account_id", "name", "parent_id", "parent_name", "billing_street", "billing_city", "billing_state",
            "billing_zip", "care_type", "status", "phone", "lifetime_revenue", "outstanding_ar",
            "chow_current_account", "duplicate_of_account", "note")
    return {k: acc.get(k, "") for k in keep}


def location_problems(loc: Location) -> list[str]:
    missing = [f for f in ("name", "street", "city", "state", "zip") if not getattr(loc, f)]
    out = [f"missing {', '.join(missing)}"] if missing else []
    if loc.zip and not zip5(loc.zip):
        out.append(f"bad zip {loc.zip!r}")
    if not loc.care_offerings:
        out.append("no care offerings")
    return out


class Planner:
    SURVIVOR_RULES = ["already under Bellhaven", "not Inactive", "has billing history (revenue or AR)",
                      "corroborated by the website phone or administrator contact",
                      "under a documented predecessor group (acquired by Bellhaven)", "exact address match",
                      "address typed exactly as on the website", "lowest account id (tie-break)"]

    def __init__(self, cfg: dict, accounts: list[dict], contacts: list[dict], today: date | None = None):
        self.cfg = cfg
        self.accounts = accounts
        self.by_id = {a["account_id"]: a for a in accounts}
        self.contacts_by_acc: dict[str, list[dict]] = {}
        for c in contacts:
            self.contacts_by_acc.setdefault(c["account_id"], []).append(c)
        self.today = (today or date.today()).isoformat()
        op = cfg["operator"]
        self.parent_id = self._parent_id(op["parent_account_name"])
        if not self.parent_id:
            raise RuntimeError(f"Parent account {op['parent_account_name']!r} not found (or not unique) in the CRM")
        self.predecessor_ids = {p for p in (self._parent_id(n) for n in op.get("predecessor_parents", [])) if p}
        self.prop = cfg["propose"]

    # ------------------------------------------------------------------ helpers
    def _parent_id(self, name: str) -> str | None:
        hits = [a["account_id"] for a in self.accounts if a["name"] == name]
        return hits[0] if len(hits) == 1 else None

    def _pname(self, pid: str) -> str:
        return self.by_id[pid]["name"].replace(" (Parent Account)", "") if pid in self.by_id else "(no parent)"

    def _lock(self, acc: dict) -> bool:
        return has_billing_lock(acc, self.cfg)

    def _website_fields(self, loc: Location) -> dict:
        cmap = self.cfg["care_type_map"]   # priority = order in config, not order on the page
        primary = next((v for k, v in cmap.items() if k in loc.care_offerings), "")
        return {"name": loc.name, "billing_street": loc.street, "billing_city": loc.city,
                "billing_state": loc.state, "billing_zip": loc.zip, "care_type": primary}

    def _survivor_key(self, c: Candidate, loc: Location):
        a = c.account
        contacts = self.contacts_by_acc.get(a["account_id"], [])
        admin = bool(loc.administrator) and any(x["name"].strip().lower() == loc.administrator.strip().lower() for x in contacts)
        return (
            0 if a.get("parent_id") == self.parent_id else 1,
            0 if a.get("status") != "Inactive" else 1,      # a record a person flagged Needs Review is still live
            0 if _money(a.get("lifetime_revenue")) > 0 or _money(a.get("outstanding_ar")) > 0 else 1,
            0 if (c.signals.get("phone_match") or admin) else 1,
            0 if a.get("parent_id") in self.predecessor_ids else 1,
            0 if c.signals.get("street_exact") else 1,
            0 if norm_text(a.get("billing_street")) == norm_text(loc.street) else 1,
            a["account_id"],
        )

    def _why_kept(self, current: Candidate, other: Candidate, loc: Location) -> str:
        a, b = self._survivor_key(current, loc), self._survivor_key(other, loc)
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return self.SURVIVOR_RULES[i]
        return self.SURVIVOR_RULES[-1]

    def _ev(self, loc: Location | None, acc: dict | None, c: Candidate | None = None, **extra) -> dict:
        ev = {}
        if loc is not None:
            ev["website"] = loc.to_dict()
        if acc is not None:
            ev["crm"] = _brief(acc)
            ev["contacts"] = self.contacts_by_acc.get(acc["account_id"], [])
            if is_po_box(acc.get("billing_street")):
                ev["po_box_note"] = (f"CRM billing street is {acc.get('billing_street')!r}; kept as is (a PO Box is a valid "
                                     f"billing address).")
        if c is not None:
            ev["match"] = {"method": c.method, "score": c.score, **c.signals}
        ev.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
        return ev

    @staticmethod
    def _conf(*cands: Candidate) -> str:
        return "high" if all(c.method == "address" for c in cands) else "medium"

    def _is_noop(self, p: Proposal) -> bool:
        """Fields already hold the desired values (and, for conflicts, the explanation is already in the note)."""
        if any(o.action == "create" for o in p.ops):
            return False
        for o in p.ops:
            cur = self.by_id.get(o.account_id, {})
            if any(canon(k, cur.get(k)) != canon(k, v) for k, v in o.fields.items()):
                return False
            if p.kind == "CONFLICT" and o.append_note and o.append_note not in (cur.get("note") or ""):
                return False
        return True

    # ------------------------------------------------------------------ main entry
    def plan(self, locations: list[Location], locked_ids: set[str] | None = None,
             locked_slugs: set[str] | None = None, allow_absence: bool = True) -> tuple[list[Proposal], dict]:
        """locked_*: records/locations with an approved-but-unfinished write; left alone until it completes.
        allow_absence: False when the crawl looked incomplete - then nothing is concluded from absence."""
        locked_ids, locked_slugs = locked_ids or set(), locked_slugs or set()
        live = [a for a in self.accounts if not is_superseded(a, self.by_id)]
        report = {"rows": [], "quarantined": [], "locked": [], "absence_checks": allow_absence, "deferred": []}
        proposals: list[Proposal] = []

        valid, cands_by_slug, claims = [], {}, {}
        quarantine_held: set[str] = set()
        for loc in locations:
            probs = location_problems(loc)
            if probs:
                # no proposals from an incomplete page - but its accounts are still "on the website", so they
                # must never be flagged as missing from it
                held = [c.account["account_id"] for c in candidates_for(loc, live, self.cfg, lenient=True)]
                report["quarantined"].append({"slug": loc.slug, "problems": probs, "accounts": held})
                report["rows"].append({"location": loc.name or loc.slug, "outcome": "quarantined", "accounts": held})
                quarantine_held.update(held)
                continue
            valid.append(loc)
            cands_by_slug[loc.slug] = candidates_for(loc, live, self.cfg)
            for c in cands_by_slug[loc.slug]:
                claims.setdefault(c.account["account_id"], []).append(loc)
        matched_ids = set(claims) | quarantine_held
        if report["quarantined"] and allow_absence:
            # a page we couldn't read completely might be the one that lists an account - draw no absence conclusions
            allow_absence = False
            report["absence_checks"] = False

        conflicted = {l.slug for locs in claims.values() if len(locs) > 1 for l in locs}
        for aid, locs in claims.items():
            if len(locs) > 1 and aid not in locked_ids:
                proposals.append(self._conflict(self.by_id[aid], locs))

        for loc in valid:
            cands = cands_by_slug[loc.slug]
            ids = [c.account["account_id"] for c in cands]
            if loc.slug in locked_slugs or set(ids) & locked_ids:
                report["locked"].append(loc.slug)
                report["rows"].append({"location": loc.name, "outcome": "locked", "accounts": ids})
                continue
            if loc.slug in conflicted:
                report["rows"].append({"location": loc.name, "outcome": "conflict", "accounts": ids})
                continue
            if not cands:
                proposals.append(self._create(loc))
                report["rows"].append({"location": loc.name, "outcome": "create", "accounts": []})
                continue
            out = self._resolve_group(loc, cands, report)
            proposals.extend(out)
            report["rows"].append({"location": loc.name, "accounts": ids,
                                   "outcome": ",".join(sorted({p.kind.lower() for p in out})) or "confirmed"})

        for acc in live:
            if acc.get("parent_id") != self.parent_id or acc["account_id"] in matched_ids:
                continue
            if not allow_absence or acc["account_id"] in locked_ids or acc.get("status") == "Inactive":
                report["rows"].append({"location": None, "outcome": "not_on_site(skipped)", "accounts": [acc["account_id"]]})
                continue
            p = self._not_on_site(acc, live)
            if p:
                proposals.append(p)
            report["rows"].append({"location": None, "accounts": [acc["account_id"]],
                                   "outcome": f"not_on_site:{p.kind.lower() if p else 'already flagged'}"})

        # a duplicate that points at a live survivor but is still Active was only half retired
        for acc in self.accounts:
            if acc.get("duplicate_of_account") and is_superseded(acc, self.by_id) and acc.get("status") != "Inactive" \
                    and acc["account_id"] not in locked_ids:
                term = self.by_id[acc["duplicate_of_account"]]
                while term.get("duplicate_of_account") and term["duplicate_of_account"] in self.by_id:
                    term = self.by_id[term["duplicate_of_account"]]       # chain is cycle-free (is_superseded checked)
                req = {"account_id": term["account_id"],
                       "fields": {"duplicate_of_account": "", "chow_current_account": ""}}
                proposals.append(Proposal(
                    kind="DUPLICATE", title=f"Finish retiring duplicate {acc['name']}",
                    summary=(f"Marked as a duplicate of {acc['duplicate_of_account']} but its status is {acc.get('status')!r}; "
                             f"duplicates must be Inactive. Current record: {term['account_id']}."),
                    ops=[Op("patch", acc["account_id"], {"status": "Inactive"}, old={"status": acc.get("status", "")},
                            guard=_guard(acc, ("status",)), requires=[req])],
                    evidence={"crm": _brief(acc), "survivor": _brief(term)},
                    confidence="high", tags=["duplicate", "repair"]))
        # Never queue a change that is already in place (e.g. a CONFLICT on a record that is already Needs Review).
        proposals = [p for p in proposals if not self._is_noop(p)]
        # Proposals are decided independently. A guard must not include a field that a sibling proposal
        # changes on the same record (e.g. Zanesville's rename must not go stale because its re-parent landed).
        # A guarded field that a sibling proposal changes may hold the reviewed value OR the sibling's desired
        # value - nothing else (e.g. Zanesville's rename must not go stale because its re-parent landed, but it
        # must go stale if someone renames the record to something else).
        sibling: dict[str, dict[str, list]] = {}        # account -> field -> [(proposal index, value)]
        for n, p in enumerate(proposals):
            for o in p.ops:
                if o.account_id:
                    for k, v in o.fields.items():
                        if not (isinstance(v, str) and v.startswith("$")):
                            sibling.setdefault(o.account_id, {}).setdefault(k, []).append((n, v))

        def others(n, aid, k, exp):                      # values OTHER proposals may legitimately set
            return [v for m, v in sibling.get(aid, {}).get(k, []) if m != n and not same(k, v, exp)]

        for n, p in enumerate(proposals):
            for o in p.ops:
                for k in list(o.guard):
                    alts = others(n, o.account_id, k, o.guard[k])
                    if alts:
                        o.guard[k] = {"any_of": [o.guard[k], *alts]}
                for req in o.requires:                  # same for records we depend on (e.g. a survivor's address fix)
                    for k in list(req["fields"]):
                        exp = req["fields"][k]
                        if isinstance(exp, dict):
                            continue
                        alts = others(n, req["account_id"], k, exp)
                        if alts:
                            req["fields"][k] = {"any_of": [exp, *alts]}
        for p in proposals:
            p.compute_key()
            ids = set()
            for o in p.ops:
                ids.add(o.account_id)
                ids.update(v for v in list(o.fields.values()) + list(o.old.values()) if isinstance(v, str))
                ids.update(r["account_id"] for r in o.requires)
            p.labels = {i: self.by_id[i]["name"].replace(" (Parent Account)", "") for i in ids if i in self.by_id}
        report["counts"] = {}
        for p in proposals:
            report["counts"][p.kind] = report["counts"].get(p.kind, 0) + 1
        return proposals, report

    # ------------------------------------------------------------------ a location with one or more CRM records
    def _resolve_group(self, loc: Location, cands: list[Candidate], report: dict) -> list[Proposal]:
        bh = [c for c in cands if c.account.get("parent_id") == self.parent_id and c.account.get("status") != "Inactive"]
        current = sorted(bh or cands, key=lambda c: self._survivor_key(c, loc))[0]
        others = [c for c in cands if c is not current]
        acc = current.account
        group = [c.account["account_id"] for c in cands]
        out: list[Proposal] = []

        if acc.get("parent_id") != self.parent_id:
            if self._lock(acc):
                out.append(self._chow_create(loc, current, group))
                if others:
                    report["deferred"].append({"location": loc.slug, "why": "copies are retired once the CHOW is applied",
                                               "accounts": [c.account["account_id"] for c in others]})
                return out                      # the old record must stay exactly as it is
            if others and acc.get("status") == "Active":
                out.append(self._consolidate(loc, current, others, group))
            else:
                out.append(self._reparent(loc, current))
                if others:
                    report["deferred"].append({"location": loc.slug, "why": "copies are retired once the surviving record is reactivated",
                                               "accounts": [c.account["account_id"] for c in others]})
        elif acc.get("status") != "Active" and others:
            report["deferred"].append({"location": loc.slug, "why": "copies are retired once the surviving record is reactivated",
                                       "accounts": [c.account["account_id"] for c in others]})
        else:
            out.extend(self._retire(loc, o, current, group) for o in others)
        out.extend(self._field_fixes(loc, current))
        return out

    def _field_fixes(self, loc: Location, c: Candidate) -> list[Proposal]:
        acc, aid, out = c.account, c.account["account_id"], []
        if self.prop.get("name") and (acc.get("name") or "").strip() != loc.name.strip():
            cosmetic = names_equivalent(acc.get("name", ""), loc.name)
            if not cosmetic or self.prop.get("cosmetic_name"):
                weak = not cosmetic and c.signals.get("name_score", 100) < 60 and not c.signals.get("phone_match")
                out.append(Proposal(
                    kind="RENAME", title=f"Rename {acc['name']} -> {loc.name}",
                    summary=("Same name, different formatting; align with the website." if cosmetic else
                             "The CRM still uses an old name; the website lists this address under a new name."),
                    ops=[Op("patch", aid, {"name": loc.name}, old={"name": acc.get("name", "")}, guard=_guard(acc, ("name",)))],
                    evidence=self._ev(loc, acc, c, rename_warning="Names share little - the match rests on the address; "
                                                                  "check it is the same facility." if weak else None,
                                      warning=(f"This record is still under {self._pname(acc.get('parent_id'))}; moving it to "
                                               f"Bellhaven is a separate proposal. If you reject that move, consider rejecting "
                                               f"this rename too.") if acc.get("parent_id") != self.parent_id else None),
                    confidence="medium" if weak else self._conf(c),
                    tags=["formatting only"] if cosmetic else ["rebrand"], location_slug=loc.slug))
        if self.prop.get("address"):
            street = acc.get("billing_street") or ""
            fixes = []
            if not is_po_box(street) and norm_street(street) != norm_street(loc.street):
                fixes.append(("billing_street", loc.street))
            if (acc.get("billing_city") or "").strip().lower() != loc.city.lower():
                fixes.append(("billing_city", loc.city))
            if self.prop.get("zip") and zip5(acc.get("billing_zip")) != zip5(loc.zip):
                fixes.append(("billing_zip", loc.zip))
            for f, v in fixes:                      # one proposal per field: each decision stands on its own
                label = f.replace("billing_", "")
                out.append(Proposal(
                    kind="ADDRESS", title=f"Fix {label} of {acc['name']}",
                    summary=f"Matched by {c.method} (score {c.score}); the website {label} is {v!r}, the CRM has {acc.get(f, '')!r}.",
                    ops=[Op("patch", aid, {f: v}, old={f: acc.get(f, "")}, guard=_guard(acc, (f,)))],
                    evidence=self._ev(loc, acc, c), confidence=self._conf(c), tags=[label], location_slug=loc.slug))
        broken = broken_pointers(acc, self.by_id)
        if broken:
            clear = {k: "" for k in broken}
            out.append(Proposal(
                kind="CONFLICT", title=f"Clear broken link on {acc['name']}",
                summary=(f"This is the current record for {loc.name}, but {', '.join(broken)} points at "
                         f"{', '.join(broken.values())}, which is missing, itself, or no longer a live record. Clear it."),
                ops=[Op("patch", aid, clear, old=dict(broken), guard=_guard(acc, tuple(clear)),
                        append_note=f"{self.today}: cleared broken link(s) {broken}.")],
                evidence=self._ev(loc, acc, c), confidence="medium", tags=["broken link"], location_slug=loc.slug))
        if acc.get("status") != "Active":
            out.append(Proposal(
                kind="REACTIVATE", title=f"Reactivate {acc['name']}",
                summary=f"Status is {acc.get('status')!r} but the website lists this location.",
                ops=[Op("patch", aid, {"status": "Active"}, old={"status": acc.get("status", "")}, guard=_guard(acc, ("status",)),
                        append_note=f"{self.today}: listed on the Bellhaven website ({loc.url}).")],
                evidence=self._ev(loc, acc, c), confidence=self._conf(c), tags=["status"], location_slug=loc.slug))
        return out

    # ------------------------------------------------------------------ builders
    def _reparent(self, loc: Location, c: Candidate) -> Proposal:
        acc = c.account
        return Proposal(
            kind="REPARENT", title=f"Move {acc['name']}: {self._pname(acc.get('parent_id'))} -> Bellhaven",
            summary=f"The website lists this location as Bellhaven; the CRM has it under {self._pname(acc.get('parent_id'))}.",
            ops=[Op("patch", acc["account_id"], {"parent_id": self.parent_id}, old={"parent_id": acc.get("parent_id", "")},
                    guard=_guard(acc, ("parent_id",)))],
            evidence=self._ev(loc, acc, c, sop=sop_verdict(acc, self.cfg)), confidence=self._conf(c),
            tags=["re-parent"], location_slug=loc.slug)

    def _retire_op(self, o: Candidate, current_id: str, current_acc: dict | None, parent_id: str | None = None) -> tuple[Op, str]:
        acc = o.account
        req = [_live_requirement(current_acc, parent_id)] if current_acc else []
        if self._lock(acc) and acc.get("parent_id") != self.parent_id:
            # a billing-locked copy under another parent is an ownership-history record, not a plain duplicate
            return Op("patch", acc["account_id"], {"chow_current_account": current_id},
                      old={"chow_current_account": acc.get("chow_current_account", "")},
                      guard=_guard(acc, ("chow_current_account",)), requires=req), "chow-link"
        return Op("patch", acc["account_id"], {"duplicate_of_account": current_id, "status": "Inactive"},
                  old={"duplicate_of_account": acc.get("duplicate_of_account", ""), "status": acc.get("status", "")},
                  guard=_guard(acc, ("duplicate_of_account", "status")), requires=req,
                  append_note=f"{self.today}: duplicate of {current_id} (same address)."), "duplicate"

    def _retire(self, loc: Location, o: Candidate, current: Candidate, group: list[str]) -> Proposal:
        op, how = self._retire_op(o, current.account["account_id"], current.account, self.parent_id)
        why = self._why_kept(current, o, loc)
        ev = self._ev(loc, o.account, o, survivor=_brief(current.account), survivor_reason=why, duplicate_group=group)
        if how == "chow-link":
            ev["sop"] = sop_verdict(o.account, self.cfg)
            return Proposal(
                kind="CHOW", title=f"CHOW link {o.account['name']} -> {current.account['name']}",
                summary=(f"{o.account['name']} ({self._pname(o.account.get('parent_id'))}) is an older record of this location with "
                         f"billing history and open AR. It stays exactly as it is and points to the current Bellhaven record."),
                ops=[op], evidence=ev, confidence=self._conf(o, current), tags=["chow", "billing-sop"], location_slug=loc.slug)
        if self._lock(o.account):
            ev["warning"] = "This copy has revenue and open AR - check with billing before retiring it."
        return Proposal(
            kind="DUPLICATE", title=f"Retire duplicate {o.account['name']} (keep {current.account['name']})",
            summary=(f"{len(group)} CRM records sit at {loc.street}, {loc.city} {loc.state}. Keep {current.account['account_id']} "
                     f"({why}); mark this copy Inactive with duplicate_of_account -> {current.account['account_id']}."),
            ops=[op], evidence=ev, confidence=self._conf(o, current), tags=["duplicate"], location_slug=loc.slug)

    def _consolidate(self, loc: Location, current: Candidate, others: list[Candidate], group: list[str]) -> Proposal:
        acc = current.account
        ops = [Op("patch", acc["account_id"], {"parent_id": self.parent_id}, old={"parent_id": acc.get("parent_id", "")},
                  guard=_guard(acc, ("parent_id",)))]
        reasons = {}
        for o in others:
            op, how = self._retire_op(o, acc["account_id"], acc)
            ops.append(op)
            reasons[o.account["account_id"]] = f"{how}: kept {acc['account_id']} because it is {self._why_kept(current, o, loc)}"
        n = len(others)
        return Proposal(
            kind="CONSOLIDATE", title=f"Consolidate {loc.name}: move {acc['name']} to Bellhaven, retire {n} cop{'y' if n == 1 else 'ies'}",
            summary=(f"{len(group)} CRM records sit at {loc.street}, {loc.city} {loc.state}, none under Bellhaven. Keep "
                     f"{acc['account_id']} and move it to Bellhaven; retire the others. One decision, so copies are never "
                     f"retired into a record that stays under the old parent."),
            ops=ops, evidence=self._ev(loc, acc, current, sop=sop_verdict(acc, self.cfg), duplicate_group=group,
                                       retire_reasons=reasons, others=[_brief(o.account) for o in others]),
            confidence=self._conf(current, *others), tags=["re-parent", "duplicate"], location_slug=loc.slug)

    def _chow_create(self, loc: Location, c: Candidate, group: list[str]) -> Proposal:
        acc = c.account
        new = self._website_fields(loc) | {"parent_id": self.parent_id, "status": "Active"}
        return Proposal(
            kind="CHOW", title=f"CHOW: {loc.name} moves {self._pname(acc.get('parent_id'))} -> Bellhaven",
            summary=(f"{acc['name']} is under {self._pname(acc.get('parent_id'))}, but the website lists it as Bellhaven. It has billing "
                     f"history and open AR, so the old account stays exactly as it is: a new account is created under Bellhaven and "
                     f"the old one points to it via chow_current_account."),
            ops=[Op("create", None, new, ref="$new", known_occupants=list(group), probe={**new, "phone": loc.phone},
                    append_note=f"{self.today}: CHOW - current ownership record; previous account {acc['account_id']} "
                                f"({self._pname(acc.get('parent_id'))}) kept for billing."),
                 Op("patch", acc["account_id"], {"chow_current_account": "$new"},
                    old={"chow_current_account": acc.get("chow_current_account", "")}, guard=_guard(acc, ("chow_current_account",)))],
            evidence=self._ev(loc, acc, c, sop=sop_verdict(acc, self.cfg), duplicate_group=group),
            confidence=self._conf(c), tags=["chow", "billing-sop"], location_slug=loc.slug)

    def _create(self, loc: Location) -> Proposal:
        fields = self._website_fields(loc) | {"parent_id": self.parent_id, "status": "Active"}
        near = [a for a in self.accounts if a.get("billing_state") == loc.state and
                (names_equivalent(a["name"], loc.name) or (a.get("billing_city", "").lower() == loc.city.lower()))]
        return Proposal(
            kind="CREATE", title=f"Create {loc.name} ({loc.city}, {loc.state})",
            summary=f"No CRM account at {loc.street}, {loc.city} {loc.state} {loc.zip}. Create it under Bellhaven.",
            ops=[Op("create", None, fields, ref="$new", probe={**fields, "phone": loc.phone},
                    append_note=f"{self.today}: created from {loc.url}.")],
            evidence=self._ev(loc, None, None, checked_not_matching=[_brief(a) for a in near][:6],
                              why_not_matched="These accounts share the state and the city or name, but none has this street "
                                              "address and none is corroborated by zip or phone."),
            confidence="high", tags=["new"], location_slug=loc.slug)

    def _conflict(self, acc: dict, locs: list[Location]) -> Proposal:
        names = ", ".join(f"{l.name} ({l.street})" for l in locs)
        return Proposal(
            kind="CONFLICT", title=f"Conflict: {acc['name']} matches {len(locs)} website locations",
            summary=f"One CRM record matches several website locations ({names}). A person has to decide which facility it is.",
            ops=[Op("patch", acc["account_id"], {"status": "Needs Review"}, old={"status": acc.get("status", "")},
                    guard=_guard(acc, ("status",)), append_note=f"Matches several website locations: {names}.")],
            evidence=self._ev(None, acc, None, conflicting_locations=[l.to_dict() for l in locs]),
            confidence="medium", tags=["conflict"])

    def _not_on_site(self, acc: dict, live: list[dict]) -> Proposal | None:
        street_min = int(self.cfg["matching"]["street_fuzzy_min"])
        succ = [o for o in same_address_accounts(acc, live, street_min)
                if o.get("parent_id") not in ("", None, self.parent_id) and o.get("status") == "Active"]
        ev = {"crm": _brief(acc), "contacts": self.contacts_by_acc.get(acc["account_id"], []),
              "same_address_other_operators": [_brief(o) for o in succ]}
        if len(succ) == 1:
            s = succ[0]
            sid, spar = s["account_id"], self._pname(s.get("parent_id"))
            if self._lock(acc):
                ev["sop"] = sop_verdict(acc, self.cfg)
                if self.cfg["sop"].get("existing_successor", "link") == "create":
                    new = {k: s.get(k, "") for k in ("name", "billing_street", "billing_city", "billing_state", "billing_zip",
                                                     "care_type", "phone")}
                    new |= {"parent_id": s["parent_id"], "status": "Active"}
                    ev["why"] = (f"Literal SOP (config sop.existing_successor=create): a new {spar} account is created and linked, "
                                 f"and the pre-existing {spar} copy {sid} is retired as its duplicate.")
                    return Proposal(
                        kind="CHOW", title=f"CHOW: {acc['name']} -> new {spar} account",
                        summary=(f"Not on the Bellhaven website; {s['name']} ({spar}) is at the same address. Billing history + open "
                                 f"AR: the old account stays as is, a new {spar} account is created and linked, and the existing "
                                 f"{spar} copy is retired as its duplicate."),
                        ops=[Op("create", None, new, ref="$new", known_occupants=[acc["account_id"], sid],
                                requires=[_live_requirement(s)],
                                append_note=f"{self.today}: CHOW successor of {acc['account_id']}."),
                             Op("patch", acc["account_id"], {"chow_current_account": "$new"},
                                old={"chow_current_account": acc.get("chow_current_account", "")},
                                guard=_guard(acc, ("chow_current_account",))),
                             Op("patch", sid, {"duplicate_of_account": "$new", "status": "Inactive"},
                                old={"duplicate_of_account": s.get("duplicate_of_account", ""), "status": s.get("status", "")},
                                guard=_guard(s, ("duplicate_of_account", "status")),
                                append_note=f"{self.today}: duplicate of the CHOW successor record.")],
                        evidence=ev, confidence="medium", tags=["chow", "billing-sop", "not-on-site"])
                ev["why"] = (f"Not on the Bellhaven website; {s['name']} ({spar}) is in the CRM at the same address, so the facility "
                             f"changed hands. An account under the new parent already exists, so the old account is linked to it "
                             f"instead of creating a second copy (config: sop.existing_successor=link).")
                return Proposal(
                    kind="CHOW", title=f"CHOW: {acc['name']} -> {s['name']}",
                    summary=(f"{acc['name']} is no longer on the Bellhaven website and {s['name']} ({spar}) occupies the same address. "
                             f"Billing history + open AR: leave the old account exactly as it is and set chow_current_account -> {sid}."),
                    ops=[Op("patch", acc["account_id"], {"chow_current_account": sid},
                            old={"chow_current_account": acc.get("chow_current_account", "")},
                            guard=_guard(acc, ("chow_current_account",)), requires=[_live_requirement(s)])],
                    evidence=ev, confidence="medium", tags=["chow", "billing-sop", "not-on-site"])
            ev["why"] = (f"Not on the Bellhaven website; {s['name']} ({spar}) already represents this address and this record has no "
                         f"billing lock, so it is retired as a duplicate of that account.")
            return Proposal(
                kind="DUPLICATE", title=f"Retire {acc['name']} (now {s['name']}, {spar})",
                summary=f"Not on the Bellhaven website; {s['name']} ({spar}) is the current record at this address.",
                ops=[Op("patch", acc["account_id"], {"duplicate_of_account": sid, "status": "Inactive"},
                        old={"duplicate_of_account": acc.get("duplicate_of_account", ""), "status": acc.get("status", "")},
                        guard=_guard(acc, ("duplicate_of_account", "status")), requires=[_live_requirement(s)],
                        append_note=f"{self.today}: no longer on the Bellhaven website; same address as {sid} ({spar}).")],
                evidence=ev, confidence="medium", tags=["duplicate", "not-on-site"])
        if acc.get("status") == "Needs Review":
            return None                          # already flagged for a human
        ev["why"] = ("The website is the operator's own list of communities. Absence alone doesn't prove a sale or a closure, "
                     "so the account is flagged for a human instead of being retired or re-parented.")
        note = (f"{self.today}: not listed on the Bellhaven website; "
                + (f"several operators at this address ({', '.join(o['name'] for o in succ)})." if succ
                   else "no other operator found at this address in the CRM. Confirm closed / sold / website omission."))
        return Proposal(
            kind="NOT_ON_SITE", title=f"Flag {acc['name']} - not on the Bellhaven website",
            summary=f"{acc['name']} ({acc.get('billing_city')}, {acc.get('billing_state')}) is under Bellhaven in the CRM but not on the website.",
            ops=[Op("patch", acc["account_id"], {"status": "Needs Review"}, old={"status": acc.get("status", "")},
                    guard=_guard(acc, ("status",)), append_note=note)],
            evidence=ev, confidence="medium", tags=["not-on-site"])
