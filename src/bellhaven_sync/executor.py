"""Apply an approved proposal to the CRM - the only code path that writes.

Progress is derived from the CRM itself, so a retry re-checks everything instead of trusting a log:
  * a PATCH is "done" when the record already holds the desired values;
  * a CREATE is "done" when the account it made can be found again: first by the id we logged when the
    POST returned, then by the marker ([bhsync <key>#<op>]) written into the new account's note. A marked
    account is verified (and corrected - it is ours) before anything is linked to it.
  * a CREATE is only sent if no live account occupies the facility (same matching rules as the planner),
    so a lost POST response, an edited note or a rep who created the facility by hand doesn't lead to a
    second account. If a POST was sent and its account can't be found again by id, marker or address,
    nothing more is sent until a person confirms it was not created (or closes the proposal).

Before any write, everything the decision relied on is re-checked against the live CRM: the values the
reviewer saw, the guarded fields (a field another approved proposal may change is allowed to hold either
the reviewed value or that proposal's value - nothing else), and the records it depends on (a duplicate's
survivor must still be a live, Active record at the same address). If anything moved, nothing is written
and the proposal is superseded (the next run re-plans it) - unless an earlier attempt already wrote part
of it, in which case it stays `failed`, its records locked, for a person to look at.
"""
from __future__ import annotations

from .crm import CRM, CRMError, NotSent, WriteUncertain
from .match import candidates_for, is_superseded
from .normalize import same
from .state import State
from .website import Location

DEFAULT_MATCH = {"matching": {"street_fuzzy_min": 90, "name_fuzzy_min": 85}}


class NotApproved(RuntimeError):
    pass


class StaleProposal(RuntimeError):
    pass


def marker(key: str, i: int) -> str:
    return f"[bhsync {key}#{i}]"


def _resolve(fields: dict, refs: dict) -> tuple[dict, bool]:
    out, ok = {}, True
    for k, v in fields.items():
        if isinstance(v, str) and v.startswith("$"):
            if v not in refs:
                ok = False
                continue
            v = refs[v]
        out[k] = v
    return out, ok


def _matches(field: str, value, expected) -> bool:
    if isinstance(expected, dict) and "any_of" in expected:
        return any(same(field, value, e) for e in expected["any_of"])
    return same(field, value, expected)


def _find_marked(crm: CRM, fields: dict, mark: str) -> dict | None:
    for a in crm.list_accounts(state=fields.get("billing_state", ""), city=fields.get("billing_city", "")):
        if mark in (a.get("note") or ""):
            return a
    for a in crm.list_accounts():          # the city may have been edited since: look everywhere
        if mark in (a.get("note") or ""):
            return a
    return None


def _billing_locked(acc: dict) -> bool:
    try:
        return float(acc.get("lifetime_revenue") or 0) > 0 and float(acc.get("outstanding_ar") or 0) > 0
    except (TypeError, ValueError):
        return True                                      # can't tell -> treat as protected


def _occupants(crm: CRM, op: dict, own_ids: set[str], cfg: dict) -> list[dict]:
    """Live accounts that already represent this facility, by the planner's own rules (address, or a fallback
    corroborated by zip or phone - the probe keeps the website phone even though phones are never written).
    Accounts known at planning time are only exempt while they still are what they were: not under the
    parent we are creating for."""
    probe = op.get("probe") or op["fields"]
    target_parent = op["fields"].get("parent_id")
    loc = Location(slug="", name=probe.get("name", ""), street=probe.get("billing_street", ""),
                   city=probe.get("billing_city", ""), state=probe.get("billing_state", ""),
                   zip=probe.get("billing_zip", ""), care_offerings=[], url="", phone=probe.get("phone", ""))
    accounts = crm.list_accounts(state=loc.state)
    by_id = {a["account_id"]: a for a in accounts}
    known = set(op.get("known_occupants", []))
    live = [a for a in accounts if not is_superseded(a, by_id) and a["account_id"] not in own_ids
            and not (a["account_id"] in known and a.get("parent_id") != target_parent)]
    return [c.account for c in candidates_for(loc, live, cfg)]


def _with_note(current_note: str, addition: str) -> str:
    current_note = current_note or ""
    if not addition or addition in current_note:
        return current_note
    return f"{current_note}\n{addition}".strip()


