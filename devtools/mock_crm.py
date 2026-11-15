"""Local stand-in for the Bellhaven CRM Sandbox API (offline development and demo only).

Same contract as the real sandbox as far as we can observe it:
  GET  /api/v1/me
  GET  /api/v1/accounts?q&city&state&zip&street&parent_id&page&page_size  -> {data,page,page_size,total}
  GET  /api/v1/accounts/{id}
  POST /api/v1/accounts
  PATCH /api/v1/accounts/{id}
  GET  /api/v1/contacts?account_id&q, GET /api/v1/contacts/{id}
plus a read-only HTML "CRM browser" at /crm/accounts.

It is deliberately STRICTER than we assume the real API is (unknown fields, bad status values and
dangling ids are rejected), so code that works here should not surprise us against the real one.

Run:  uv run python -m devtools.mock_crm            (port 8100, state in devtools/mock_state.json)
      uv run python -m devtools.mock_crm --reset    (re-seed from the initial snapshot first)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import string
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

HERE = Path(__file__).resolve().parent
DEFAULT_SEED_DIR = HERE / "seed"
STATE_FILE = Path(os.environ.get("MOCK_CRM_STATE", HERE / "mock_state.json"))
TOKEN = os.environ.get("MOCK_CRM_TOKEN", "mock_local_token")

STATUSES = {"Active", "Inactive", "Needs Review"}
ACCOUNT_WRITABLE = {
    "name", "parent_id", "billing_street", "billing_city", "billing_state", "billing_zip",
    "care_type", "status", "phone", "lifetime_revenue", "outstanding_ar",
    "chow_current_account", "duplicate_of_account", "note",
}

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _new_id(prefix: str, existing: set[str]) -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        cand = prefix + "".join(random.choice(alphabet) for _ in range(18 - len(prefix)))
        if cand not in existing:
            return cand


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.accounts: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.log: list[dict] = []

    def seed(self, seed_dir: Path = DEFAULT_SEED_DIR) -> None:
        accounts = json.loads((seed_dir / "accounts.json").read_text())
        contacts = json.loads((seed_dir / "contacts.json").read_text())
        self.accounts = {a["account_id"]: a for a in accounts}
        self.contacts = {c["contact_id"]: c for c in contacts}
        self.log = []
        self.save()

    def load(self) -> None:
        if not self.path.exists():
            self.seed()
            return
        d = json.loads(self.path.read_text())
        self.accounts, self.contacts, self.log = d["accounts"], d["contacts"], d.get("log", [])

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"accounts": self.accounts, "contacts": self.contacts, "log": self.log}, indent=1))
        tmp.replace(self.path)

    def parent_name(self, parent_id: str) -> str:
        p = self.accounts.get(parent_id or "")
        return p["name"] if p else ""


store = Store(STATE_FILE)
app = FastAPI(title="Bellhaven CRM Sandbox API (LOCAL MOCK)")


def auth(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Provide your candidate token as: Authorization: Bearer <token>")
    if authorization.removeprefix("Bearer ").strip() != TOKEN:
        raise HTTPException(401, "Unknown token")


def _page(items: list[dict], page: int, page_size: int) -> dict:
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "page must be >=1 and 1<=page_size<=100")
    start = (page - 1) * page_size
    return {"data": items[start:start + page_size], "page": page, "page_size": page_size,
            "total": len(items)}


def _validate_account_fields(body: dict, *, creating: bool) -> None:
    unknown = set(body) - ACCOUNT_WRITABLE
    if unknown:
        raise HTTPException(422, f"Unknown or read-only fields: {sorted(unknown)}")
    if "status" in body and body["status"] not in STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(STATUSES)}")
    for ref in ("parent_id", "chow_current_account", "duplicate_of_account"):
        v = body.get(ref)
        if v and v not in store.accounts:
            raise HTTPException(422, f"{ref} references unknown account {v}")
    for num in ("lifetime_revenue", "outstanding_ar"):
        if num in body and not isinstance(body[num], (int, float)):
            raise HTTPException(422, f"{num} must be a number")
    if creating and not str(body.get("name", "")).strip():
        raise HTTPException(422, "name is required")


@app.get("/api/v1/me")
def me(_: None = Depends(auth)):
    return {"candidate": "LOCAL MOCK", "message": "Token OK. This is the offline mock CRM."}


@app.get("/api/v1/accounts")
def list_accounts(q: str | None = None, city: str | None = None, state: str | None = None,
                  zip: str | None = None, street: str | None = None, parent_id: str | None = None,
                  page: int = 1, page_size: int = 25, _: None = Depends(auth)):
    items = list(store.accounts.values())

    def has(field: str, needle: str | None):
        nonlocal items
        if needle:
            items = [a for a in items if needle.lower() in str(a.get(field, "")).lower()]

    has("name", q); has("billing_city", city); has("billing_zip", zip); has("billing_street", street)
    if state:
        items = [a for a in items if a["billing_state"].lower() == state.lower()]
    if parent_id is not None:
        items = [a for a in items if a["parent_id"] == parent_id]
    items.sort(key=lambda a: a["account_id"])
    return _page(items, page, page_size)


@app.get("/api/v1/accounts/{account_id}")
def get_account(account_id: str, _: None = Depends(auth)):
    a = store.accounts.get(account_id)
    if not a:
        raise HTTPException(404, "Account not found")
    return a


@app.post("/api/v1/accounts", status_code=201)
async def create_account(request: Request, _: None = Depends(auth)):
    body = await request.json()
    with _lock:
        _validate_account_fields(body, creating=True)
        aid = _new_id("001", set(store.accounts))
        rec = {k: "" for k in ("name", "parent_id", "billing_street", "billing_city",
                               "billing_state", "billing_zip", "care_type", "phone",
                               "chow_current_account", "duplicate_of_account", "note")}
        rec.update({"account_id": aid, "status": "Active", "lifetime_revenue": 0,
                    "outstanding_ar": 0})
        rec.update(body)
        rec["parent_name"] = store.parent_name(rec["parent_id"])
        rec["created_by_candidate"] = True
        rec["updated_at"] = _now()
        order = ["account_id", "name", "parent_id", "parent_name", "billing_street",
                 "billing_city", "billing_state", "billing_zip", "care_type", "status", "phone",
                 "lifetime_revenue", "outstanding_ar", "chow_current_account",
                 "duplicate_of_account", "note", "created_by_candidate", "updated_at"]
        rec = {k: rec[k] for k in order}
        store.accounts[aid] = rec
        store.log.append({"at": rec["updated_at"], "op": "POST", "id": aid, "body": body})
        store.save()
    return rec


@app.patch("/api/v1/accounts/{account_id}")
async def patch_account(account_id: str, request: Request, _: None = Depends(auth)):
    body = await request.json()
    with _lock:
        a = store.accounts.get(account_id)
        if not a:
            raise HTTPException(404, "Account not found")
        _validate_account_fields(body, creating=False)
        if body.get("parent_id") == account_id or body.get("duplicate_of_account") == account_id:
            raise HTTPException(422, "An account cannot reference itself")
        a.update(body)
        a["parent_name"] = store.parent_name(a["parent_id"])
        a["updated_at"] = _now()
        store.log.append({"at": a["updated_at"], "op": "PATCH", "id": account_id, "body": body})
        store.save()
    return a


@app.get("/api/v1/contacts")
def list_contacts(account_id: str | None = None, q: str | None = None, page: int = 1,
                  page_size: int = 25, _: None = Depends(auth)):
    items = list(store.contacts.values())
    if account_id:
        items = [c for c in items if c["account_id"] == account_id]
    if q:
        items = [c for c in items if q.lower() in c["name"].lower()]
    items.sort(key=lambda c: c["contact_id"])
    return _page(items, page, page_size)


@app.get("/api/v1/contacts/{contact_id}")
def get_contact(contact_id: str, _: None = Depends(auth)):
    c = store.contacts.get(contact_id)
    if not c:
        raise HTTPException(404, "Contact not found")
    return c


# ---------------- read-only CRM browser (what a sales rep would click around) ----------------
_CSS = """body{font-family:-apple-system,Segoe UI,sans-serif;margin:24px;color:#222}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid #ddd;padding:6px;text-align:left}
th{background:#f4f4f4}a{color:#0b5cad;text-decoration:none}.tag{padding:2px 6px;border-radius:4px;font-size:12px}
.Active{background:#e3f4e8}.Inactive{background:#eee;color:#777}.Needs{background:#fff1cc}.banner{background:#fde8e8;padding:8px;margin-bottom:12px}"""


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


@app.get("/crm/accounts", response_class=HTMLResponse)
def crm_list(q: str = "", parent_id: str = ""):
    items = sorted(store.accounts.values(), key=lambda a: (a["parent_name"], a["name"]))
    if q:
        items = [a for a in items if q.lower() in (a["name"] + a["billing_city"]).lower()]
    if parent_id:
        items = [a for a in items if a["parent_id"] == parent_id]
    rows = "".join(
        f"<tr><td><a href='/crm/accounts/{_esc(a['account_id'])}'>{_esc(a['name'])}</a></td>"
        f"<td>{_esc(a['parent_name'])}</td><td>{_esc(a['billing_city'])}, {_esc(a['billing_state'])}</td>"
        f"<td><span class='tag {_esc(a['status'].split()[0])}'>{_esc(a['status'])}</span></td>"
        f"<td>{_esc(a['updated_at'])}</td></tr>" for a in items)
    return (f"<html><head><title>CRM browser (local mock)</title><style>{_CSS}</style></head><body>"
            f"<div class='banner'>LOCAL MOCK CRM — offline copy seeded from the initial snapshot</div>"
            f"<form><input name=q value='{_esc(q)}' placeholder='search name/city'> <button>Search</button></form>"
            f"<p>{len(items)} accounts</p><table><tr><th>Name</th><th>Parent</th><th>City</th><th>Status</th>"
            f"<th>Updated</th></tr>{rows}</table></body></html>")


@app.get("/crm/accounts/{account_id}", response_class=HTMLResponse)
def crm_detail(account_id: str):
    a = store.accounts.get(account_id)
    if not a:
        raise HTTPException(404, "Account not found")
    fields = "".join(f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in a.items())
    kids = [x for x in store.accounts.values() if x["parent_id"] == account_id]
    kid_rows = "".join(f"<li><a href='/crm/accounts/{_esc(k['account_id'])}'>{_esc(k['name'])}</a> "
                       f"({_esc(k['status'])})</li>" for k in kids)
    contacts = [c for c in store.contacts.values() if c["account_id"] == account_id]
    c_rows = "".join(f"<li>{_esc(c['name'])} — {_esc(c['title'])}</li>" for c in contacts)
    return (f"<html><head><title>{_esc(a['name'])}</title><style>{_CSS}</style></head><body>"
            f"<div class='banner'>LOCAL MOCK CRM</div><p><a href='/crm/accounts'>&larr; all accounts</a></p>"
            f"<h2>{_esc(a['name'])}</h2><table>{fields}</table>"
            f"<h3>Child accounts ({len(kids)})</h3><ul>{kid_rows}</ul>"
            f"<h3>Contacts ({len(contacts)})</h3><ul>{c_rows}</ul></body></html>")


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--reset", action="store_true", help="re-seed from devtools/seed/*.json")
    args = ap.parse_args()
    if args.reset:
        store.seed()
    else:
        store.load()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


store.load() if STATE_FILE.exists() else None

if __name__ == "__main__":
    main()
