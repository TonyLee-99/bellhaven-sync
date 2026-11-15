"""Load config.yaml (with ${ENV} substitution) and environment settings."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
_ENV = re.compile(r"\$\{([A-Z0-9_]+)\}")


class RemoteNotAllowed(RuntimeError):
    pass


@dataclass
class Settings:
    raw: dict
    root: Path

    def __getitem__(self, key):
        return self.raw[key]

    @property
    def website_base(self) -> str:
        return self.raw["website"]["base_url"].rstrip("/")

    @property
    def crm_base(self) -> str:
        return self.raw["crm"]["base_url"].rstrip("/")

    @property
    def crm_browser(self) -> str:
        return (self.raw["crm"].get("browser_url") or "").rstrip("/")

    @property
    def token(self) -> str:
        tok = os.environ.get("CRM_TOKEN", "")
        if not tok:
            raise RuntimeError("CRM_TOKEN is not set (put it in .env)")
        return tok

    @property
    def env_name(self) -> str:
        """State namespace = CRM endpoint + workspace (token), so decisions made against one CRM copy
        (e.g. the local mock) can never suppress proposals for another (e.g. the real sandbox)."""
        u = urlparse(self.crm_base)
        host = u.hostname or "unknown"
        label = "local-mock" if host in {"127.0.0.1", "localhost", "testserver"} else host.split(".")[0]
        ident = hashlib.sha256(f"{u.netloc}{u.path}|{os.environ.get('CRM_TOKEN', '')}".encode()).hexdigest()[:8]
        return f"{label}-{ident}"

    @property
    def db_path(self) -> Path:
        p = Path(self.raw["state"]["db_path"].replace("{env}", self.env_name))
        return p if p.is_absolute() else self.root / p


def _subst(obj):
    if isinstance(obj, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), obj)
    if isinstance(obj, dict):
        return {k: _subst(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_subst(v) for v in obj]
    return obj


def guard_remote(url: str) -> None:
    """Refuse non-local hosts unless ALLOW_REMOTE=1 (keeps development fully offline)."""
    host = urlparse(url).hostname or ""
    if host in {"127.0.0.1", "localhost", "testserver"}:
        return
    if os.environ.get("ALLOW_REMOTE") == "1":
        return
    raise RemoteNotAllowed(f"Refusing to contact {host!r}: set ALLOW_REMOTE=1 to use a remote endpoint")


def load_settings(path: str | Path | None = None, root: Path | None = None) -> Settings:
    root = root or ROOT
    load_dotenv(root / ".env", override=False)
    path = Path(path) if path else root / "config.yaml"
    raw = _subst(yaml.safe_load(path.read_text()))
    s = Settings(raw=raw, root=root)
    if not s.website_base or not s.crm_base:
        raise RuntimeError("WEBSITE_BASE_URL and CRM_BASE_URL must be set (see .env.example)")
    return s