def _logged_creates(state: State, key: str) -> dict[int, str]:
    out = {}
    for w in state.q("SELECT op_index, account_id FROM writes WHERE key=? AND method='POST' AND account_id IS NOT NULL ORDER BY id", (key,)):
        out[int(w["op_index"])] = w["account_id"]
    return out


def apply_proposal(crm: CRM, state: State, key: str, cfg: dict | None = None, confirm_unresolved_create: bool = False) -> dict:
    """confirm_unresolved_create: a person checked the CRM and confirms an earlier create whose outcome was
    unknown did NOT happen - only then may it be sent again."""
    cfg = cfg or DEFAULT_MATCH
    p = state.get(key)
    if not p or p["status"] != "applying":
        raise NotApproved(f"proposal {key} is {p['status'] if p else 'unknown'}; it must be approved (claimed) first")
    ops = p["ops"]
    own_ids = {o["account_id"] for o in ops if o.get("account_id")}
    refs: dict[str, str] = {}
    done: set[int] = set()
    results = []
    try:
        # ---- 1. creates that already happened (logged id first, then marker). Nothing is written here.
        drift: list[str] = []
        corrections: dict[int, tuple[str, dict]] = {}
        logged = _logged_creates(state, key)
        last_post_event: dict[int, str] = {}           # per create op: POST_INTENT / POST_NOT_SENT / POST, latest wins
        for w in state.q("SELECT op_index, method FROM writes WHERE key=? AND method IN ('POST_INTENT','POST_NOT_SENT','POST') "
                         "ORDER BY id", (key,)):
            if w["op_index"] is not None:
                last_post_event[int(w["op_index"])] = w["method"]
        intents = {i for i, m in last_post_event.items() if m == "POST_INTENT"}   # sent, outcome never established
        for i, op in enumerate(ops):
            if op["action"] != "create":
                continue
            found = None
            if i in logged:
                try:
                    found = crm.get_account(logged[i])
                except CRMError:
                    found = None
            found = found or _find_marked(crm, op["fields"], marker(key, i))
            if not found:
                if i in intents and not confirm_unresolved_create:
                    raise CRMError("An earlier attempt sent this create but its outcome is unknown and the account "
                                   "can't be found by id or marker. Check the CRM by hand; retry with 'confirm' only "
                                   "if it was not created.")
                continue
            refs[op.get("ref") or "$new"] = found["account_id"]
            want, _ = _resolve(op["fields"], refs)
            if found.get("duplicate_of_account") or found.get("chow_current_account"):
                drift.append(f"the account created earlier ({found['account_id']}) has since been linked elsewhere "
                             f"(duplicate/CHOW) - a person must decide")
            wrong = {k: v for k, v in want.items() if k != "note" and not same(k, found.get(k), v)}
            if "parent_id" in wrong and _billing_locked(found):
                drift.append(f"the account created earlier ({found['account_id']}) now has billing history and open AR; "
                             f"its parent may not be changed (SOP)")
            if wrong:
                corrections[i] = (found["account_id"], wrong)   # applied in step 3, only if every check passes
            done.add(i)

        # ---- 2. re-check everything the decision relied on (no writes yet)
        current: dict[int, dict] = {}
        for i, op in enumerate(ops):
            if op["action"] != "patch":
                continue
            cur = crm.get_account(op["account_id"])
            current[i] = cur
            want, resolved = _resolve(op["fields"], refs)
            note_ok = not op.get("append_note") or op["append_note"] in (cur.get("note") or "")
            if resolved and note_ok and all(same(k, cur.get(k), v) for k, v in want.items()):
                done.add(i)
                continue
            for k, old in op.get("old", {}).items():
                if not same(k, cur.get(k), old) and not (k in want and same(k, cur.get(k), want[k])):
                    drift.append(f"{op['account_id']}.{k}: was {old!r}, now {cur.get(k)!r}")
            for k, g in op.get("guard", {}).items():
                if not _matches(k, cur.get(k), g):
                    drift.append(f"{op['account_id']}.{k}: expected {g!r}, now {cur.get(k)!r}")
        # records this proposal depends on - checked even when the op itself is already done, except where the
        # dependency is a record this same proposal has already changed (its later state is our own doing)
        changed_by_us: dict[str, set[str]] = {}
        for j in done:
            if ops[j]["action"] == "patch":
                changed_by_us.setdefault(ops[j]["account_id"], set()).update(ops[j]["fields"])
        for i, op in enumerate(ops):
            for req in op.get("requires", []):
                other = crm.get_account(req["account_id"])
                for k, v in req["fields"].items():
                    if k in changed_by_us.get(req["account_id"], set()):
                        continue
                    if not _matches(k, other.get(k), v):
                        drift.append(f"{req['account_id']}.{k} must be {v!r}, is {other.get(k)!r}")
        for i, op in enumerate(ops):
            if op["action"] == "create" and i not in done:
                for a in _occupants(crm, op, own_ids, cfg):
                    drift.append(f"account {a['account_id']} ({a['name']}) already represents this facility")
        if drift:
            if done:
                state.record_write(key, None, "CHECK", None, {"drift": drift}, "partly applied earlier", False)
                state.finish(key, "failed")
                raise StaleProposal("Part of this change was already written, and the records have changed since: "
                                    + "; ".join(drift) + ". Left as failed for a person to check.")
            state.record_write(key, None, "CHECK", None, {"drift": drift}, "nothing written", False)
            state.finish(key, "superseded")
            raise StaleProposal("The CRM changed since this proposal was made: " + "; ".join(drift)
                                + ". Nothing was written; the next run will re-plan it.")

        # ---- 3. write what is left, in order, verifying each step
        for i, (aid, wrong) in corrections.items():      # the account we created earlier: put it right first
            crm.patch_account(aid, wrong)
            check = crm.get_account(aid)
            still = [k for k, v in wrong.items() if not same(k, check.get(k), v)]
            state.record_write(key, i, "PATCH", aid, wrong, check, not still)
            if still:
                raise CRMError(f"created account {aid} could not be corrected: {still}")
            results.append({"op": i, "action": "correct created account", "account_id": aid, "status": "written", "fields": wrong})
        for i, op in enumerate(ops):
            if i in done:
                results.append({"op": i, "action": op["action"], "status": "already done",
                                "account_id": op.get("account_id") or refs.get(op.get("ref") or "$new")})
                continue
            want, resolved = _resolve(op["fields"], refs)
            if not resolved:
                raise CRMError(f"op {i}: unresolved reference in {op['fields']}")
            if op["action"] == "create":
                mark = marker(key, i)
                body = dict(want, note=_with_note(want.get("note", ""), f"{op.get('append_note', '')} {mark}".strip()))
                state.record_write(key, i, "POST_INTENT", None, body, "", True)
                try:
                    out = crm.create_account(body)
                    aid = out.get("account_id")
                except NotSent:
                    state.record_write(key, i, "POST_NOT_SENT", None, body, "connection failed before sending", True)
                    raise
                except WriteUncertain:
                    aid = None
                if aid:
                    state.record_write(key, i, "POST", aid, body, out, True)      # id persisted before verification
                else:
                    found = _find_marked(crm, want, mark)
                    if not found:
                        raise CRMError("create outcome unknown and no account carries the marker; nothing more "
                                       "written - retry will look again before creating")
                    aid = found["account_id"]
                    state.record_write(key, i, "POST", aid, body, found, True)
                refs[op.get("ref") or "$new"] = aid
                check = crm.get_account(aid)
                bad = [k for k, v in want.items() if k != "note" and not same(k, check.get(k), v)]
                if mark not in (check.get("note") or ""):
                    bad.append("note marker")
                state.record_write(key, i, "VERIFY", aid, {}, check, not bad)
                if bad:
                    raise CRMError(f"read-back mismatch on new account {aid}: {bad}")
                results.append({"op": i, "action": "create", "account_id": aid, "status": "written"})
            else:
                aid = op["account_id"]
                body = dict(want)
                if op.get("append_note"):
                    body["note"] = _with_note(current[i].get("note", ""), op["append_note"])
                crm.patch_account(aid, body)
                check = crm.get_account(aid)
                bad = [k for k, v in want.items() if not same(k, check.get(k), v)]
                if op.get("append_note") and op["append_note"] not in (check.get("note") or ""):
                    bad.append("note")
                state.record_write(key, i, "PATCH", aid, body, check, not bad)
                if bad:
                    raise CRMError(f"read-back mismatch on {aid}: {[(k, check.get(k)) for k in bad]}")
                results.append({"op": i, "action": "patch", "account_id": aid, "status": "written"})
    except StaleProposal:
        raise
    except Exception as e:
        state.record_write(key, None, "ERROR", None, {}, f"{type(e).__name__}: {e}", False)
        state.finish(key, "failed")
        raise
    state.finish(key, "applied")
    return {"key": key, "results": results, "refs": refs}
