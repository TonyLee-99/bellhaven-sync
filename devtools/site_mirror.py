"""Serve a saved copy of the Bellhaven website at the original paths (offline development/demo).

  /                      -> site/index.html
  /about                 -> site/about.html
  /communities[?page=N]  -> site/communities_pageN.html
  /communities/<slug>    -> site/communities/<slug>.html

Run: uv run python -m devtools.site_mirror   (port 8200)
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

SITE = Path(__file__).resolve().parent / "site"
app = FastAPI(title="Bellhaven website (LOCAL MIRROR)")


def _read(p: Path) -> HTMLResponse:
    if not p.is_file():
        raise HTTPException(404, "Not Found")
    return HTMLResponse(p.read_text())


@app.get("/", response_class=HTMLResponse)
def home():
    return _read(SITE / "index.html")


@app.get("/about", response_class=HTMLResponse)
def about():
    return _read(SITE / "about.html")


@app.get("/communities", response_class=HTMLResponse)
def communities(page: int = 1):
    return _read(SITE / f"communities_page{page}.html")


@app.get("/communities/{slug}", response_class=HTMLResponse)
def community(slug: str):
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        raise HTTPException(404, "Not Found")
    return _read(SITE / "communities" / f"{slug}.html")


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8200)
    uvicorn.run(app, host="127.0.0.1", port=ap.parse_args().port, log_level="warning")


if __name__ == "__main__":
    main()
