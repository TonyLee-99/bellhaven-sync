"""Thin client for the CRM sandbox API (works against the real sandbox or the local mock).

Retry policy: GET and PATCH are idempotent and are retried on transport errors / 429 / 5xx.
POST is never retried automatically - if a POST times out we cannot know whether the account was
created, so the error is raised as `WriteUncertain` and the executor reconciles by searching for the
proposal's marker before it would ever POST again (the API has no delete).
"""
from __future__ import annotations

import time

import httpx

from .config import Settings, guard_remote


class CRMError(RuntimeError):
    pass


class WriteUncertain(CRMError):
    """The request may or may not have been applied by the server."""


class NotSent(CRMError):
    """The request never reached the server (connection could not be made) - nothing was applied."""


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _retry_after(value: str | None, default: float) -> float:
    """Retry-After is either seconds or an HTTP date; cap the wait at 60 s."""
    if not value:
        return default
    try:
        return min(float(value), 60.0)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        delta = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        return min(max(delta, 0.0), 60.0)
    except (TypeError, ValueError):
        return default
IDEMPOTENT = {"GET", "PATCH"}


def record_from(body) -> dict:
    """Accept the plain record or a {"data": record} envelope; require an account/contact id."""
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        body = body["data"]
    if not isinstance(body, dict):
        raise CRMError(f"unexpected response body: {body!r:.200}")
    if "account_id" not in body and "id" in body:
        body = {**body, "account_id": body["id"]}
    return body


class CRM:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.base = settings.crm_base
        guard_remote(self.base)
        self.page_size = int(settings["crm"]["page_size"])
        self.http = client or httpx.Client(timeout=30, follow_redirects=False)
        self.http.headers["Authorization"] = f"Bearer {settings.token}"

    def _req(self, method: str, path: str, **kw):
        url = self.base + path
        guard_remote(url)
        attempts = 4 if method in IDEMPOTENT else 1
        for attempt in range(attempts):
            try:
                r = self.http.request(method, url, **kw)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                if method not in IDEMPOTENT:
                    raise NotSent(f"{method} {path}: could not connect ({e!r}) - nothing was sent") from e
                if attempt == attempts - 1:
                    raise CRMError(f"{method} {path}: {e!r}") from e
                time.sleep(2 ** attempt)
                continue
            except httpx.TransportError as e:
                if method not in IDEMPOTENT:
                    raise WriteUncertain(f"{method} {path}: {e!r} - the server may have applied it") from e
                if attempt == attempts - 1:
                    raise CRMError(f"{method} {path}: {e!r}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code in RETRYABLE_STATUS and attempt < attempts - 1:
                time.sleep(_retry_after(r.headers.get("Retry-After"), 2 ** attempt))
                continue
            if 300 <= r.status_code < 400:
                raise CRMError(f"{method} {path} -> unexpected redirect {r.status_code} to {r.headers.get('location')}")
            if r.status_code >= 500 and method == "POST":
                raise WriteUncertain(f"POST {path} -> {r.status_code}: {r.text[:300]}")
            if r.status_code >= 400:
                raise CRMError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            if not r.content:
                return {}
            try:
                return r.json()
            except ValueError as e:
                raise CRMError(f"{method} {path}: non-JSON response {r.text[:200]!r}") from e
        raise CRMError(f"{method} {path}: retries exhausted")

    def me(self) -> dict:
        return self._req("GET", "/me")

    def _list(self, path: str, id_field: str, **params) -> list[dict]:
        """Read every page. A row seen twice means the list shifted under us (offset paging while
        something was inserted) - re-read from the start rather than plan from a corrupt snapshot."""
        for _ in range(3):
            out, seen, page, total = [], set(), 1, None
            shifted = False
            while True:
                d = self._req("GET", path, params={**params, "page": page, "page_size": self.page_size})
                if not isinstance(d, dict) or "data" not in d:
                    raise CRMError(f"unexpected list envelope from {path}: keys {list(d) if isinstance(d, dict) else type(d)}")
                total = d.get("total", total)
                for row in d["data"]:
                    rid = row.get(id_field)
                    if rid in seen:
                        shifted = True
                        break
                    seen.add(rid)
                    out.append(row)
                if shifted or not d["data"] or (total is not None and len(out) >= total):
                    break
                page += 1
            if not shifted and (total is None or len(out) == total):
                return out
        raise CRMError(f"could not read a consistent snapshot of {path} (list kept changing or total mismatch)")

    def list_accounts(self, **params) -> list[dict]:
        return self._list("/accounts", "account_id", **params)

    def list_contacts(self, **params) -> list[dict]:
        return self._list("/contacts", "contact_id", **params)

    def get_account(self, account_id: str) -> dict:
        return record_from(self._req("GET", f"/accounts/{account_id}"))

    def create_account(self, fields: dict) -> dict:
        body = self._req("POST", "/accounts", json=fields)
        if not body:
            raise WriteUncertain("POST /accounts returned an empty body - cannot tell which account was created")
        return record_from(body)

    def patch_account(self, account_id: str, fields: dict) -> dict:
        body = self._req("PATCH", f"/accounts/{account_id}", json=fields)
        return record_from(body) if body else {}
