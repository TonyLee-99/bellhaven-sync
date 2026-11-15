"""One daily run: scrape -> read CRM -> plan -> queue proposals. Never writes to the CRM."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .config import Settings
from .crm import CRM
from .plan import Planner
from .state import State
from .website import Scraper


def crawl_complete(crawl: dict, locations) -> dict:
    """Only conclude 'no longer on the website' from a crawl we trust to be complete."""
    problems = []
    if crawl.get("directory_pages", 0) < 1:
        problems.append("no directory page was read")
    if not crawl.get("directory_finished", False):
        problems.append("directory pagination did not finish cleanly")
    for src in crawl.get("failed_sources", []):
        problems.append(f"could not read {src['path']}: {src['error']}")
    listed = sum(1 for l in locations if l.listed_in_directory)
    if crawl.get("directory_claimed_total") is not None and listed != crawl["directory_claimed_total"]:
        problems.append(f"directory says {crawl['directory_claimed_total']} communities, parsed {listed}")
    if crawl.get("homepage_claimed_total") is not None and len(locations) != crawl["homepage_claimed_total"]:
        problems.append(f"homepage says {crawl['homepage_claimed_total']} communities, found {len(locations)}")
    if crawl.get("failed_pages"):
        problems.append(f"detail pages failed: {[f['slug'] for f in crawl['failed_pages']]}")
    return {"ok": not problems, "problems": problems}


def run(settings: Settings, crm: CRM | None = None, scraper: Scraper | None = None,
        state: State | None = None, out_dir: Path | None = None, wait: bool = True) -> dict:
    state = state or State(settings.db_path)
    crm = crm or CRM(settings)
    scraper = scraper or Scraper(settings)
    out_dir = out_dir or settings.root / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    # exclusive: no approval can write while this run reads the CRM and plans from it
    with state.exclusive("pipeline run / CRM write", wait=wait):
        run_id = state.start_run()
        locations, crawl = scraper.scrape()
        complete = crawl_complete(crawl, locations)
        crawl["complete"] = complete
        accounts = crm.list_accounts()
        contacts = crm.list_contacts()
        locked_ids, locked_slugs = state.in_flight_targets()
        planner = Planner(settings.raw, accounts, contacts)
        proposals, report = planner.plan(locations, locked_ids=locked_ids, locked_slugs=locked_slugs,
                                         allow_absence=complete["ok"])
        payloads = [p.to_dict() for p in proposals]
        quarantined = {q["slug"] for q in report["quarantined"]} | {f["slug"] for f in crawl.get("failed_pages", [])}
        queue = state.sync_proposals(run_id, payloads, keep_scope={
            "slugs": set(report["locked"]) | quarantined, "ids": locked_ids, "absence_off": not report["absence_checks"]})
        stats = {"run_id": run_id, "crawl": crawl, "crm_accounts": len(accounts), "crm_contacts": len(contacts),
                 "proposals_generated": report["counts"], "queue": queue, "outcomes": _outcome_counts(report),
                 "quarantined": report["quarantined"], "locked": report["locked"], "deferred": report["deferred"],
                 "absence_checks": report["absence_checks"]}
        state.finish_run(run_id, stats)
        (out_dir / "last_run.json").write_text(json.dumps({"stats": stats, "rows": report["rows"]}, indent=2, default=str))
        (out_dir / "locations.json").write_text(json.dumps([l.to_dict() for l in locations], indent=2))
        status = {p["key"]: p["status"] for p in state.list()}
        with (out_dir / "proposals.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["key", "kind", "confidence", "title", "account_id", "field", "old", "new", "queue_status"])
            for p in payloads:
                for op in p["ops"]:
                    for k, v in op["fields"].items():
                        w.writerow([p["key"], p["kind"], p["confidence"], p["title"], op["account_id"] or "(new)",
                                    k, op["old"].get(k, ""), v, status.get(p["key"], "")])

    return stats


def _outcome_counts(report: dict) -> dict:
    out: dict[str, int] = {}
    for r in report["rows"]:
        out[r["outcome"]] = out.get(r["outcome"], 0) + 1
    return out
