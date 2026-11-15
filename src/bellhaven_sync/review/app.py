"""Local review app: a reviewer sees each proposed change with its evidence and approves or rejects it.

Approving is the only way anything is written to the CRM (via executor.apply_proposal). The approve
form carries the proposal version the reviewer looked at; if the daily run refreshed the evidence in
the meantime, the approval is refused and the page reloads with the new version.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import Settings
from ..crm import CRM
from ..normalize import digits, norm_name, norm_street
from ..executor import StaleProposal, apply_proposal
from ..plan import KIND_LABEL, KIND_ORDER
from ..state import Busy, State

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
COMPARE_FIELDS = ["name", "parent_name", "billing_street", "billing_city", "billing_state", "billing_zip",
                  "care_type", "status", "phone", "lifetime_revenue", "outstanding_ar"]


def _go(path: str, **params) -> RedirectResponse:
    q = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse(f"{path}?{q}" if q else path, status_code=303)


def _compare_rows(p: dict) -> list[dict]:
    ev = p.get("evidence", {})
    web, crm = ev.get("website") or {}, ev.get("crm") or {}
    if not web or not crm:
        return []
    web_map = {"name": web.get("name"), "billing_street": web.get("street"), "billing_city": web.get("city"),
               "billing_state": web.get("state"), "billing_zip": web.get("zip"),
               "care_type": " + ".join(web.get("care_offerings", [])), "phone": web.get("phone"),
               "parent_name": "Bellhaven Senior Living (it is on Bellhaven's website)"}
    target = crm.get("account_id")
    changed = {k for op in p.get("ops", []) if op.get("action") == "patch" and op.get("account_id") == target
               for k in op.get("fields", {})}
    rows = []
    for f in COMPARE_FIELDS:
        w, c = web_map.get(f, ""), crm.get(f, "")
        differs = bool(w) and str(w).strip().lower() != str(c).strip().lower()
        note = ""
        if f == "parent_name":
            differs = not str(c or "").lower().startswith("bellhaven senior living")
        elif f == "care_type":
            differs, note = False, "website wording; mapped to a CRM care type by config"
        elif f == "phone" and differs:
            differs, note = False, "differs from the CRM; context only, this tool does not update phone numbers"
        same_form = {"billing_street": norm_street, "name": norm_name, "phone": digits}.get(f)
        fmt_only = differs and same_form is not None and same_form(str(w)) == same_form(str(c or ""))
        rows.append({"field": f, "website": w if w is not None else "", "crm": c,
                     "differs": differs and not fmt_only, "fmt_only": fmt_only, "note": note,
                     "changes": f in changed or (f == "parent_name" and "parent_id" in changed)})
    return rows


def create_app(settings: Settings, state: State | None = None, crm: CRM | None = None) -> FastAPI:
    app = FastAPI(title="Bellhaven ownership review")
    store = state or State(settings.db_path)
    app.state.store, app.state.crm = store, crm
    reviewer_default = os.environ.get("REVIEWER", "reviewer")
    with store.exclusive("startup"):
        store.recover_interrupted()

    def get_crm() -> CRM:
        if app.state.crm is None:
            app.state.crm = CRM(settings)
        return app.state.crm

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        """The app writes to a CRM: refuse form POSTs coming from another site (basic CSRF protection)."""
        if request.method == "POST":
            src = request.headers.get("origin") or request.headers.get("referer")
            if src and urlparse(src).netloc != request.headers.get("host"):
                return PlainTextResponse("cross-site request refused", status_code=403)
        return await call_next(request)

    def ctx(request: Request, **kw):
        return {"request": request, "browser": settings.crm_browser, "kind_label": KIND_LABEL,
                "counts": store.counts(), "env": settings.env_name, **kw}

    @app.get("/", response_class=HTMLResponse)
    def queue(request: Request, status: str = "pending", msg: str = "", err: str = ""):
        items = store.list(None if status == "all" else status)
        groups = [(k, [p for p in items if p["kind"] == k]) for k in KIND_ORDER]
        groups = [(k, ps) for k, ps in groups if ps]
        return TEMPLATES.TemplateResponse(request, "queue.html", ctx(
            request, groups=groups, status=status, last_run=store.last_run(), msg=msg, err=err))

    @app.get("/p/{key}", response_class=HTMLResponse)
    def detail(request: Request, key: str, msg: str = "", err: str = ""):
        p = store.get(key)
        if not p:
            raise HTTPException(404, "Unknown proposal")
        return TEMPLATES.TemplateResponse(request, "detail.html", ctx(
            request, p=p, compare=_compare_rows(p), msg=msg, err=err, reviewer=reviewer_default, nav=_nav(key)))

    def _nav(key: str) -> dict:
        """Previous / next pending item in queue order, so a reviewer can work through the list one by one."""
        order = sorted(store.list(None), key=lambda x: KIND_ORDER.index(x["kind"]) if x["kind"] in KIND_ORDER else 99)
        keys = [x["key"] for x in order]
        pending = [x["key"] for x in order if x["status"] == "pending"]
        i = keys.index(key) if key in keys else -1
        before = [k for k in keys[:i] if k in pending]
        after = [k for k in keys[i + 1:] if k in pending]
        return {"prev": before[-1] if before else None, "next": after[0] if after else (before[0] if before else None),
                "pos": pending.index(key) + 1 if key in pending else None, "total": len(pending)}

    @app.post("/p/{key}/decide")
    def decide(key: str, decision: str = Form(...), reviewer: str = Form(""), reason: str = Form(""),
               version: str = Form(""), confirm_create: str = Form("")):
        p = store.get(key)
        if not p:
            raise HTTPException(404, "Unknown proposal")
        who = (reviewer or reviewer_default).strip()
        if decision == "reject":
            if not version:
                return _go(f"/p/{key}", err="Rejection refused: reload the page and try again.")
            if not reason.strip():
                return _go(f"/p/{key}", err="Please give a reason when rejecting.")
            if not store.reject(key, who, reason.strip(), version or None):
                return _go(f"/p/{key}", err=f"Not rejected: the proposal is {store.get(key)['status']} or was refreshed - review it again.")
            return _go(f"/p/{key}", msg="Rejected. Nothing was written, and this exact change will not be proposed again.")
        if decision == "close":
            if not reason.strip():
                return _go(f"/p/{key}", err="Please say why you are closing it (e.g. 'account already exists').")
            if not store.close_failed(key, who, reason.strip()):
                return _go(f"/p/{key}", err=f"Not closed: the proposal is {store.get(key)['status']}.")
            return _go(f"/p/{key}", msg="Closed. Nothing more will be written for it; the next run re-plans this location from the CRM.")
        if decision not in ("approve", "retry"):
            raise HTTPException(400, "decision must be approve, reject, retry or close")
        if decision == "approve" and not version:
            return _go(f"/p/{key}", err="Approval refused: the form did not say which version you reviewed. Reload and try again.")
        try:
            client = get_crm()                 # fail before recording anything if the CRM isn't configured
        except Exception as e:
            return _go(f"/p/{key}", err=f"CRM client not available: {e}")
        try:
            with store.exclusive("pipeline run / CRM write", wait=False):
                claimed = (store.approve_and_claim(key, who, reason.strip(), version or None) if decision == "approve"
                           else store.claim_retry(key, who))
                if not claimed:
                    return _go(f"/p/{key}", err=f"Not applied: the proposal is {store.get(key)['status']} or was refreshed "
                                                 f"since you opened it - review the current version.")
                res = apply_proposal(client, store, key, settings.raw,
                                     confirm_unresolved_create=(decision == "retry" and confirm_create == "yes"))
        except Busy:
            return _go(f"/p/{key}", err="A pipeline run or another write is in progress - try again in a moment.")
        except StaleProposal as e:
            return _go(f"/p/{key}", err=str(e)[:600])
        except Exception as e:
            return _go(f"/p/{key}", err=f"Write failed ({type(e).__name__}): {str(e)[:400]}. Nothing more will be written until you retry.")
        written = [r for r in res["results"] if r["status"] == "written"]
        ids = ", ".join(r["account_id"] for r in res["results"])
        return _go(f"/p/{key}", msg=f"Approved and written to the CRM ({len(written)} write(s); records {ids}). Read back and verified.")

    @app.post("/run")
    def run_now():
        from ..pipeline import run
        try:
            stats = run(settings, crm=get_crm(), state=store, wait=False)
        except Busy:
            return _go("/", err="A run or a write is already in progress.")
        except Exception as e:
            return _go("/", err=f"Run failed: {type(e).__name__}: {str(e)[:300]}")
        q = stats["queue"]
        applied = store.counts().get("applied", 0)
        return _go("/", msg=f"Run #{stats['run_id']}: {q['new']} new, {q['still_pending']} still pending, "
                            f"{q['already_decided']} previously rejected (kept out of the queue), {q['regressed']} regressed. "
                            f"{applied} approved change(s) are already in the CRM, so they are no longer proposed.")

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request):
        decisions = [dict(r) for r in store.q(
            "SELECT d.*, p.title, p.kind FROM decisions d JOIN proposals p USING(key) ORDER BY d.id DESC")]
        writes = [dict(r) for r in store.q("SELECT * FROM writes ORDER BY id DESC LIMIT 300")]
        runs = [dict(r) for r in store.q("SELECT * FROM runs ORDER BY id DESC LIMIT 20")]
        return TEMPLATES.TemplateResponse(request, "history.html", ctx(request, decisions=decisions, writes=writes, runs=runs))

    return app
