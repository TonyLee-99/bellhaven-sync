"""Command line entry point: `bhsync run | review | status`."""
from __future__ import annotations

import argparse
import json

from .config import load_settings
from .state import State


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="bhsync", description="Bellhaven website -> CRM ownership sync")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="scrape + match + queue proposals (never writes to the CRM)")
    rv = sub.add_parser("review", help="start the local review app")
    rv.add_argument("--port", type=int, default=8300)
    sub.add_parser("status", help="show queue counts and the last run")
    args = ap.parse_args(argv)
    s = load_settings()

    if args.cmd == "run":
        from .pipeline import run
        stats = run(s)
        print(json.dumps(stats, indent=2))
    elif args.cmd == "review":
        import uvicorn
        from .review.app import create_app
        uvicorn.run(create_app(s), host="127.0.0.1", port=args.port, log_level="warning")
    elif args.cmd == "status":
        st = State(s.db_path)
        last = st.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        print(json.dumps({"queue": st.counts(), "last_run": dict(last) if last else None}, indent=2, default=str))


if __name__ == "__main__":
    main()
