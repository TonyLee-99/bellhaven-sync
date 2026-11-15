"""Test harness: the real pipeline against the in-process mock CRM and website mirror (no network)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bellhaven_sync.config import load_settings
from bellhaven_sync.crm import CRM
from bellhaven_sync.executor import apply_proposal
from bellhaven_sync.pipeline import run
from bellhaven_sync.state import State
from bellhaven_sync.website import Scraper
from devtools import mock_crm, site_mirror

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "devtools" / "seed"
SITE = ROOT / "devtools" / "site"


class Env:
    def run(self):
        return run(self.settings, crm=self.crm, scraper=self.scraper, state=self.state, out_dir=self.tmp / "out")

    def approve(self, key, reviewer="test"):
        p = self.state.get(key)
        assert self.state.approve_and_claim(key, reviewer, "", p["version"])
        return apply_proposal(self.crm, self.state, key, self.settings.raw)

    def approve_all(self):
        for p in self.state.list("pending"):
            self.approve(p["key"])

    def pending(self, kind=None, slug=None, title=None):
        return [p for p in self.state.list("pending") if (kind is None or p["kind"] == kind)
                and (slug is None or p.get("location_slug") == slug) and (title is None or title in p["title"])]

    def site_page(self, rel: str) -> Path:
        return self.site / rel


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSITE_BASE_URL", "http://testserver")
    monkeypatch.setenv("CRM_BASE_URL", "http://testserver/api/v1")
    monkeypatch.setenv("CRM_BROWSER_URL", "http://testserver/crm")
    monkeypatch.setenv("CRM_TOKEN", "mock_local_token")
    monkeypatch.delenv("ALLOW_REMOTE", raising=False)
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    settings = load_settings(tmp_path / "config.yaml", root=tmp_path)

    store = mock_crm.Store(tmp_path / "mock_state.json")
    store.seed(SEED)
    monkeypatch.setattr(mock_crm, "store", store)
    monkeypatch.setattr(mock_crm, "TOKEN", "mock_local_token")
    site_copy = tmp_path / "site"
    shutil.copytree(SITE, site_copy)                       # tests may edit their own copy of the website
    monkeypatch.setattr(site_mirror, "SITE", site_copy)

    e = Env()
    e.settings, e.store, e.tmp, e.site = settings, store, tmp_path, site_copy
    e.crm = CRM(settings, client=TestClient(mock_crm.app))
    e.scraper = Scraper(settings, client=TestClient(site_mirror.app))
    e.state = State(settings.db_path)
    e.seed_accounts = {a["account_id"]: a for a in json.loads((SEED / "accounts.json").read_text())}
    return e
